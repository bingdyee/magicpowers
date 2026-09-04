"""Agents that turn user text into durable memory operations."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol
from uuid import uuid4

from google.adk.agents import Agent, RunConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools import ToolContext
from google.genai import types


MemoryOperation = Mapping[str, object]
CuratorFunction = Callable[
    [str, Sequence[Mapping[str, object]]],
    Sequence[MemoryOperation] | Awaitable[Sequence[MemoryOperation]],
]


class MemoryAgentProtocol(Protocol):
    async def run(
        self,
        input_text: str,
        existing_memories: Sequence[Mapping[str, object]],
    ) -> Sequence[MemoryOperation]: ...


def _system_prompt(existing_memories: Sequence[Mapping[str, object]]) -> str:
    system_prompt_lines = [
        "You are a Memory Manager that is responsible for managing information and preferences about the user. "
        "You will be provided with a criteria for memories to capture in the <memories_to_capture> section and a list of existing memories in the <existing_memories> section.",
        "",
        "## When to add or update memories",
        "- Your first task is to decide if a memory needs to be added, updated, or deleted based on the user's message OR if no changes are needed.",
        "- If the user's message meets the criteria in the <memories_to_capture> section and that information is not already captured in the <existing_memories> section, you should capture it as a memory.",
        "- If the users messages does not meet the criteria in the <memories_to_capture> section, no memory updates are needed.",
        "- If the existing memories in the <existing_memories> section capture all relevant information, no memory updates are needed.",
        "",
        "## How to add or update memories",
        "- If you decide to add a new memory, create memories that captures key information, as if you were storing it for future reference.",
        "- Memories should be a brief, third-person statements that encapsulate the most important aspect of the user's input, without adding any extraneous information.",
        "  - Example: If the user's message is 'I'm going to the gym', a memory could be `John Doe goes to the gym regularly`.",
        "  - Example: If the user's message is 'My name is John Doe', a memory could be `User's name is John Doe`.",
        "- Don't make a single memory too long or complex, create multiple memories if needed to capture all the information.",
        "- Don't repeat the same information in multiple memories. Rather update existing memories if needed.",
        "- If a user asks for a memory to be updated or forgotten, remove all reference to the information that should be forgotten. Don't say 'The user used to like ...`",
        "- When updating a memory, append the existing memory with new information rather than completely overwriting it.",
        "- When a user's preferences change, update the relevant memories to reflect the new preferences but also capture what the user's preferences used to be and what has changed.",
        "",
        "## Criteria for creating memories",
        "Use the following criteria to determine if a user's message should be captured as a memory.",
        "",
        "<memories_to_capture>",
        "Memories should capture personal information about the user that is relevant to the current conversation, such as:",
        "  - Personal facts: name, age, occupation, location, interests, and preferences",
        "  - Opinions and preferences: what the user likes, dislikes, enjoys, or finds frustrating",
        "  - Significant life events or experiences shared by the user",
        "  - Important context about the user's current situation, challenges, or goals",
        "  - Any other details that offer meaningful insight into the user's personality, perspective, or needs",
        "</memories_to_capture>",
        "",
        "## Updating memories",
        "You will also be provided with a list of existing memories in the <existing_memories> section. You can:",
        "  - Decide to make no changes.",
        "  - Decide to add a new memory, using the `add_memory` tool.",
        "  - Decide to update an existing memory, using the `update_memory` tool.",
        "  - Decide to delete an existing memory, using the `delete_memory` tool.",
        "You can call multiple tools in a single response if needed. ",
        "Only add or update memories if it is necessary to capture key information provided by the user.",
    ]
    if existing_memories:
        system_prompt_lines.append("\n<existing_memories>")
        for existing_memory in existing_memories:
            system_prompt_lines.append(f"ID: {existing_memory['id']}")
            system_prompt_lines.append(f"Memory: {existing_memory['memory']}")
            system_prompt_lines.append("")
        system_prompt_lines.append("</existing_memories>")
    return "\n".join(system_prompt_lines)


class CallableMemoryAgent:
    """Adapter for applications that provide their own memory processor."""

    def __init__(self, function: CuratorFunction) -> None:
        self._function = function

    async def run(
        self,
        input_text: str,
        existing_memories: Sequence[Mapping[str, object]],
    ) -> Sequence[MemoryOperation]:
        result = self._function(input_text, existing_memories)
        return await result if inspect.isawaitable(result) else result


class MemoryAgent:
    """Default model-backed agent that emits memory operations."""

    def __init__(self, *, model: str | BaseLlm) -> None:
        self._model = model

    async def run(
        self,
        input_text: str,
        existing_memories: Sequence[Mapping[str, object]],
    ) -> Sequence[MemoryOperation]:
        operations: list[MemoryOperation] = []

        def add_memory(
            memory: str, topics: list[str] | None = None, tool_context: ToolContext | None = None
        ) -> dict[str, str]:
            operation: dict[str, object] = {"action": "add", "memory": memory}
            if topics is not None:
                operation["topics"] = topics
            operations.append(operation)
            if tool_context is not None:
                tool_context.actions.skip_summarization = True
            return {"status": "added"}

        def update_memory(
            memory_id: str, memory: str, topics: list[str] | None = None, tool_context: ToolContext | None = None
        ) -> dict[str, str]:
            operation: dict[str, object] = {"action": "update", "id": memory_id, "memory": memory}
            if topics is not None:
                operation["topics"] = topics
            operations.append(operation)
            if tool_context is not None:
                tool_context.actions.skip_summarization = True
            return {"status": "updated"}

        def delete_memory(memory_id: str, tool_context: ToolContext | None = None) -> dict[str, str]:
            operations.append({"action": "delete", "id": memory_id})
            if tool_context is not None:
                tool_context.actions.skip_summarization = True
            return {"status": "deleted"}

        agent = Agent(
            name="memory_manager",
            model=self._model,
            instruction=_system_prompt(existing_memories),
            tools=[add_memory, update_memory, delete_memory],
        )
        runner = Runner(
            agent=agent,
            app_name="memory_manager",
            session_service=InMemorySessionService(),
            auto_create_session=True,
        )
        async with runner:
            async for _ in runner.run_async(
                user_id="memory_service",
                session_id=str(uuid4()),
                new_message=types.UserContent(parts=[types.Part(text=input_text)]),
                run_config=RunConfig(max_llm_calls=5),
            ):
                pass
        return operations
