"""ADK-facing memory service that coordinates agents and SQL storage."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
import json
import threading
from pathlib import Path

from google.adk.agents.callback_context import CallbackContext
from google.adk.events.event import Event
from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.session import Session

from .extractor import CallableMemoryAgent, CuratorFunction, MemoryAgent, MemoryAgentProtocol
from .store import MemoryStore, content_text


def _event_id(event: Event) -> str:
    if event.id:
        return event.id
    identity = json.dumps([event.author, event.timestamp, content_text(event.content)], ensure_ascii=False).encode()
    return f"event-{sha256(identity).hexdigest()}"


class PersistentMemoryService(BaseMemoryService):
    """Persistent user facts with model-driven add/update/delete operations."""

    def __init__(
        self,
        uri: str = "sqlite://",
        *,
        memory_agent: MemoryAgentProtocol | None = None,
        curator_fn: CuratorFunction | None = None,
        agents_dir: str | Path | None = None,
    ) -> None:
        self._store = MemoryStore(uri, agents_dir=agents_dir)
        self._model = self._store.options.get("model") or "openai/gpt-5.6-terra"
        if memory_agent is not None and curator_fn is not None:
            raise ValueError("Provide memory_agent or curator_fn, not both")
        self._memory_agent = memory_agent or (
            CallableMemoryAgent(curator_fn) if curator_fn is not None else MemoryAgent(model=self._model)
        )
        self._curation_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._lock = threading.RLock()

    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        await asyncio.to_thread(self._store.upsert_entries, app_name, user_id, memories, custom_metadata)

    def _user_events(self, events: Sequence[Event]) -> list[tuple[str, str, Event]]:
        candidates: dict[str, tuple[str, str, Event]] = {}
        for event in events:
            text = content_text(event.content)
            if (event.author or "").casefold() != "user" or not text:
                continue
            event_id = _event_id(event)
            candidates.setdefault(event_id, (event_id, text, event))
        return list(candidates.values())

    async def _curate_events(
        self, *, app_name: str, user_id: str, events: Sequence[Event], custom_metadata: Mapping[str, object] | None
    ) -> None:
        with self._lock:
            curation_lock = self._curation_locks.setdefault((app_name, user_id), asyncio.Lock())
        async with curation_lock:
            user_events = self._user_events(events)
            if not user_events:
                return
            input_text = "\n".join(text for _, text, _ in user_events)
            existing = self._store.existing_for_agent(app_name, user_id)
            operations = await self._memory_agent.run(input_text, existing)
            self._store.apply_operations(
                app_name, user_id, operations, custom_metadata, int(datetime.now().timestamp())
            )

    async def add_session_to_memory(self, session: Session) -> None:
        await self._curate_events(
            app_name=session.app_name, user_id=session.user_id, events=session.events, custom_metadata=None
        )

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        del session_id
        await self._curate_events(app_name=app_name, user_id=user_id, events=events, custom_metadata=custom_metadata)

    async def search_memory(self, *, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        if not query.strip():
            return SearchMemoryResponse()
        return await asyncio.to_thread(self._store.search, app_name, user_id, query)

    def close(self) -> None:
        self._store.close()


async def generate_memories_callback(callback_context: CallbackContext) -> None:
    await callback_context.add_session_to_memory()
