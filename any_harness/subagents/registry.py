from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any


TaskFactory = Callable[[], Coroutine[Any, Any, dict[str, Any]]]


@dataclass
class SubAgentHandle:
    task_id: str
    goal: str
    task: asyncio.Task[dict[str, Any]]
    started_at: float
    name: str | None = None
    consumed: bool = False
    cancelled: bool = False


def _status(handle: SubAgentHandle) -> str:
    if handle.cancelled or handle.task.cancelled():
        return "cancelled"
    if not handle.task.done():
        return "running"
    return "failed" if handle.task.exception() is not None else "completed"


class SubAgentRegistry:
    """Track background child-agent tasks in the current process."""

    def __init__(self) -> None:
        self._handles: dict[str, SubAgentHandle] = {}
        self._lock = asyncio.Lock()

    async def register(self, *, coro_factory: TaskFactory, goal: str, name: str | None = None) -> str:
        task_id = uuid.uuid4().hex[:12]
        task = asyncio.create_task(coro_factory(), name=f"subagent-{task_id}")
        handle = SubAgentHandle(task_id, goal, task, time.monotonic(), name=name)
        async with self._lock:
            self._handles[task_id] = handle
        return task_id

    async def get_status(self, task_id: str) -> dict[str, Any]:
        async with self._lock:
            handle = self._handles.get(task_id)
        if handle is None:
            return {"status": "unknown", "task_id": task_id}
        result: dict[str, Any] = {
            "status": _status(handle),
            "task_id": task_id,
            "goal": handle.goal,
            "elapsed_s": round(time.monotonic() - handle.started_at, 3),
        }
        if handle.name:
            result["name"] = handle.name
        return result

    async def get_result(self, task_id: str, *, wait: bool = True, timeout_s: float = 120) -> dict[str, Any]:
        async with self._lock:
            handle = self._handles.get(task_id)
        if handle is None:
            return {"status": "unknown", "task_id": task_id}

        if not handle.task.done() and wait:
            try:
                await asyncio.wait_for(asyncio.shield(handle.task), timeout=timeout_s)
            except TimeoutError:
                return {"status": "timeout", "task_id": task_id}
            except asyncio.CancelledError:
                return {"status": "cancelled", "task_id": task_id}
            except Exception as exc:
                return {"status": "failed", "task_id": task_id, "error": str(exc)}

        status = _status(handle)
        if status == "running":
            return {"status": status, "task_id": task_id}
        if status == "cancelled":
            return {"status": status, "task_id": task_id}
        if status == "failed":
            return {"status": status, "task_id": task_id, "error": str(handle.task.exception())}
        return {"status": status, "task_id": task_id, "result": handle.task.result()}

    async def wait(self, *, task_ids: list[str] | None = None, timeout_s: float = 120) -> dict[str, Any]:
        async with self._lock:
            if task_ids is None:
                handles = [handle for handle in self._handles.values() if not handle.consumed]
            else:
                handles = [self._handles[task_id] for task_id in task_ids if task_id in self._handles]
        if not handles:
            return {"status": "no_active_tasks"}

        done, _ = await asyncio.wait(
            {handle.task for handle in handles},
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            return {"status": "timeout", "still_running": [handle.task_id for handle in handles]}

        finished = next(handle for handle in handles if handle.task in done)
        async with self._lock:
            finished.consumed = True
        result = await self.get_result(finished.task_id, wait=False)
        return {
            "task_id": finished.task_id,
            "status": result["status"],
            "result": result.get("result"),
            "error": result.get("error"),
            "still_running": [
                handle.task_id for handle in handles if handle is not finished and not handle.task.done()
            ],
        }

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            handle = self._handles.get(task_id)
        if handle is None:
            return False
        handle.cancelled = True
        cancelled = handle.task.cancel()
        return cancelled or handle.task.done()

    async def list_active(self) -> list[dict[str, Any]]:
        async with self._lock:
            handles = list(self._handles.values())
        return [await self.get_status(handle.task_id) for handle in handles if _status(handle) == "running"]

    async def cancel_all(self) -> None:
        async with self._lock:
            handles = list(self._handles.values())
        current_loop = asyncio.get_running_loop()
        pending = []
        for handle in handles:
            handle.cancelled = True
            if not handle.task.done() and handle.task.get_loop() is current_loop:
                handle.task.cancel()
                pending.append(handle.task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


_registry: SubAgentRegistry | None = None


def get_registry() -> SubAgentRegistry:
    global _registry
    if _registry is None:
        _registry = SubAgentRegistry()
    return _registry


async def close_registry() -> None:
    global _registry
    if _registry is None:
        return
    registry = _registry
    _registry = None
    await registry.cancel_all()
