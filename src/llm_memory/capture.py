"""Persist Codex conversation turns directly in the session's Markdown journal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from llm_memory.markdown_store import MarkdownStore
from llm_memory.schemas import AgentSessionCreate, HandoffCreate
from llm_memory.scope import project_for_directory


def capture_event(event: dict[str, Any]) -> dict[str, Any] | None:
    external = event.get("session_id")
    if not isinstance(external, str) or not external:
        return None
    store = MarkdownStore()
    project = project_for_directory(Path(event.get("cwd") or Path.cwd()))
    external_id = f"codex:{external}"
    with store.transaction() as state:
        existing = next(
            (s for s in state.sessions.values() if s["external_id"] == external_id), None
        )
        previous = sorted(
            (h for h in state.handoffs.values() if h["project_id"] == project),
            key=lambda h: h["created_at"],
        )
    hook = event.get("hook_event_name")
    if existing and existing["status"] != "active":
        return None
    if existing is None:
        existing = store.start(
            AgentSessionCreate(
                external_id=external_id,
                agent_id="codex",
                project_id=project,
                title=f"Codex: {project}",
            )
        )
    session_id = existing["id"]
    if hook == "SessionStart":
        if previous:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": (
                        f"Saved conversation from the latest Codex session in project {project}:\n"
                        + previous[-1]["summary"][-7000:]
                    ),
                }
            }
    elif hook in {"UserPromptSubmit", "Stop"}:
        role = "user" if hook == "UserPromptSubmit" else "assistant"
        content = event.get("prompt" if role == "user" else "last_assistant_message")
        if not isinstance(content, str) or not content.strip():
            return None
        turn_id = event.get("turn_id")
        tag = f"turn:{turn_id}:{role}" if isinstance(turn_id, str) else None
        # Deduplication and append share the checkout lock, including hook retries.
        store.capture_turn(session_id, role, content[:40_000], tag)
    elif hook == "SessionEnd":
        memories = store.list_memories(session_id=session_id)
        transcript = "\n\n---\n\n".join(m["content"] for m in memories)
        store.handoff(
            session_id,
            HandoffCreate(
                summary=(transcript[-90_000:] or "Nenhuma mensagem capturada nesta sessão.")
            ),
        )
    return None
