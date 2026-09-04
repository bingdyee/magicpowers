from __future__ import annotations

from typing import Any


MAX_TRANSCRIPT_CHARS = 4_000


def parent_transcript(events: Any) -> str:
    """Render recent parent user/assistant text without tool payloads."""
    turns: list[str] = []
    for event in events or []:
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) if content else None
        text = "".join(
            part.text
            for part in parts or []
            if getattr(part, "text", None)
            and not getattr(part, "function_call", None)
            and not getattr(part, "function_response", None)
        ).strip()
        if text:
            role = "User" if getattr(event, "author", None) == "user" else "Assistant"
            turns.append(f"{role}: {text}")

    kept: list[str] = []
    size = 0
    for turn in reversed(turns):
        size += len(turn) + 1
        if size > MAX_TRANSCRIPT_CHARS:
            break
        kept.append(turn)
    return "\n".join(reversed(kept))
