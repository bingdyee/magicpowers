from __future__ import annotations

from typing import Any, Literal

from google.adk.tools.tool_context import ToolContext

from .registry import get_registry
from .runner import run_child
from .transcript import parent_transcript


_ACTIONS = ("status", "result", "wait", "cancel", "list")


async def subagent(
    goal: str | None = None,
    context: str = "",
    *,
    background: bool = False,
    include_transcript: bool = False,
    action: Literal["status", "result", "wait", "cancel", "list"] | None = None,
    toolsets: list[str] | None = None,
    skills: list[str] | None = None,
    task_id: str | None = None,
    task_ids: list[str] | None = None,
    wait: bool = True,
    timeout_s: float | None = None,
    model: str | None = None,
    tools: list[str] | None = None,
    instructions: str | None = None,
    inline_skills: list[dict[str, str]] | None = None,
    output_format: Literal["text", "json"] = "text",
    output_schema: dict[str, Any] | None = None,
    max_iterations: int | None = None,
    name: str | None = None,
    profile: Literal["explore"] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Run or manage an isolated child agent.

    Pass a goal to run a blocking child. Set background=True to return a task_id
    immediately. Manage background tasks with action=status/result/wait/cancel/list.
    Toolsets are file, shell, and web; profile=explore restricts the child to
    read-only workspace tools. Set include_transcript=True to pass recent parent
    conversation text. Children do not receive this tool and cannot recurse.
    """
    registry = get_registry()
    if action is not None:
        if action not in _ACTIONS:
            return {"success": False, "error": f"Unknown action {action!r}. Available actions: {_ACTIONS}"}
        if action in {"status", "result", "cancel"} and task_id is None:
            return {"success": False, "error": f"action={action!r} requires task_id"}
        if action == "status":
            assert task_id is not None
            return await registry.get_status(task_id)
        if action == "result":
            assert task_id is not None
            return await registry.get_result(task_id, wait=wait, timeout_s=120 if timeout_s is None else timeout_s)
        if action == "wait":
            return await registry.wait(task_ids=task_ids, timeout_s=120 if timeout_s is None else timeout_s)
        if action == "cancel":
            assert task_id is not None
            return {"task_id": task_id, "cancelled": await registry.cancel(task_id)}
        return {"active": await registry.list_active()}

    if goal is None or not goal.strip():
        return {"success": False, "error": "goal is required to start a subagent task"}

    if include_transcript and tool_context is not None:
        invocation = getattr(tool_context, "_invocation_context", None)
        events = getattr(getattr(invocation, "session", None), "events", None)
        transcript = parent_transcript(events)
        if transcript:
            block = f"## Parent conversation so far\n\n{transcript}"
            context = f"{block}\n\n{context.strip()}" if context.strip() else block

    kwargs = {
        "goal": goal,
        "context": context,
        "toolsets": toolsets,
        "skills": skills,
        "timeout_s": (300 if background else 120) if timeout_s is None else timeout_s,
        "model": model,
        "tools": tools,
        "instructions": instructions,
        "inline_skills": inline_skills,
        "output_format": output_format,
        "output_schema": output_schema,
        "max_iterations": max_iterations,
        "name": name,
        "profile": profile,
    }
    if not background:
        try:
            return await run_child(**kwargs)
        except Exception as exc:
            return {"status": "halted", "summary": str(exc), "error": str(exc), "telemetry": {}}

    async def factory() -> dict[str, Any]:
        return await run_child(**kwargs)

    new_task_id = await registry.register(coro_factory=factory, goal=goal, name=name)
    return {"success": True, "task_id": new_task_id, "status": "pending"}
