from __future__ import annotations

import json
from typing import cast

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from llm_memory.config import get_settings
from llm_memory.db import SessionLocal, initialize_database
from llm_memory.schemas import Confidence, MemoryCreate, MemoryPatch, SourceInput
from llm_memory.service import (
    DEFAULT_OWNER,
    apply_compaction,
    create_memory,
    delete_memory,
    memory_dict,
    preview_compaction,
    require_memory,
    resolve_conflict,
    search_memories,
    update_memory,
)


def _jsonable(value: object) -> object:
    return json.loads(json.dumps(value, default=lambda item: item.isoformat() if hasattr(item, "isoformat") else str(item)))


def build_server() -> MCPServer:
    server = MCPServer("LLM Memory")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def memory_search(query: str, limit: int = 5, scope_id: str | None = None) -> list[dict[str, object]]:
        """Search authorized local memories and return source and confidence metadata."""
        with SessionLocal() as session:
            rows = search_memories(session, DEFAULT_OWNER, query, scope_id, max(1, min(limit, 50)))
            return [cast(dict[str, object], _jsonable({
                "memory": memory_dict(memory), "score": score, "reason": reason,
            }))
                    for memory, score, reason in rows]

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def memory_get(memory_id: str) -> dict[str, object]:
        """Inspect one local memory, including provenance and version."""
        with SessionLocal() as session:
            return cast(
                dict[str, object],
                _jsonable(memory_dict(require_memory(session, memory_id, DEFAULT_OWNER))),
            )

    @server.tool()
    def memory_create(content: str, kind: str = "fact", confidence: Confidence = "unverified",
                      tags: list[str] | None = None, source_type: str = "mcp") -> dict[str, object]:
        """Save a memory for the local owner."""
        data = MemoryCreate(
            content=content,
            kind=kind,
            confidence=confidence,
            tags=tags or [],
            source=SourceInput(type=source_type),
        )
        with SessionLocal() as session:
            memory, warnings = create_memory(session, data, DEFAULT_OWNER)
            return cast(
                dict[str, object],
                _jsonable({"memory": memory_dict(memory), "warnings": warnings}),
            )

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def memory_delete(memory_id: str, confirm: bool) -> dict[str, str]:
        """Delete a memory and its derived summaries. This cannot be undone."""
        if not confirm:
            raise ValueError("Set confirm=true only after reviewing the memory to delete")
        with SessionLocal() as session:
            memory = require_memory(session, memory_id, DEFAULT_OWNER)
            if memory.owner_id != DEFAULT_OWNER:
                raise ValueError("Only the owner can delete memory")
            delete_memory(session, memory)
        return {"status": "deleted", "memory_id": memory_id}

    @server.tool()
    def memory_update(
        memory_id: str,
        expected_version: int,
        content: str | None = None,
        kind: str | None = None,
        confidence: Confidence | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, object]:
        """Update a local memory with optimistic version check."""
        from sqlalchemy import select as _select

        from llm_memory.models import Memory as _Memory

        patch = MemoryPatch(
            content=content,
            kind=kind,
            confidence=confidence,
            tags=tags,
            expected_version=expected_version,
        )
        with SessionLocal() as session:
            memory = require_memory(session, memory_id, DEFAULT_OWNER, edit=True)
            if memory.owner_id != DEFAULT_OWNER:
                raise ValueError("Only the owner can update shared memories")
            updated = update_memory(session, memory, patch, DEFAULT_OWNER)
            _ = session.scalar(_select(_Memory.id).where(_Memory.id == updated.id))
            return cast(dict[str, object], _jsonable(memory_dict(updated)))

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def memory_history(memory_id: str) -> list[dict[str, object]]:
        """List version history for a local memory without content leakage beyond scope."""
        from sqlalchemy import select as _select

        from llm_memory.models import MemoryVersion as _MemoryVersion

        with SessionLocal() as session:
            memory = require_memory(session, memory_id, DEFAULT_OWNER)
            rows = session.scalars(
                _select(_MemoryVersion)
                .where(_MemoryVersion.memory_id == memory.id)
                .order_by(_MemoryVersion.version)
            ).all()
            entries = [
                {
                    "version": row.version,
                    "content": row.content,
                    "changed_by": row.changed_by,
                    "created_at": row.created_at.isoformat()
                    if hasattr(row.created_at, "isoformat")
                    else str(row.created_at),
                }
                for row in rows
            ]
            entries.append(
                {
                    "version": memory.version,
                    "content": memory.content,
                    "changed_by": memory.owner_id,
                    "created_at": memory.updated_at.isoformat()
                    if hasattr(memory.updated_at, "isoformat")
                    else str(memory.updated_at),
                }
            )
            return cast(list[dict[str, object]], _jsonable(entries))

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def memory_compaction_preview(
        memory_ids: list[str], proposed_content: str
    ) -> dict[str, object]:
        """Preview a compaction: sources stay active until apply is confirmed."""
        with SessionLocal() as session:
            proposal = preview_compaction(session, DEFAULT_OWNER, memory_ids, proposed_content)
            ratio = (
                0.0
                if proposal.estimated_bytes_before == 0
                else 1.0 - proposal.estimated_bytes_after / proposal.estimated_bytes_before
            )
            return cast(
                dict[str, object],
                _jsonable(
                    {
                        "id": proposal.id,
                        "memory_ids": memory_ids,
                        "proposed_content": proposal.proposed_content,
                        "estimated_bytes_before": proposal.estimated_bytes_before,
                        "estimated_bytes_after": proposal.estimated_bytes_after,
                        "reduction_ratio": round(ratio, 4),
                        "status": proposal.status,
                    }
                ),
            )

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def memory_compaction_apply(proposal_id: str, confirm: bool) -> dict[str, object]:
        """Apply a reviewed compaction proposal. Archives sources. Requires confirm=true."""
        if not confirm:
            raise ValueError("Set confirm=true only after reviewing the proposal and sources")
        with SessionLocal() as session:
            derived = apply_compaction(session, DEFAULT_OWNER, proposal_id)
            return cast(dict[str, object], _jsonable(memory_dict(derived)))

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def memory_share(memory_id: str, recipient_id: str, role: str, confirm: bool) -> dict[str, object]:
        """Share a memory scope with recipient as reader/editor. Requires confirm=true."""
        import uuid as _uuid

        from llm_memory.models import Grant as _Grant

        if role not in {"reader", "editor"}:
            raise ValueError("role must be reader or editor")
        if not confirm:
            raise ValueError("Set confirm=true only after reviewing what is shared")
        if not recipient_id.strip():
            raise ValueError("recipient_id cannot be empty")
        with SessionLocal() as session:
            memory = require_memory(session, memory_id, DEFAULT_OWNER)
            if memory.owner_id != DEFAULT_OWNER:
                raise ValueError("Only the owner can share memory")
            grant = _Grant(
                id=str(_uuid.uuid4()),
                scope_id=memory.scope_id,
                owner_id=DEFAULT_OWNER,
                recipient_id=recipient_id.strip(),
                role=role,
            )
            session.add(grant)
            session.commit()
            return cast(
                dict[str, object],
                _jsonable(
                    {
                        "id": grant.id,
                        "scope_id": grant.scope_id,
                        "recipient_id": grant.recipient_id,
                        "role": grant.role,
                        "revoked": grant.revoked,
                    }
                ),
            )

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def memory_resolve_conflict(
        memory_id: str, related_memory_id: str, decision: str, confirm: bool
    ) -> dict[str, object]:
        """Resolve a conflict: keep_memory, keep_related or keep_both. May delete. Requires confirm."""
        if decision not in {"keep_memory", "keep_related", "keep_both"}:
            raise ValueError("decision must be keep_memory, keep_related or keep_both")
        if not confirm:
            raise ValueError("Set confirm=true only after reviewing both memories")
        with SessionLocal() as session:
            resolved = resolve_conflict(
                session, DEFAULT_OWNER, memory_id, related_memory_id, decision
            )
            return cast(dict[str, object], _jsonable(memory_dict(resolved)))

    return server


def run() -> None:
    initialize_database()
    if get_settings().auth_required:
        raise RuntimeError("Local stdio MCP does not accept remote auth tokens; disable auth for this local profile")
    build_server().run()
