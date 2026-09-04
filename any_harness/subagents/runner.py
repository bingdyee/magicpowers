from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from typing import Any, Literal

from google.adk.agents import Agent, BaseAgent
from google.adk.memory import InMemoryMemoryService
from google.adk.models import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from ..tools import WebSearchToolset, WorkspaceToolset, load_skills_from_dir, resolve_working_path

_APP_NAME = "pardx_subagent"
_USER_ID = "parent_agent"
_DEFAULT_TOOLSETS = ("file", "shell")
_TOOLSET_ALIASES = {
    "file": ("read", "list", "search", "write", "edit", "move", "delete"),
    "shell": ("shell",),
}
_WEB_TOOLS = ("web_search", "load_web_page")
_TOOL_NAMES = {
    "read_file": "read",
    "list_files": "list",
    "search_content": "search",
    "write_file": "write",
    "edit_file": "edit",
    "move_file": "move",
    "delete_file": "delete",
    "run_command": "shell",
    "bash": "shell",
}
_ALL_ALIASES = tuple(alias for aliases in _TOOLSET_ALIASES.values() for alias in aliases)
_PROFILES = {"explore": frozenset({"read", "list", "search"})}
_NAME_SANITIZER = re.compile(r"[^a-zA-Z0-9_]+")

_BASE_INSTRUCTION = (
    "You are a delegated worker with an isolated conversation and no memory of the parent chat. "
    "Complete the self-contained task, then return a concise summary of what you did, what you found, "
    "and any file paths or errors the parent needs. Make reasonable assumptions for minor ambiguity and "
    "state them. Do not claim work is complete unless you verified it."
)


def _resolve_tools(
    toolsets: list[str] | None, tools: list[str] | None, profile: str | None
) -> tuple[list[str], list[str]]:
    if profile is not None and profile not in _PROFILES:
        raise ValueError(f"Unknown profile {profile!r}. Available profiles: {sorted(_PROFILES)}")

    selected: list[str] = []
    selected_toolsets = [] if toolsets is None and tools else list(toolsets or _DEFAULT_TOOLSETS)
    for toolset in selected_toolsets:
        if toolset == "web":
            selected.extend(_WEB_TOOLS)
        elif toolset in _TOOLSET_ALIASES:
            selected.extend(_TOOLSET_ALIASES[toolset])
        else:
            available = sorted((*_TOOLSET_ALIASES, "web"))
            raise KeyError(f"Unknown toolset {toolset!r}. Available toolsets: {available}")
    for tool in tools or []:
        alias = _TOOL_NAMES.get(tool, tool)
        if alias not in _ALL_ALIASES and alias not in _WEB_TOOLS:
            available = sorted((*_ALL_ALIASES, *_TOOL_NAMES, *_WEB_TOOLS))
            raise KeyError(f"Unknown tool {tool!r}. Available tools: {available}")
        selected.append(alias)

    allowed = _PROFILES[profile] if profile is not None else None
    if allowed is not None:
        selected = [tool for tool in selected if tool in allowed]
    aliases = [alias for alias in _ALL_ALIASES if alias in selected]
    web_tools = [tool for tool in _WEB_TOOLS if tool in selected]
    return aliases, web_tools


def _skill_blocks(skill_names: list[str]) -> list[str]:
    catalog = {skill.frontmatter.name: skill for skill in load_skills_from_dir(resolve_working_path("skills"))}
    blocks = []
    for name in skill_names:
        skill = catalog.get(name)
        body = skill.instructions.strip() if skill else "_Requested skill was not found._"
        blocks.append(f"### Skill: {name}\n\n{body}")
    return blocks


def _instruction(
    goal: str,
    context: str,
    skills: list[str],
    instructions: str | None,
    inline_skills: list[dict[str, str]],
    output_format: Literal["text", "json"],
) -> str:
    parts = [_BASE_INSTRUCTION, f"## Goal\n\n{goal.strip()}"]
    if context.strip():
        parts.append(f"## Context\n\n{context.strip()}")
    skill_blocks = _skill_blocks(skills)
    skill_blocks.extend(
        f"### Skill: {skill.get('name', 'unnamed')}\n\n{skill.get('body', '').strip()}" for skill in inline_skills
    )
    if skill_blocks:
        parts.append("## Available skills\n\n" + "\n\n".join(skill_blocks))
    if instructions and instructions.strip():
        parts.append(f"## Caller instructions\n\n{instructions.strip()}")
    if output_format == "json":
        parts.append("## Output format\n\nReturn only one JSON object, without prose or Markdown fences.")
    return "\n\n".join(parts)


async def build_child_agent(
    *,
    goal: str,
    context: str = "",
    toolsets: list[str] | None = None,
    skills: list[str] | None = None,
    model: str | None = None,
    tools: list[str] | None = None,
    instructions: str | None = None,
    inline_skills: list[dict[str, str]] | None = None,
    output_format: Literal["text", "json"] = "text",
    output_schema: dict[str, Any] | None = None,
    name: str | None = None,
    profile: str | None = None,
) -> Agent:
    aliases, web_tools = _resolve_tools(toolsets, tools, profile)
    child_tools: list[Any] = []
    if aliases:
        child_tools.append(WorkspaceToolset(root=resolve_working_path("workspace"), allowed=aliases, confirm=[]))
    if web_tools:
        child_tools.append(
            WebSearchToolset(
                enable_search="web_search" in web_tools,
                enable_news="load_web_page" in web_tools,
            )
        )
    child_name = _NAME_SANITIZER.sub("_", name or "delegate").strip("_") or "delegate"
    if child_name[0].isdigit():
        child_name = f"delegate_{child_name}"
    child_model = model or os.environ.get("MODEL") or "openai/gpt-5.6-sol"
    kwargs: dict[str, Any] = {
        "name": f"{child_name[:32]}_{uuid.uuid4().hex[:8]}",
        "model": LiteLlm(model=child_model),
        "instruction": _instruction(
            goal,
            context,
            list(skills or []),
            instructions,
            list(inline_skills or []),
            output_format,
        ),
        "tools": child_tools,
    }
    if output_format == "json" and output_schema:
        kwargs["output_schema"] = output_schema
    return Agent(**kwargs)


def _event_text(event: Any) -> str:
    content = getattr(event, "content", None)
    return "".join(part.text for part in getattr(content, "parts", None) or [] if getattr(part, "text", None))


async def run_agent(
    child: BaseAgent,
    *,
    timeout_s: float = 120,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    session_service = InMemorySessionService()
    runner = Runner(
        app_name=_APP_NAME,
        agent=child,
        session_service=session_service,
        memory_service=InMemoryMemoryService(),
    )
    session = await session_service.create_session(app_name=_APP_NAME, user_id=_USER_ID)
    chunks: list[str] = []
    iterations = 0
    started_at = time.monotonic()

    async def drain() -> None:
        nonlocal iterations
        async for event in runner.run_async(
            user_id=_USER_ID,
            session_id=session.id,
            new_message=Content(role="user", parts=[Part(text="Complete the task in your system instruction.")]),
        ):
            iterations += 1
            if text := _event_text(event):
                chunks.append(text)
            if max_iterations is not None and iterations >= max_iterations:
                raise RuntimeError(f"max_iterations ({max_iterations}) exceeded")

    status = "completed"
    error = None
    try:
        await asyncio.wait_for(drain(), timeout=timeout_s)
    except TimeoutError:
        status = "timeout"
        error = f"Child timed out after {timeout_s}s without finishing."
    except Exception as exc:
        status = "halted"
        error = f"Child halted with {type(exc).__name__}: {exc}"
    finally:
        await runner.close()

    partial = "".join(chunks).strip()
    if status == "completed":
        summary = partial or "The child finished but reported no result."
    else:
        summary = f"INCOMPLETE: {error} Do not report this work as done."
        if partial:
            summary += f"\n\nPartial output:\n{partial}"
    result: dict[str, Any] = {
        "status": status,
        "summary": summary,
        "telemetry": {
            "iterations": iterations,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        },
    }
    if error:
        result["error"] = error
    return result


def _parse_json_result(result: dict[str, Any]) -> dict[str, Any]:
    if result["status"] != "completed":
        return result
    raw = result["summary"].strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        result["summary"] = json.loads(raw)
    except json.JSONDecodeError as exc:
        result["status"] = "halted"
        result["summary_raw"] = result["summary"]
        result["summary"] = f"Child returned invalid JSON: {exc}"
        result["error"] = result["summary"]
    return result


async def run_child(
    *,
    goal: str,
    context: str = "",
    toolsets: list[str] | None = None,
    skills: list[str] | None = None,
    timeout_s: float = 120,
    model: str | None = None,
    tools: list[str] | None = None,
    instructions: str | None = None,
    inline_skills: list[dict[str, str]] | None = None,
    output_format: Literal["text", "json"] = "text",
    output_schema: dict[str, Any] | None = None,
    max_iterations: int | None = None,
    name: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    child = await build_child_agent(
        goal=goal,
        context=context,
        toolsets=toolsets,
        skills=skills,
        model=model,
        tools=tools,
        instructions=instructions,
        inline_skills=inline_skills,
        output_format=output_format,
        output_schema=output_schema,
        name=name,
        profile=profile,
    )
    result = await run_agent(child, timeout_s=timeout_s, max_iterations=max_iterations)
    return _parse_json_result(result) if output_format == "json" else result
