"""SQL storage adapter for durable user memories."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from google.adk.memory.memory_entry import MemoryEntry
from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.genai import types
from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    JSON,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    desc,
    func,
    inspect as sqlalchemy_inspect,
    select,
    update,
)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import StaticPool

USER_MEMORY_TABLE_SCHEMA = {
    "id": {"type": String, "primary_key": True, "nullable": False},
    "memory": {"type": JSON, "nullable": False},
    "app_name": {"type": String, "nullable": True},
    "user_id": {"type": String, "nullable": True, "index": True},
    "topics": {"type": JSON, "nullable": True},
    "metadata": {"type": JSON, "nullable": True},
    "created_at": {"type": BigInteger, "nullable": False, "index": True},
    "updated_at": {"type": BigInteger, "nullable": True, "index": True},
}
_EXPECTED_INDEXES = {
    ("ix_memories_user_id", ("user_id",)),
    ("ix_memories_created_at", ("created_at",)),
    ("ix_memories_updated_at", ("updated_at",)),
}
_MEMORY_OPTION_NAMES = {"model", "top_k"}
_SUPPORTED_DIALECTS = {"mysql", "postgresql", "sqlite"}


def parse_memory_uri(uri: str) -> tuple[URL, dict[str, str]]:
    engine_url = make_url(uri)
    dialect = engine_url.get_backend_name()
    if dialect not in _SUPPORTED_DIALECTS:
        raise ValueError(f"Unsupported memory service URI: {uri}")
    options = {
        name: engine_url.normalized_query[name][-1]
        for name in _MEMORY_OPTION_NAMES
        if name in engine_url.normalized_query
    }
    engine_url = engine_url.difference_update_query(_MEMORY_OPTION_NAMES)
    if dialect == "postgresql" and engine_url.drivername == "postgresql":
        engine_url = engine_url.set(drivername="postgresql+psycopg")
    return engine_url, options


def positive_int(value: str | None, *, default: int, name: str) -> int:
    result = default if value is None or value == "" else int(value)
    if result < 1:
        raise ValueError(f"{name} must be at least 1")
    return result


def content_text(content: types.Content | None) -> str:
    if content is None:
        return ""
    return " ".join(part.text for part in (content.parts or []) if part.text).strip()


def epoch(timestamp: str | float | None) -> int:
    if isinstance(timestamp, (int, float)):
        return int(timestamp)
    if timestamp:
        try:
            return int(datetime.fromisoformat(timestamp).timestamp())
        except ValueError:
            pass
    return int(datetime.now().timestamp())


class MemoryStore:
    """Owns the SQL engine, schema validation, and memory CRUD operations."""

    def __init__(self, uri: str, *, agents_dir: str | Path | None = None) -> None:
        engine_url, self.options = parse_memory_uri(uri)
        self.top_k = positive_int(self.options.get("top_k"), default=10, name="top_k")
        self.lock = threading.RLock()
        self.metadata = MetaData()
        self.memories = Table(
            "memories",
            self.metadata,
            Column("id", String(255), primary_key=True, nullable=False),
            Column("memory", JSON, nullable=False),
            Column("app_name", String(255)),
            Column("user_id", String(255)),
            Column("topics", JSON),
            Column("metadata", JSON),
            Column("created_at", BigInteger, nullable=False),
            Column("updated_at", BigInteger),
            Index("ix_memories_user_id", "user_id"),
            Index("ix_memories_created_at", "created_at"),
            Index("ix_memories_updated_at", "updated_at"),
        )
        engine_options: dict[str, object] = {"future": True}
        if engine_url.get_backend_name() == "sqlite":
            database_path = engine_url.database
            if database_path is not None and database_path not in {"", ":memory:"}:
                path = Path(database_path)
                if agents_dir and not path.is_absolute():
                    path = Path(agents_dir) / path
                path.parent.mkdir(parents=True, exist_ok=True)
                engine_url = engine_url.set(database=path.resolve().as_posix())
            engine_options["connect_args"] = {"check_same_thread": False}
            if database_path is None or database_path in {"", ":memory:"}:
                engine_options["poolclass"] = StaticPool
        self.engine = create_engine(engine_url, **engine_options)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self.lock:
            self.metadata.create_all(self.engine)
            inspector = sqlalchemy_inspect(self.engine)
            if "memories" not in inspector.get_table_names():
                raise ValueError("Incompatible memory schema; memories table is missing")
            columns = {column["name"]: column for column in inspector.get_columns("memories")}
            primary_key = set(inspector.get_pk_constraint("memories").get("constrained_columns") or [])
            valid_columns = columns.keys() == USER_MEMORY_TABLE_SCHEMA.keys() and all(
                isinstance(columns[name]["type"], definition["type"])
                and bool(columns[name]["nullable"]) is definition["nullable"]
                for name, definition in USER_MEMORY_TABLE_SCHEMA.items()
            )
            indexes = {(index["name"], tuple(index["column_names"])) for index in inspector.get_indexes("memories")}
            if not valid_columns or primary_key != {"id"} or indexes != _EXPECTED_INDEXES:
                raise ValueError("Incompatible memories schema; migrate the database or use a new file")

    @staticmethod
    def row_to_memory(row: Any) -> MemoryEntry:
        raw_metadata = row.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        if row.get("topics") is not None:
            metadata["topics"] = row["topics"]
        return MemoryEntry(
            id=row["id"],
            content=types.Content(role="user", parts=[types.Part(text=str(row["memory"]))]),
            timestamp=metadata.pop("_timestamp", None),
            custom_metadata=metadata,
        )

    def upsert_entries(
        self, app_name: str, user_id: str, memories: Sequence[MemoryEntry], custom_metadata: Mapping[str, object] | None
    ) -> None:
        now = int(datetime.now().timestamp())
        with self.lock, self.engine.begin() as connection:
            for memory in memories:
                text = content_text(memory.content)
                if not text:
                    continue
                metadata = {**memory.custom_metadata, **dict(custom_metadata or {})}
                timestamp = memory.timestamp
                if timestamp is not None:
                    metadata["_timestamp"] = timestamp
                topics = metadata.pop("topics", None)
                memory_id = memory.id or str(uuid4())
                existing = (
                    connection.execute(select(self.memories).where(self.memories.c.id == memory_id)).mappings().first()
                )
                if existing and (existing["app_name"] != app_name or existing["user_id"] != user_id):
                    raise ValueError(f"Memory {memory_id} belongs to another scope")
                values = {
                    "id": memory_id,
                    "memory": text,
                    "app_name": app_name,
                    "user_id": user_id,
                    "topics": topics,
                    "metadata": metadata,
                    "created_at": epoch(timestamp),
                    "updated_at": now,
                }
                if existing:
                    connection.execute(
                        update(self.memories)
                        .where(self.memories.c.id == memory_id)
                        .values(memory=text, topics=topics, metadata=metadata, updated_at=now)
                    )
                else:
                    connection.execute(self.memories.insert().values(**values))

    def existing_for_agent(self, app_name: str, user_id: str) -> list[dict[str, object]]:
        with self.lock, self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(self.memories.c.id, self.memories.c.memory).where(
                        self.memories.c.app_name == app_name, self.memories.c.user_id == user_id
                    )
                )
                .mappings()
                .all()
            )
        return [{"id": row["id"], "memory": row["memory"]} for row in rows]

    def apply_operations(
        self,
        app_name: str,
        user_id: str,
        operations: Sequence[Mapping[str, object]],
        custom_metadata: Mapping[str, object] | None,
        now: int,
    ) -> None:
        with self.lock, self.engine.begin() as connection:
            for operation in operations:
                action = str(operation.get("action", "")).casefold()
                memory_id = str(operation.get("id") or operation.get("memory_id") or "")
                memory_text = str(operation.get("memory") or "").strip()
                if action in {"add", "update"} and not memory_text:
                    raise ValueError(f"Memory {action} operation requires a non-empty memory")
                if action in {"update", "delete"} and not memory_id:
                    raise ValueError(f"Memory {action} operation requires id")
                if action not in {"add", "update", "delete"}:
                    raise ValueError(f"Unsupported memory operation: {action}")
                if action == "delete":
                    connection.execute(
                        delete(self.memories).where(
                            self.memories.c.id == memory_id,
                            self.memories.c.app_name == app_name,
                            self.memories.c.user_id == user_id,
                        )
                    )
                    continue
                if action == "add":
                    memory_id = memory_id or str(uuid4())
                existing = (
                    connection.execute(select(self.memories).where(self.memories.c.id == memory_id)).mappings().first()
                )
                if action == "add" and existing:
                    raise ValueError(f"Memory {memory_id} already exists")
                if action == "update" and (
                    not existing or existing["app_name"] != app_name or existing["user_id"] != user_id
                ):
                    raise ValueError(f"Memory {memory_id} does not exist in this scope")
                raw_metadata = existing["metadata"] if existing else None
                metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
                metadata.update(custom_metadata or {})
                operation_metadata = operation.get("metadata", operation.get("custom_metadata"))
                if isinstance(operation_metadata, Mapping):
                    metadata.update(operation_metadata)
                topics = operation.get("topics", existing["topics"] if existing else None)
                if action == "update":
                    connection.execute(
                        update(self.memories)
                        .where(
                            self.memories.c.id == memory_id,
                            self.memories.c.app_name == app_name,
                            self.memories.c.user_id == user_id,
                        )
                        .values(memory=memory_text, topics=topics, metadata=metadata, updated_at=now)
                    )
                else:
                    connection.execute(
                        self.memories.insert().values(
                            id=memory_id,
                            memory=memory_text,
                            app_name=app_name,
                            user_id=user_id,
                            topics=topics,
                            metadata=metadata,
                            created_at=now,
                            updated_at=now,
                        )
                    )

    def search(self, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        with self.lock, self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(self.memories)
                    .where(
                        self.memories.c.app_name == app_name,
                        self.memories.c.user_id == user_id,
                    )
                    .order_by(desc(func.coalesce(self.memories.c.updated_at, self.memories.c.created_at)))
                    .limit(self.top_k)
                )
                .mappings()
                .all()
            )
        return SearchMemoryResponse(memories=[self.row_to_memory(row) for row in rows])

    def close(self) -> None:
        self.engine.dispose()
