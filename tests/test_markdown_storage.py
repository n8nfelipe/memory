from __future__ import annotations

import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from llm_memory.config import get_settings
from llm_memory.markdown_cli import app as cli
from llm_memory.markdown_migration import migrate_database
from llm_memory.markdown_store import MarkdownStore, parse, render
from llm_memory.schemas import (
    AgentSessionCreate,
    CompactionPreviewRequest,
    HandoffCreate,
    MemoryCreate,
    MemoryPatch,
    SearchRequest,
)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MarkdownStore:
    monkeypatch.setenv("MEMORY_STORAGE_BACKEND", "markdown")
    monkeypatch.setenv("MEMORY_REPOSITORY", str(tmp_path / "repository"))
    monkeypatch.delenv("MEMORY_EMBEDDING_URL", raising=False)
    monkeypatch.setenv("MEMORY_AUTH_REQUIRED", "false")
    return MarkdownStore()


def start(store: MarkdownStore, agent: str = "codex", project: str = "memory") -> str:
    return str(store.start(AgentSessionCreate(agent_id=agent, project_id=project))["id"])


def remember(store: MarkdownStore, content: str = "memória em português") -> dict:
    return store.create(MemoryCreate(content=content))["memory"]


def test_sessions_are_separate_and_restart_searches_all(store: MarkdownStore) -> None:
    first = start(store)
    a = remember(store)
    first_file = store.root / "sessions" / f"{first}.md"
    original = first_file.read_bytes()
    second = start(store, "claude")
    b = remember(store, "outra memória em português")
    assert a["session_id"] == first and b["session_id"] == second
    assert first_file.read_bytes() == original
    restarted = MarkdownStore(store.root)
    hits = restarted.search(SearchRequest(query="português", project_id="memory"))
    assert {hit["memory"]["id"] for hit in hits} == {a["id"], b["id"]}
    assert not list(store.root.rglob("*.db"))
    assert len(list((store.root / "sessions").glob("*.md"))) == 2


def test_corrections_and_handoffs_leave_closed_session_untouched(store: MarkdownStore) -> None:
    first = start(store)
    memory = remember(store)
    handoff = store.handoff(first, HandoffCreate(summary="continuar", to_agent="claude"))
    original = (store.root / "sessions" / f"{first}.md").read_bytes()
    second = start(store, "claude")
    store.update(memory["id"], MemoryPatch(expected_version=1, content="correção"), second)
    claim = store.claim("default", "memory", "claude", second)
    assert claim["id"] == handoff["id"]
    store.complete(handoff["id"], "claude", second)
    assert (store.root / "sessions" / f"{first}.md").read_bytes() == original
    assert [row["version"] for row in store.history(memory["id"])] == [1, 2]
    with pytest.raises(HTTPException, match="409"):
        store.update(memory["id"], MemoryPatch(expected_version=1, content="stale"))
    with pytest.raises(HTTPException, match="404"):
        store.create(MemoryCreate(content="late", session_id=first))


def test_concurrent_appends_and_claims_are_serialized(store: MarkdownStore) -> None:
    session = start(store)

    def append(index: int) -> None:
        MarkdownStore(store.root).create(MemoryCreate(content=f"entry {index}", session_id=session))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(24)))
    assert len(store.list_memories()) == 24
    store.handoff(session, HandoffCreate(summary="ready"))

    def claim(index: int) -> bool:
        try:
            MarkdownStore(store.root).claim("default", "memory", f"agent-{index}")
            return True
        except HTTPException as exc:
            assert exc.status_code == 404
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, range(8))) == 1


def test_offline_concurrent_corrections_fail_closed(store: MarkdownStore, tmp_path: Path) -> None:
    first = start(store)
    memory = remember(store)
    store.handoff(first, HandoffCreate(summary="done"))
    clone_root = tmp_path / "clone"
    shutil.copytree(store.root, clone_root)
    other = MarkdownStore(clone_root)
    a, b = start(store), start(other)
    store.update(memory["id"], MemoryPatch(expected_version=1, content="left"), a)
    other.update(memory["id"], MemoryPatch(expected_version=1, content="right"), b)
    shutil.copyfile(clone_root / "sessions" / f"{b}.md", store.root / "sessions" / f"{b}.md")
    with pytest.raises(HTTPException) as error:
        store.get(memory["id"])
    assert error.value.detail["code"] == "concurrent_revision"


def test_arbitrary_markdown_roundtrip_and_corrupt_file_rejected(store: MarkdownStore) -> None:
    session = start(store)
    text = '# Título\n\n```json\n{"a": "<!-- -->"}\n```\n\n---\n😺\n<!-- llm-memory-event: {} -->'
    memory = remember(store, text)
    path = store.root / "sessions" / f"{session}.md"
    assert render(parse(path.read_text(), path)) == path.read_text()
    assert MarkdownStore(store.root).get(memory["id"])["content"] == text
    original = path.read_text()
    path.write_text(original + "<<<<<<< merge conflict\n")
    with pytest.raises(ValueError, match="Invalid session Markdown"):
        remember(store)
    assert path.read_text() == original + "<<<<<<< merge conflict\n"


def test_wrong_scope_and_external_session_reuse(store: MarkdownStore) -> None:
    data = AgentSessionCreate(agent_id="codex", project_id="memory", external_id="external")
    session = store.start(data)
    assert store.start(data)["id"] == session["id"]
    with pytest.raises(HTTPException):
        store.create(MemoryCreate(content="wrong", project_id="other", session_id=session["id"]))
    with pytest.raises(HTTPException):
        store.start(data.model_copy(update={"agent_id": "other"}))
    store.handoff(session["id"], HandoffCreate(summary="done"))
    with pytest.raises(HTTPException):
        store.start(data)


def test_compaction_is_atomic_and_deletion_hides_derivatives(store: MarkdownStore) -> None:
    start(store)
    a, b = remember(store, "first"), remember(store, "second")
    proposal = store.preview(
        CompactionPreviewRequest(memory_ids=[a["id"], b["id"]], proposed_content="summary")
    )
    summary = store.compact(proposal["id"])
    assert store.get(a["id"])["status"] == "archived"
    with pytest.raises(HTTPException):
        store.compact(proposal["id"])
    store.delete(a["id"])
    with pytest.raises(HTTPException):
        store.get(summary["id"])
    # Deletion is intentionally a journal tombstone, not a promise of erasure.
    assert any("first" in path.read_text() for path in (store.root / "sessions").glob("*.md"))


def test_stale_compaction_does_not_change_sources(store: MarkdownStore) -> None:
    start(store)
    a, b = remember(store, "first"), remember(store, "second")
    proposal = store.preview(
        CompactionPreviewRequest(memory_ids=[a["id"], b["id"]], proposed_content="summary")
    )
    store.update(a["id"], MemoryPatch(expected_version=1, content="changed"))
    with pytest.raises(HTTPException):
        store.compact(proposal["id"])
    assert store.get(b["id"])["status"] == "active"


def test_policy_rejection_does_not_create_implicit_file(store: MarkdownStore) -> None:
    limited = MarkdownStore(store.root, replace(store.settings, max_bytes_per_owner=3))
    with pytest.raises(HTTPException):
        remember(limited, "too large")
    assert not list((store.root / "sessions").glob("*.md"))


def test_atomic_write_failure_leaves_previous_file(
    store: MarkdownStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = start(store)
    memory = remember(store)
    path = store.root / "sessions" / f"{session}.md"
    original = path.read_bytes()

    def broken_replace(*args: object) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        store.update(memory["id"], MemoryPatch(expected_version=1, content="new"))
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".session-*"))


def test_commit_does_not_include_other_sessions_or_staged_files(store: MarkdownStore) -> None:
    session = start(store)
    remember(store)
    second = start(store)
    remember(store, "unfinished work")
    prefix = ["git", "-C", str(store.root)]
    subprocess.run([*prefix, "config", "user.name", "Test"], check=True)
    subprocess.run([*prefix, "config", "user.email", "test@example.com"], check=True)
    (store.root / "unrelated.txt").write_text("unrelated")
    subprocess.run([*prefix, "add", "unrelated.txt"], check=True)
    store.commit(session, "save session")
    files = subprocess.check_output([*prefix, "ls-tree", "-r", "--name-only", "HEAD"], text=True)
    assert files.splitlines() == [f"sessions/{session}.md"]
    assert (store.root / "sessions" / f"{second}.md").exists()
    staged = subprocess.check_output([*prefix, "diff", "--cached", "--name-only"], text=True)
    assert "unrelated.txt" in staged


def test_api_session_flow(store: MarkdownStore) -> None:
    from llm_memory.markdown_api import app

    with TestClient(app) as client:
        session = client.post("/v1/sessions", json={"agent_id": "codex", "project_id": "memory"})
        assert session.status_code == 201
        memory = client.post(
            "/v1/memories", json={"content": "teste API", "session_id": session.json()["id"]}
        )
        assert memory.status_code == 201
        memory_id = memory.json()["memory"]["id"]
        response = client.post("/v1/search", json={"query": "teste", "project_id": "memory"})
        assert response.json()["results"][0]["memory"]["id"] == memory_id
        assert (
            client.patch(
                f"/v1/memories/{memory_id}", json={"content": "novo", "expected_version": 7}
            ).status_code
            == 409
        )
        assert client.get(f"/v1/memories/{memory_id}/history").status_code == 200
        assert client.post("/v1/search", json={"query": ""}).status_code == 422


def test_cli_configuration_and_no_database_import(store: MarkdownStore) -> None:
    result = CliRunner().invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    assert str(store.root) in CliRunner().invoke(cli, ["policy"]).output
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from llm_memory.cli import app; from llm_memory.api import app; "
                "from llm_memory.mcp_server import build_server; build_server(); "
                "import sys; assert 'llm_memory.db' not in sys.modules; "
                "assert 'sqlalchemy' not in sys.modules"
            ),
        ],
        check=True,
        env=os.environ.copy(),
    )


def test_config_relative_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "memory.toml"
    config.write_text('[storage]\nbackend="markdown"\nrepository="sessions-repo"\n')
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    monkeypatch.delenv("MEMORY_REPOSITORY", raising=False)
    assert get_settings().repository == tmp_path / "sessions-repo"
    monkeypatch.setenv("MEMORY_REPOSITORY", str(tmp_path / "override"))
    assert get_settings().repository == tmp_path / "override"


def test_database_migration_preserves_sessions_versions_and_handoffs(
    store: MarkdownStore, session_factory
) -> None:
    from llm_memory.continuity import create_handoff, create_session
    from llm_memory.service import create_memory, update_memory

    with session_factory() as session:
        agent_session = create_session(
            session, "local", AgentSessionCreate(agent_id="codex", project_id="memory")
        )
        memory, _ = create_memory(
            session, MemoryCreate(content="old", session_id=agent_session.id), "local"
        )
        update_memory(session, memory, MemoryPatch(expected_version=1, content="new"), "local")
        create_handoff(session, "local", agent_session.id, HandoffCreate(summary="done"))
        url = str(session.get_bind().url)
        source_path = Path(session.get_bind().url.database)
        memory_id, session_id = memory.id, agent_session.id
    original = source_path.read_bytes()
    counts = migrate_database(store, url)
    assert counts == {"sessions": 1, "memories": 1, "handoffs": 1}
    assert store.get(memory_id)["session_id"] == session_id
    assert [h["content"] for h in store.history(memory_id)] == ["old", "new"]
    assert source_path.read_bytes() == original
    before = {p.name: p.read_bytes() for p in (store.root / "sessions").glob("*.md")}
    migrate_database(store, url)
    assert before == {p.name: p.read_bytes() for p in (store.root / "sessions").glob("*.md")}


def test_mcp_tools_use_current_session_and_survive_restart(store: MarkdownStore) -> None:
    import asyncio

    from llm_memory.markdown_mcp import build_server

    async def scenario() -> None:
        server = build_server()
        created = await server.call_tool(
            "memory_session_start", {"agent_id": "codex", "project_id": "memory"}
        )
        assert not created.is_error
        result = await server.call_tool(
            "memory_create", {"content": "MCP português", "project_id": "memory"}
        )
        assert not result.is_error
        restarted = build_server()
        result = await restarted.call_tool(
            "memory_search", {"query": "português", "project_id": "memory"}
        )
        assert not result.is_error
        assert "MCP português" in str(result)

    asyncio.run(scenario())


def test_capture_persists_each_turn_and_deduplicates_retries(store: MarkdownStore) -> None:
    from llm_memory.capture import capture_event

    base = {"session_id": "conversation-1", "cwd": str(store.root), "turn_id": "turn-1"}
    capture_event({**base, "hook_event_name": "SessionStart"})
    prompt = {**base, "hook_event_name": "UserPromptSubmit", "prompt": "Olá"}
    capture_event(prompt)
    capture_event(prompt)
    capture_event({**base, "hook_event_name": "Stop", "last_assistant_message": "Oi"})
    assert len(store.list_memories()) == 2
    capture_event({**base, "hook_event_name": "SessionEnd"})
    capture_event({**base, "hook_event_name": "SessionEnd"})
    with store.transaction() as state:
        assert len(state.documents) == 1
        assert len(state.handoffs) == 1
        assert "Olá" in next(iter(state.handoffs.values()))["summary"]
