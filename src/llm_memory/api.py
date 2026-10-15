from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from llm_memory.config import get_settings
from llm_memory.db import SessionLocal, get_session, initialize_database
from llm_memory.models import Grant, Memory, MemoryRelation, MemoryVersion
from llm_memory.schemas import (
    CompactionPreviewOut,
    CompactionPreviewRequest,
    ConflictResolutionRequest,
    GrantCreate,
    GrantOut,
    HistoryEntry,
    MemoryCreate,
    MemoryCreated,
    MemoryImportRequest,
    MemoryOut,
    MemoryPatch,
    SearchRequest,
    SearchResponse,
)
from llm_memory.service import (
    DEFAULT_OWNER,
    apply_compaction,
    can_read,
    create_memory,
    delete_memory,
    memory_dict,
    owner_for_token,
    preview_compaction,
    purge_expired,
    require_memory,
    resolve_conflict,
    search_memories,
    update_memory,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    initialize_database()
    yield


app = FastAPI(
    title="LLM Memory",
    version="0.1.0",
    description="Local-first, inspectable memory service for LLM applications",
    lifespan=lifespan,
)
_last_purge = 0.0
_purge_lock = threading.Lock()


def _request_id(request: Request) -> str:
    if not getattr(request.state, "request_id", None):
        request.state.request_id = str(uuid.uuid4())
    return str(request.state.request_id)


@app.middleware("http")
async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    global _last_purge
    now = time.monotonic()
    if now - _last_purge >= 60 and _purge_lock.acquire(blocking=False):
        try:
            with SessionLocal() as session:
                purge_expired(session)
            _last_purge = now
        finally:
            _purge_lock.release()
    request.state.request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    response = await call_next(request)
    response.headers["x-request-id"] = request.state.request_id
    return response


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "http_error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={
        "code": detail.get("code", "http_error"),
        "message": detail.get("message", "Request failed"),
        "request_id": _request_id(request),
    }, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={
        "code": "validation_error",
        "message": "Request validation failed",
        "request_id": _request_id(request),
    })


SessionDep = Annotated[Session, Depends(get_session)]


def get_principal(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    settings = get_settings()
    if not settings.auth_required:
        return DEFAULT_OWNER
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Bearer token required"},
                            headers={"WWW-Authenticate": "Bearer"})
    owner = owner_for_token(session, authorization[7:].strip())
    if owner is None:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Invalid or revoked token"},
                            headers={"WWW-Authenticate": "Bearer"})
    return owner


PrincipalDep = Annotated[str, Depends(get_principal)]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/memories", response_model=MemoryCreated, status_code=201)
def create_memory_route(data: MemoryCreate, session: SessionDep, owner: PrincipalDep) -> dict[str, object]:
    memory, warnings = create_memory(session, data, owner)
    return {"memory": memory_dict(memory), "warnings": warnings}


@app.get("/v1/memories", response_model=list[MemoryOut])
def list_memories(
    session: SessionDep,
    owner: PrincipalDep,
    scope_id: str | None = None,
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, object]]:
    if not 1 <= limit <= 100 or offset < 0:
        raise HTTPException(status_code=422, detail={"code": "invalid_pagination", "message": "Use limit 1-100 and offset >= 0"})
    now = datetime.now(UTC)
    stmt = select(Memory).where(Memory.status == status).order_by(Memory.created_at.desc(), Memory.id)
    if scope_id:
        stmt = stmt.where(Memory.scope_id == scope_id)
    memories = [m for m in session.scalars(stmt).all()
                if can_read(session, m, owner)
                and (m.expires_at is None or (m.expires_at.replace(tzinfo=UTC)
                                               if m.expires_at.tzinfo is None else m.expires_at) > now)]
    return [memory_dict(m) for m in memories[offset:offset + limit]]


@app.get("/v1/memories/{memory_id}", response_model=MemoryOut)
def get_memory(memory_id: str, session: SessionDep, owner: PrincipalDep) -> dict[str, object]:
    return memory_dict(require_memory(session, memory_id, owner))


@app.patch("/v1/memories/{memory_id}", response_model=MemoryOut)
def patch_memory(memory_id: str, data: MemoryPatch, session: SessionDep, owner: PrincipalDep) -> dict[str, object]:
    memory = require_memory(session, memory_id, owner, edit=True)
    return memory_dict(update_memory(session, memory, data, owner))


@app.delete("/v1/memories/{memory_id}", status_code=204)
def remove_memory(memory_id: str, session: SessionDep, owner: PrincipalDep) -> Response:
    memory = require_memory(session, memory_id, owner)
    if memory.owner_id != owner:
        raise HTTPException(status_code=403, detail={"code": "owner_required", "message": "Only the owner can delete memory"})
    delete_memory(session, memory)
    return Response(status_code=204)


@app.post("/v1/search", response_model=SearchResponse)
def search(data: SearchRequest, session: SessionDep, owner: PrincipalDep) -> dict[str, object]:
    rows = search_memories(session, owner, data.query, data.scope_id, data.limit, data.include_archived)
    return {"results": [{"memory": memory_dict(memory), "score": round(score, 6), "reason": reason}
                         for memory, score, reason in rows]}


@app.get("/v1/memories/{memory_id}/history", response_model=list[HistoryEntry])
def history(memory_id: str, session: SessionDep, owner: PrincipalDep) -> list[dict[str, object]]:
    memory = require_memory(session, memory_id, owner)
    entries = session.scalars(select(MemoryVersion).where(MemoryVersion.memory_id == memory.id)
                              .order_by(MemoryVersion.version)).all()
    output = [{"version": row.version, "content": row.content, "changed_by": row.changed_by,
               "created_at": row.created_at} for row in entries]
    output.append({"version": memory.version, "content": memory.content, "changed_by": memory.owner_id,
                   "created_at": memory.updated_at})
    return output


@app.post("/v1/memories/{memory_id}/resolve-conflict", response_model=MemoryOut)
def resolve_memory_conflict(memory_id: str, data: ConflictResolutionRequest,
                           session: SessionDep, owner: PrincipalDep) -> dict[str, object]:
    memory = resolve_conflict(session, owner, memory_id, data.related_memory_id, data.decision)
    return memory_dict(memory)


@app.post("/v1/memories/{memory_id}/grants", response_model=GrantOut, status_code=201)
def grant_access(memory_id: str, data: GrantCreate, session: SessionDep, owner: PrincipalDep) -> dict[str, object]:
    memory = require_memory(session, memory_id, owner)
    if memory.owner_id != owner:
        raise HTTPException(status_code=403, detail={"code": "owner_required", "message": "Only the owner can share memory"})
    grant = Grant(id=str(uuid.uuid4()), scope_id=memory.scope_id, owner_id=owner,
                  recipient_id=data.recipient_id, role=data.role, expires_at=data.expires_at)
    session.add(grant)
    session.commit()
    return {"id": grant.id, "scope_id": grant.scope_id, "recipient_id": grant.recipient_id,
            "role": grant.role, "expires_at": grant.expires_at, "revoked": grant.revoked}


@app.delete("/v1/grants/{grant_id}", status_code=204)
def revoke_grant(grant_id: str, session: SessionDep, owner: PrincipalDep) -> Response:
    grant = session.get(Grant, grant_id)
    if grant is None or grant.owner_id != owner:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Grant not found"})
    grant.revoked = True
    session.commit()
    return Response(status_code=204)


@app.post("/v1/compactions/preview", response_model=CompactionPreviewOut, status_code=201)
def compaction_preview(data: CompactionPreviewRequest, session: SessionDep, owner: PrincipalDep) -> dict[str, object]:
    proposal = preview_compaction(session, owner, data.memory_ids, data.proposed_content)
    ratio = 0.0 if proposal.estimated_bytes_before == 0 else (
        1.0 - proposal.estimated_bytes_after / proposal.estimated_bytes_before
    )
    return {"id": proposal.id, "memory_ids": json.loads(proposal.source_ids),
            "proposed_content": proposal.proposed_content,
            "estimated_bytes_before": proposal.estimated_bytes_before,
            "estimated_bytes_after": proposal.estimated_bytes_after,
            "reduction_ratio": round(ratio, 4), "status": proposal.status}


@app.post("/v1/compactions/{proposal_id}/apply", response_model=MemoryOut)
def compaction_apply(proposal_id: str, session: SessionDep, owner: PrincipalDep) -> dict[str, object]:
    return memory_dict(apply_compaction(session, owner, proposal_id))


@app.get("/v1/export")
def export_memories(session: SessionDep, owner: PrincipalDep) -> dict[str, object]:
    memories = session.scalars(select(Memory).where(Memory.owner_id == owner).order_by(Memory.id)).all()
    memory_ids = {memory.id for memory in memories}
    relations = session.scalars(select(MemoryRelation).where(
        MemoryRelation.source_id.in_(memory_ids), MemoryRelation.derived_id.in_(memory_ids)
    )).all() if memory_ids else []
    return {
        "schema_version": "1",
        "memories": [memory_dict(memory) for memory in memories],
        "relations": [{"source_id": relation.source_id, "derived_id": relation.derived_id,
                       "relation": relation.relation} for relation in relations],
    }


@app.post("/v1/import", status_code=201)
def import_memories(data: MemoryImportRequest, session: SessionDep, owner: PrincipalDep) -> dict[str, int]:
    incoming_ids = {item.id for item in data.memories}
    if len(incoming_ids) != len(data.memories):
        raise HTTPException(status_code=422, detail={"code": "duplicate_ids", "message": "Import contains duplicate IDs"})
    existing = session.scalars(select(Memory.id).where(Memory.id.in_(incoming_ids))).all() if incoming_ids else []
    if existing:
        raise HTTPException(status_code=409, detail={"code": "already_exists", "message": "One or more memory IDs already exist"})
    settings = get_settings()
    if settings.allowed_kinds and any(item.kind not in settings.allowed_kinds for item in data.memories):
        raise HTTPException(status_code=422, detail={"code": "kind_not_allowed", "message": "Import contains a memory kind blocked by policy"})
    owner_count = session.scalar(select(func.count()).select_from(Memory).where(Memory.owner_id == owner)) or 0
    owner_memories = session.scalars(select(Memory.content).where(Memory.owner_id == owner)).all()
    incoming_bytes = sum(len(item.content.encode("utf-8")) for item in data.memories)
    current_bytes = sum(len(content.encode("utf-8")) for content in owner_memories)
    if settings.max_entries_per_owner and owner_count + len(data.memories) > settings.max_entries_per_owner:
        raise HTTPException(status_code=413, detail={"code": "memory_limit_exceeded", "message": "Owner memory count limit reached"})
    if settings.max_bytes_per_owner and current_bytes + incoming_bytes > settings.max_bytes_per_owner:
        raise HTTPException(status_code=413, detail={"code": "memory_limit_exceeded", "message": "Owner memory size limit reached"})
    from llm_memory.service import _index_memory

    for item in data.memories:
        scope_id = f"private:{owner}" if item.scope_id.startswith("private:") else item.scope_id
        memory = Memory(
            id=item.id, owner_id=owner, scope_id=scope_id, content=item.content,
            kind=item.kind, source_type=item.source.type, source_ref=item.source.ref,
            confidence=item.confidence, tags=json.dumps(item.tags, ensure_ascii=False),
            status=item.status, expires_at=item.expires_at, created_at=item.created_at,
            updated_at=item.updated_at, version=item.version,
        )
        session.add(memory)
        session.flush()
        _index_memory(session, memory)
    relation_count = 0
    seen_relations: set[tuple[str, str, str]] = set()
    for relation in data.relations:
        if relation.get("source_id") not in incoming_ids or relation.get("derived_id") not in incoming_ids:
            continue
        relation_type = relation.get("relation", "summarized_from")
        if relation_type not in {"summarized_from", "supersedes", "duplicates", "conflicts_with"}:
            raise HTTPException(status_code=422, detail={"code": "invalid_relation", "message": "Unsupported relation type"})
        relation_key = (relation["source_id"], relation["derived_id"], relation_type)
        if relation_key in seen_relations:
            raise HTTPException(status_code=422, detail={"code": "duplicate_relation", "message": "Duplicate relation in import"})
        seen_relations.add(relation_key)
        session.add(MemoryRelation(
            id=str(uuid.uuid4()), source_id=relation["source_id"], derived_id=relation["derived_id"],
            relation=relation_type,
        ))
        relation_count += 1
    session.commit()
    return {"imported": len(data.memories), "relations": relation_count}
