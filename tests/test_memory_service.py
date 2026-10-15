from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from llm_memory.models import ApiToken, Grant, Memory, MemoryRelation
from llm_memory.schemas import MemoryCreate, MemoryPatch
from llm_memory.service import (
    apply_compaction,
    can_edit,
    can_read,
    create_memory,
    create_token,
    delete_memory,
    owner_for_token,
    preview_compaction,
    purge_expired,
    resolve_conflict,
    search_memories,
    update_memory,
)


def new_memory(session: Session, content: str, *, owner: str = "alice", scope: str | None = None) -> Memory:
    data = MemoryCreate(content=content, scope_id=scope, source={"type": "test"}, confidence="asserted")
    memory, _warnings = create_memory(session, data, owner)
    return memory


def test_create_and_search_returns_provenance(session: Session) -> None:
    memory = new_memory(session, "User prefers Brazilian Portuguese.")

    results = search_memories(session, "alice", "Portuguese language", None, 5)

    assert len(results) == 1
    assert results[0][0].id == memory.id
    assert results[0][2] == "FTS5 lexical match"
    assert memory.source_type == "test"
    assert memory.confidence == "asserted"


def test_search_does_not_return_another_owners_private_memory(session: Session) -> None:
    new_memory(session, "Secret project name is Aurora.", owner="alice")
    new_memory(session, "Secret project name is Aurora.", owner="bob")

    results = search_memories(session, "alice", "Aurora", None, 10)

    assert [row[0].owner_id for row in results] == ["alice"]


def test_create_flags_duplicate_and_possible_conflict(session: Session) -> None:
    first = new_memory(session, "The user likes green tea.")
    duplicate, duplicate_warnings = create_memory(
        session, MemoryCreate(content="The user likes green tea."), "alice"
    )
    conflicting, conflict_warnings = create_memory(
        session, MemoryCreate(content="The user dislikes green tea."), "alice"
    )

    assert f"possible_exact_duplicate:{first.id}" in duplicate_warnings
    assert f"possible_conflict_review:{first.id}" in conflict_warnings
    assert first.status == "conflict"
    assert conflicting.status == "conflict"
    assert duplicate.status == "active"


def test_conflict_resolution_can_preserve_both_memories(session: Session) -> None:
    first = new_memory(session, "The user likes green tea.")
    second, _warnings = create_memory(session, MemoryCreate(content="The user dislikes green tea."), "alice")

    resolved = resolve_conflict(session, "alice", second.id, first.id, "keep_both")

    assert resolved.status == "active"
    assert first.status == "active"
    relation = session.scalar(select(MemoryRelation).where(
        MemoryRelation.source_id == second.id,
        MemoryRelation.derived_id == first.id,
        MemoryRelation.relation == "conflicts_with",
    ))
    assert relation is not None


def test_conflict_resolution_can_keep_one_and_remove_other(session: Session) -> None:
    first = new_memory(session, "The user likes green tea.")
    second, _warnings = create_memory(session, MemoryCreate(content="The user dislikes green tea."), "alice")

    kept = resolve_conflict(session, "alice", second.id, first.id, "keep_memory")

    assert kept.id == second.id
    assert kept.status == "active"
    assert session.get(Memory, first.id) is None


def test_grants_scope_access_and_editor_permissions(session: Session) -> None:
    memory = new_memory(session, "Shared planning note", scope="team:alpha")
    session.add(Grant(id="grant-1", owner_id="alice", recipient_id="bob", scope_id="team:alpha", role="reader"))
    session.commit()

    assert can_read(session, memory, "bob")
    assert not can_edit(session, memory, "bob")
    assert not can_read(session, memory, "carol")


def test_expired_grant_is_not_authorized(session: Session) -> None:
    memory = new_memory(session, "Shared planning note", scope="team:alpha")
    session.add(Grant(
        id="expired-grant", owner_id="alice", recipient_id="bob", scope_id="team:alpha",
        role="editor", expires_at=datetime.now(UTC) - timedelta(days=1),
    ))
    session.commit()

    assert not can_read(session, memory, "bob")


def test_update_requires_current_version_and_records_old_content(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as writer:
        memory = new_memory(writer, "Original preference")
        memory_id = memory.id
    first = session_factory()
    second = session_factory()
    try:
        first_memory = first.get(Memory, memory_id)
        second_memory = second.get(Memory, memory_id)
        assert first_memory is not None and second_memory is not None
        update_memory(first, first_memory, MemoryPatch(content="Updated preference", expected_version=1), "alice")

        with pytest.raises(HTTPException) as error:
            update_memory(second, second_memory, MemoryPatch(content="Stale preference", expected_version=1), "alice")

        assert error.value.status_code == 409
        history = first.scalars(select(Memory).where(Memory.id == memory_id)).one()
        assert history.content == "Updated preference"
        assert history.version == 2
    finally:
        first.close()
        second.close()


def test_compaction_archives_sources_and_links_summary(session: Session) -> None:
    first = new_memory(session, "User likes tea.")
    second = new_memory(session, "User prefers green tea in the morning.")
    proposal = preview_compaction(session, "alice", [first.id, second.id], "User prefers morning green tea.")

    summary = apply_compaction(session, "alice", proposal.id)

    assert summary.kind == "summary"
    assert first.status == "archived"
    assert second.status == "archived"
    assert session.scalar(select(MemoryRelation).where(
        MemoryRelation.source_id == first.id,
        MemoryRelation.derived_id == summary.id,
    )) is not None


def test_deleting_a_source_removes_derived_summary(session: Session) -> None:
    source = new_memory(session, "Keep this private fact.")
    other = new_memory(session, "Keep this second private fact.")
    proposal = preview_compaction(session, "alice", [source.id, other.id], "Combined private facts.")
    summary = apply_compaction(session, "alice", proposal.id)

    delete_memory(session, source)

    assert session.get(Memory, source.id) is None
    assert session.get(Memory, summary.id) is None


def test_expiry_purge_removes_expired_memories(session: Session) -> None:
    memory = MemoryCreate(
        content="Temporary note", expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    expired, _warnings = create_memory(session, memory, "alice")

    removed = purge_expired(session)

    assert removed == 1
    assert session.get(Memory, expired.id) is None


def test_tokens_are_stored_as_hashes_and_can_be_revoked(session: Session) -> None:
    token_id, token = create_token(session, "alice", "test token")

    assert owner_for_token(session, token) == "alice"
    record = session.get(ApiToken, token_id)
    assert record is not None
    assert record.token_hash != token
    record.revoked = True
    session.commit()
    assert owner_for_token(session, token) is None
