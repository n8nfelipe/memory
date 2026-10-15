from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from llm_memory.markdown_cli import app as cli
from llm_memory.markdown_store import MarkdownStore
from llm_memory.opencode_capture import capture_opencode_event


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MarkdownStore:
    monkeypatch.setenv("MEMORY_STORAGE_BACKEND", "markdown")
    monkeypatch.setenv("MEMORY_REPOSITORY", str(tmp_path / "repository"))
    monkeypatch.delenv("MEMORY_EMBEDDING_URL", raising=False)
    monkeypatch.setenv("MEMORY_AUTH_REQUIRED", "false")
    return MarkdownStore()


def base(session: str = "ses-1", hook: str = "session.created") -> dict:
    return {
        "opencode_session_id": session,
        "hook": hook,
        "cwd": "/tmp",
        "title": "Revisar memória",
        "messages": [],
    }


def test_created_starts_opencode_session(store: MarkdownStore) -> None:
    assert capture_opencode_event(base()) is None
    with store.transaction() as state:
        assert len(state.sessions) == 1
        session = next(iter(state.sessions.values()))
        assert session["external_id"] == "opencode:ses-1"
        assert session["agent_id"] == "opencode"
        assert session["status"] == "active"
        assert session["title"] == "Revisar memória"


def test_idle_captures_turns_and_deduplicates(store: MarkdownStore) -> None:
    capture_opencode_event(base())
    payload = base(hook="session.idle")
    payload["messages"] = [
        {"id": "m1", "role": "user", "content": "Olá"},
        {"id": "m2", "role": "assistant", "content": "Oi"},
        {"id": "m3", "role": "tool", "content": "ignored"},
        {"id": "m4", "role": "assistant", "content": "   "},
        {"id": "", "role": "user", "content": "sem id"},
    ]
    capture_opencode_event(payload)
    capture_opencode_event(payload)
    memories = store.list_memories()
    assert len(memories) == 2
    assert memories[0]["content"] == "## User\n\nOlá"
    assert memories[0]["source"]["type"] == "opencode_hook"
    assert "opencode-msg:m1" in memories[0]["tags"]


def test_idle_truncates_long_messages(store: MarkdownStore) -> None:
    capture_opencode_event(base())
    payload = base(hook="session.idle")
    payload["messages"] = [{"id": "big", "role": "user", "content": "x" * 50_000}]
    capture_opencode_event(payload)
    (memory,) = store.list_memories()
    assert len(memory["content"]) <= 40_000 + len("## User\n\n")


def test_deleted_handoffs_once_and_closes_session(store: MarkdownStore) -> None:
    capture_opencode_event(base())
    payload = base(hook="session.idle")
    payload["messages"] = [{"id": "m1", "role": "user", "content": "Olá"}]
    capture_opencode_event(payload)
    capture_opencode_event(base(hook="session.deleted"))
    capture_opencode_event(base(hook="session.deleted"))
    with store.transaction() as state:
        assert len(state.handoffs) == 1
        handoff = next(iter(state.handoffs.values()))
        assert "Olá" in handoff["summary"]
        session = next(iter(state.sessions.values()))
        assert session["status"] != "active"
    # Closed sessions reject further writes.
    capture_opencode_event(payload)
    assert len(store.list_memories()) == 1


def test_invalid_payload_is_ignored(store: MarkdownStore) -> None:
    assert capture_opencode_event({}) is None
    assert capture_opencode_event({"opencode_session_id": ""}) is None
    with store.transaction() as state:
        assert not state.sessions


def test_cli_capture_opencode(store: MarkdownStore, tmp_path: Path) -> None:
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(base()), encoding="utf-8")
    result = CliRunner().invoke(cli, ["capture-opencode", "--input", str(payload_file)])
    assert result.exit_code == 0, result.output
    with store.transaction() as state:
        assert len(state.sessions) == 1
