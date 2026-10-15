from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import bindparam, delete, func, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from llm_memory.config import get_settings
from llm_memory.db import engine
from llm_memory.embeddings import (
    EmbeddingProviderError,
    cosine_similarity,
    embed_texts,
    ensure_embeddings,
    store_embedding,
)
from llm_memory.models import (
    ApiToken,
    CompactionProposal,
    Grant,
    Memory,
    MemoryEmbedding,
    MemoryRelation,
    MemoryVersion,
    utcnow,
)
from llm_memory.schemas import MemoryCreate, MemoryPatch

DEFAULT_OWNER = "local"


def new_id() -> str:
    return str(uuid.uuid4())


def tags_dump(tags: list[str]) -> str:
    return json.dumps(sorted({tag.strip() for tag in tags if tag.strip()}), ensure_ascii=False)


def tags_load(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def memory_dict(memory: Memory) -> dict[str, object]:
    return {
        "id": memory.id,
        "owner_id": memory.owner_id,
        "scope_id": memory.scope_id,
        "workspace_id": memory.workspace_id,
        "project_id": memory.project_id,
        "agent_id": memory.agent_id,
        "session_id": memory.session_id,
        "content": memory.content,
        "kind": memory.kind,
        "source": {"type": memory.source_type or "manual", "ref": memory.source_ref},
        "confidence": memory.confidence,
        "tags": tags_load(memory.tags),
        "status": memory.status,
        "expires_at": memory.expires_at,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "version": memory.version,
    }


def _is_expired(memory: Memory) -> bool:
    if memory.expires_at is None:
        return False
    expiry = memory.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry <= utcnow()


def _visible_scope_ids(session: Session, owner_id: str) -> dict[str, set[str]]:
    grants = session.scalars(
        select(Grant).where(
            Grant.recipient_id == owner_id,
            Grant.revoked.is_(False),
        )
    ).all()
    now = utcnow()
    result: dict[str, set[str]] = {"reader": set(), "editor": set()}
    for grant in grants:
        if grant.expires_at and _aware(grant.expires_at) <= now:
            continue
        result.setdefault(grant.role, set()).add(f"{grant.owner_id}\0{grant.scope_id}")
    return result


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def can_read(session: Session, memory: Memory, owner_id: str) -> bool:
    if memory.owner_id == owner_id:
        return True
    visible = _visible_scope_ids(session, owner_id)
    key = f"{memory.owner_id}\0{memory.scope_id}"
    return any(key in scopes for scopes in visible.values())


def can_edit(session: Session, memory: Memory, owner_id: str) -> bool:
    if memory.owner_id == owner_id:
        return True
    visible = _visible_scope_ids(session, owner_id)
    return f"{memory.owner_id}\0{memory.scope_id}" in visible.get("editor", set())


def require_memory(session: Session, memory_id: str, owner_id: str, *, edit: bool = False) -> Memory:
    memory = session.get(Memory, memory_id)
    if memory is None or not can_read(session, memory, owner_id):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Memory not found"})
    if _is_expired(memory):
        delete_memory(session, memory)
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Memory expired"})
    if edit and not can_edit(session, memory, owner_id):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "Edit permission required"})
    return memory


def _index_memory(session: Session, memory: Memory) -> None:
    if engine.dialect.name != "sqlite":
        return
    session.execute(
        text("DELETE FROM memory_fts WHERE memory_id = :id"), {"id": memory.id}
    )
    session.execute(
        text(
            "INSERT INTO memory_fts(memory_id, owner_id, scope_id, content, tags) "
            "VALUES (:id, :owner, :scope, :content, :tags)"
        ),
        {
            "id": memory.id,
            "owner": memory.owner_id,
            "scope": memory.scope_id,
            "content": memory.content,
            "tags": " ".join(tags_load(memory.tags)),
        },
    )


def _remove_index(session: Session, memory_id: str) -> None:
    if engine.dialect.name == "sqlite":
        session.execute(text("DELETE FROM memory_fts WHERE memory_id = :id"), {"id": memory_id})


def create_memory(session: Session, data: MemoryCreate, owner_id: str) -> tuple[Memory, list[str]]:
    scope_id = data.scope_id or f"private:{owner_id}"
    settings = get_settings()
    context = {
        "workspace_id": data.workspace_id,
        "project_id": data.project_id,
        "agent_id": data.agent_id,
        "session_id": data.session_id,
    }
    if data.session_id:
        from llm_memory.models import AgentSession

        active_session = session.scalar(select(AgentSession).where(
            AgentSession.id == data.session_id,
            AgentSession.owner_id == owner_id,
            AgentSession.status == "active",
        ))
        if active_session is None:
            raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": "Active session not found"})
        expected = {
            "workspace_id": active_session.workspace_id,
            "project_id": active_session.project_id,
            "agent_id": active_session.agent_id,
        }
        if any(context[key] not in (None, expected[key]) for key in expected):
            raise HTTPException(status_code=422, detail={"code": "session_scope_mismatch", "message": "Memory scope must match its session"})
        context.update(expected)
    if settings.allowed_kinds and data.kind not in settings.allowed_kinds:
        raise HTTPException(status_code=422, detail={"code": "kind_not_allowed", "message": "Memory kind is not allowed by policy"})
    existing_count = session.scalar(select(func.count()).select_from(Memory).where(Memory.owner_id == owner_id)) or 0
    existing_bytes = sum(
        len(value.encode("utf-8")) for value in session.scalars(
            select(Memory.content).where(Memory.owner_id == owner_id)
        ).all()
    )
    content_bytes = len(data.content.encode("utf-8"))
    if settings.max_entries_per_owner and existing_count >= settings.max_entries_per_owner:
        raise HTTPException(status_code=413, detail={"code": "memory_limit_exceeded", "message": "Owner memory count limit reached"})
    if settings.max_bytes_per_owner and existing_bytes + content_bytes > settings.max_bytes_per_owner:
        raise HTTPException(status_code=413, detail={"code": "memory_limit_exceeded", "message": "Owner memory size limit reached"})
    normalized = re.sub(r"\s+", " ", data.content).strip().casefold()
    existing = session.scalars(
        select(Memory).where(
            Memory.owner_id == owner_id,
            Memory.scope_id == scope_id,
            Memory.status == "active",
        )
    ).all()
    warnings = []
    possible_conflicts: list[Memory] = []
    new_tokens = set(re.findall(r"[\w-]+", normalized))
    opposing_terms = (
        {"like", "dislike"},
        {"likes", "dislikes"},
        {"love", "hate"},
        {"loves", "hates"},
        {"prefer", "avoid"},
        {"prefers", "avoids"},
    )
    compared_content: set[str] = set()
    for item in existing:
        old_normalized = re.sub(r"\s+", " ", item.content).strip().casefold()
        if old_normalized == normalized:
            warnings.append(f"possible_exact_duplicate:{item.id}")
            continue
        if old_normalized in compared_content:
            continue
        compared_content.add(old_normalized)
        old_tokens = set(re.findall(r"[\w-]+", old_normalized))
        overlap = len(new_tokens & old_tokens)
        has_opposing_terms = any(
            len(pair & new_tokens) == 1 and len(pair & old_tokens) == 1
            for pair in opposing_terms
        )
        if (
            item.kind == data.kind
            and has_opposing_terms
            and overlap >= 2
            and overlap / max(len(new_tokens | old_tokens), 1) >= 0.4
        ):
            warnings.append(f"possible_conflict_review:{item.id}")
            possible_conflicts.append(item)
    memory = Memory(
        id=new_id(),
        owner_id=owner_id,
        scope_id=scope_id,
        **context,
        content=data.content,
        kind=data.kind,
        source_type=data.source.type,
        source_ref=data.source.ref,
        confidence=data.confidence,
        tags=tags_dump(data.tags),
        status="conflict" if possible_conflicts else "active",
        expires_at=data.expires_at or (
            utcnow() + timedelta(days=settings.default_ttl_days) if settings.default_ttl_days else None
        ),
    )
    session.add(memory)
    for candidate in possible_conflicts:
        candidate.status = "conflict"
    session.flush()
    _index_memory(session, memory)
    embedding_created = store_embedding(session, memory, settings)
    session.commit()
    session.refresh(memory)
    if settings.embedding_url and not embedding_created:
        warnings.append("embedding_unavailable")
    return memory, warnings


def update_memory(session: Session, memory: Memory, data: MemoryPatch, actor: str) -> Memory:
    if memory.version != data.expected_version:
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "message": "Memory version changed"})
    try:
        changes = data.model_dump(exclude_unset=True, exclude={"expected_version"})
        if "content" in changes and changes["content"] is not None:
            session.add(MemoryVersion(id=new_id(), memory_id=memory.id, version=memory.version,
                                      content=memory.content, changed_by=actor))
            memory.content = str(changes["content"]).strip()
            session.execute(delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == memory.id))
        if "kind" in changes and changes["kind"] is not None:
            memory.kind = str(changes["kind"])
        if "confidence" in changes and changes["confidence"] is not None:
            memory.confidence = str(changes["confidence"])
        if "tags" in changes and changes["tags"] is not None:
            memory.tags = tags_dump(changes["tags"])
        if "expires_at" in changes:
            memory.expires_at = changes["expires_at"]
        memory.version += 1
        memory.updated_at = utcnow()
        _index_memory(session, memory)
        if "content" in changes:
            store_embedding(session, memory, get_settings())
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "message": "Memory changed concurrently"}) from exc
    session.refresh(memory)
    return memory


def search_memories(
    session: Session,
    owner_id: str,
    query: str,
    scope_id: str | None,
    limit: int,
    include_archived: bool = False,
    workspace_id: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> list[tuple[Memory, float, str]]:
    statuses = ["active", "conflict"] + (["archived"] if include_archived else [])
    stmt = select(Memory).where(Memory.status.in_(statuses))
    if scope_id:
        stmt = stmt.where(Memory.scope_id == scope_id)
    if workspace_id:
        stmt = stmt.where(or_(Memory.workspace_id.is_(None), Memory.workspace_id == workspace_id))
    if project_id:
        stmt = stmt.where(or_(Memory.project_id.is_(None), Memory.project_id == project_id))
    if agent_id:
        stmt = stmt.where(or_(Memory.agent_id.is_(None), Memory.agent_id == agent_id))
    if session_id:
        stmt = stmt.where(or_(Memory.session_id.is_(None), Memory.session_id == session_id))
    candidates = [m for m in session.scalars(stmt).all() if can_read(session, m, owner_id) and not _is_expired(m)]
    if not candidates:
        return []
    settings = get_settings()
    semantic_vectors: dict[str, list[float]] = {}
    query_vector: list[float] | None = None
    if settings.embedding_url:
        semantic_vectors = ensure_embeddings(session, candidates, settings)
        if semantic_vectors:
            try:
                query_vector = embed_texts([query], settings)[0]
            except (EmbeddingProviderError, IndexError, ValueError):
                # A configured but unavailable semantic provider must not disable lexical recall.
                query_vector = None
    fts_positions: dict[str, int] = {}
    if engine.dialect.name == "sqlite":
        terms = [term for term in re.findall(r"[\w-]+", query, flags=re.UNICODE) if term]
        if terms:
            match_query = " OR ".join(f'"{term.replace(chr(34), chr(34)*2)}"' for term in terms)
            if not candidates:
                return []
            allowed_ids = [memory.id for memory in candidates]
            statement = text(
                "SELECT memory_id, bm25(memory_fts) AS rank FROM memory_fts "
                "WHERE memory_fts MATCH :query AND memory_id IN :allowed ORDER BY rank"
            ).bindparams(bindparam("allowed", expanding=True))
            ids = session.execute(statement, {"query": match_query, "allowed": allowed_ids}).all()
            fts_positions = {row.memory_id: position for position, row in enumerate(ids)}
            if not query_vector:
                candidates = [m for m in candidates if m.id in fts_positions]
    else:
        tokens = [term for term in re.findall(r"[\w-]+", query.casefold()) if term]
        if not query_vector:
            candidates = [m for m in candidates if any(token in (m.content + " " + m.tags).casefold() for token in tokens)]
    query_tokens = set(re.findall(r"[\w-]+", query.casefold()))
    scored: list[tuple[Memory, float, str]] = []
    lexical_scores: dict[str, float] = {}
    for memory in candidates:
        content_tokens = set(re.findall(r"[\w-]+", (memory.content + " " + memory.tags).casefold()))
        overlap = len(query_tokens & content_tokens)
        overlap_score = overlap / max(len(query_tokens), 1)
        if fts_positions and memory.id in fts_positions:
            # Fuse FTS5's BM25 ordering with token coverage. Rank fusion keeps
            # scores comparable even when the raw BM25 range changes by corpus.
            fts_rank_score = 1.0 / (fts_positions[memory.id] + 1)
            score = 0.7 * fts_rank_score + 0.3 * overlap_score
        else:
            score = overlap_score
        if score:
            lexical_scores[memory.id] = score
    semantic_scores: dict[str, float] = {}
    if query_vector:
        for memory in candidates:
            vector = semantic_vectors.get(memory.id)
            if vector is not None:
                similarity = cosine_similarity(query_vector, vector)
                if similarity > 0.1:
                    semantic_scores[memory.id] = similarity
    if semantic_scores:
        lexical_order = sorted(lexical_scores, key=lambda item: lexical_scores[item], reverse=True)
        semantic_order = sorted(semantic_scores, key=lambda item: semantic_scores[item], reverse=True)
        lexical_ranks = {memory_id: rank for rank, memory_id in enumerate(lexical_order)}
        semantic_ranks = {memory_id: rank for rank, memory_id in enumerate(semantic_order)}
        by_id = {memory.id: memory for memory in candidates}
        for memory_id, memory in by_id.items():
            lexical_component = (
                0.5 * 61 / (60 + lexical_ranks[memory_id] + 1)
                if memory_id in lexical_ranks else 0.0
            )
            semantic_component = (
                0.5 * 61 / (60 + semantic_ranks[memory_id] + 1)
                if memory_id in semantic_ranks else 0.0
            )
            score = lexical_component + semantic_component
            if score:
                reason = "hybrid_lexical_semantic" if memory_id in lexical_ranks else "semantic_similarity"
                scored.append((memory, score, reason))
    else:
        for memory in candidates:
            score = lexical_scores.get(memory.id, 0.0)
            if score:
                reason = "FTS5 lexical match" if fts_positions else "token_overlap"
                scored.append((memory, score, reason))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def create_token(session: Session, owner_id: str, label: str = "") -> tuple[str, str]:
    token = f"lm_{secrets.token_urlsafe(32)}"
    token_id = new_id()
    session.add(ApiToken(id=token_id, owner_id=owner_id,
                         token_hash=hashlib.sha256(token.encode()).hexdigest(), label=label))
    session.commit()
    return token_id, token


def owner_for_token(session: Session, token: str) -> str | None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    record = session.scalar(select(ApiToken).where(ApiToken.token_hash == digest, ApiToken.revoked.is_(False)))
    return record.owner_id if record else None


def delete_memory(session: Session, memory: Memory) -> None:
    pending = [memory.id]
    visited: set[str] = set()
    while pending:
        memory_id = pending.pop()
        if memory_id in visited:
            continue
        visited.add(memory_id)
        pending.extend(session.scalars(select(MemoryRelation.derived_id).where(MemoryRelation.source_id == memory_id)).all())
    for memory_id in visited:
        _remove_index(session, memory_id)
    session.execute(delete(MemoryEmbedding).where(MemoryEmbedding.memory_id.in_(visited)))
    session.execute(delete(MemoryRelation).where(
        (MemoryRelation.source_id.in_(visited)) | (MemoryRelation.derived_id.in_(visited))))
    session.execute(delete(MemoryVersion).where(MemoryVersion.memory_id.in_(visited)))
    session.execute(delete(Memory).where(Memory.id.in_(visited)))
    session.commit()


def purge_expired(session: Session) -> int:
    expired = session.scalars(select(Memory).where(Memory.expires_at.is_not(None))).all()
    targets = [memory for memory in expired if _is_expired(memory)]
    target_ids = [memory.id for memory in targets]
    for memory in targets:
        if session.get(Memory, memory.id) is not None:
            delete_memory(session, memory)
    return len(target_ids)


def resolve_conflict(session: Session, owner_id: str, memory_id: str,
                     related_memory_id: str, decision: str) -> Memory:
    memory = require_memory(session, memory_id, owner_id, edit=True)
    related = require_memory(session, related_memory_id, owner_id, edit=True)
    if memory.owner_id != owner_id or related.owner_id != owner_id:
        raise HTTPException(status_code=403, detail={"code": "owner_required", "message": "Only the owner can resolve this review"})
    if memory.id == related.id or memory.scope_id != related.scope_id:
        raise HTTPException(status_code=422, detail={"code": "invalid_pair", "message": "Memories must be distinct and in the same scope"})
    if decision == "keep_memory":
        memory.status = "active"
        delete_memory(session, related)
        session.commit()
        return memory
    if decision == "keep_related":
        related.status = "active"
        delete_memory(session, memory)
        session.commit()
        return related
    if decision == "keep_both":
        memory.status = "active"
        related.status = "active"
        exists = session.scalar(select(MemoryRelation.id).where(
            MemoryRelation.source_id == memory.id,
            MemoryRelation.derived_id == related.id,
            MemoryRelation.relation == "conflicts_with",
        ))
        if not exists:
            session.add(MemoryRelation(id=new_id(), source_id=memory.id,
                                       derived_id=related.id, relation="conflicts_with"))
        session.commit()
        return memory
    raise HTTPException(status_code=422, detail={"code": "invalid_decision", "message": "Unsupported conflict decision"})


def preview_compaction(session: Session, owner_id: str, memory_ids: list[str], proposed_content: str) -> CompactionProposal:
    if len(set(memory_ids)) != len(memory_ids):
        raise HTTPException(status_code=422, detail={"code": "duplicate_source", "message": "Source IDs must be unique"})
    memories = [require_memory(session, memory_id, owner_id, edit=True) for memory_id in memory_ids]
    if any(m.status != "active" for m in memories):
        raise HTTPException(status_code=409, detail={"code": "invalid_state", "message": "All sources must be active"})
    scopes = {m.scope_id for m in memories}
    if len(scopes) != 1:
        raise HTTPException(status_code=422, detail={"code": "scope_mismatch", "message": "Sources must share a scope"})
    contexts = {
        (m.workspace_id, m.project_id, m.agent_id, m.session_id)
        for m in memories
    }
    if len(contexts) != 1:
        raise HTTPException(status_code=422, detail={
            "code": "context_mismatch", "message": "Sources must share a workspace, project, agent and session"
        })
    proposal = CompactionProposal(
        id=new_id(), owner_id=owner_id, scope_id=memories[0].scope_id,
        source_ids=json.dumps(memory_ids), proposed_content=proposed_content.strip(),
        estimated_bytes_before=sum(len(m.content.encode("utf-8")) for m in memories),
        estimated_bytes_after=len(proposed_content.strip().encode("utf-8")),
    )
    session.add(proposal)
    session.commit()
    return proposal


def apply_compaction(session: Session, owner_id: str, proposal_id: str) -> Memory:
    proposal = session.get(CompactionProposal, proposal_id)
    if proposal is None or proposal.owner_id != owner_id:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Proposal not found"})
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail={"code": "already_applied", "message": "Proposal is not pending"})
    sources = [require_memory(session, item, owner_id, edit=True) for item in json.loads(proposal.source_ids)]
    derived = Memory(
        id=new_id(), owner_id=owner_id, scope_id=proposal.scope_id,
        workspace_id=sources[0].workspace_id, project_id=sources[0].project_id,
        agent_id=sources[0].agent_id, session_id=sources[0].session_id,
        content=proposal.proposed_content, kind="summary", source_type="compaction",
        source_ref=proposal.id, confidence="inferred", tags="[]", status="active",
    )
    session.add(derived)
    session.flush()
    _index_memory(session, derived)
    store_embedding(session, derived, get_settings())
    for source in sources:
        source.status = "archived"
        source.updated_at = utcnow()
        source.version += 1
        session.add(MemoryRelation(id=new_id(), source_id=source.id,
                                   derived_id=derived.id, relation="summarized_from"))
    proposal.status = "applied"
    session.commit()
    session.refresh(derived)
    return derived
