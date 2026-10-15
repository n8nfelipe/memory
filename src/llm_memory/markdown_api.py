from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from llm_memory.markdown_store import MarkdownStore, fail
from llm_memory.schemas import (
    AgentSessionCreate,
    AgentSessionOut,
    CompactionPreviewOut,
    CompactionPreviewRequest,
    ConflictResolutionRequest,
    HandoffClaimRequest,
    HandoffCompleteRequest,
    HandoffCreate,
    HandoffOut,
    HistoryEntry,
    MemoryCreate,
    MemoryCreated,
    MemoryImportRequest,
    MemoryOut,
    MemoryPatch,
    SearchRequest,
    SearchResponse,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    MarkdownStore().initialize()
    yield


app = FastAPI(title="LLM Memory — Markdown", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    detail = (
        exc.detail
        if isinstance(exc.detail, dict)
        else {"code": "http_error", "message": str(exc.detail)}
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            **detail,
            "request_id": request.headers.get("x-request-id") or str(uuid.uuid4()),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, _exc: RequestValidationError) -> JSONResponse:
    return await http_error(
        request,
        HTTPException(422, {"code": "validation_error", "message": "Request validation failed"}),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/sessions", response_model=AgentSessionOut, status_code=201)
def start(data: AgentSessionCreate) -> dict[str, Any]:
    return MarkdownStore().start(data)


@app.post("/v1/memories", response_model=MemoryCreated, status_code=201)
def create(data: MemoryCreate) -> dict[str, Any]:
    return MarkdownStore().create(data)


@app.get("/v1/memories", response_model=list[MemoryOut])
def list_memories(
    scope_id: str | None = None,
    workspace_id: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 100 or offset < 0:
        fail("invalid_pagination", "Use limit 1-100 and offset >= 0", 422)
    items = MarkdownStore().list_memories(
        scope_id=scope_id,
        workspace_id=workspace_id,
        project_id=project_id,
        agent_id=agent_id,
        session_id=session_id,
        status=status,
    )
    return sorted(items, key=lambda m: (m["created_at"], m["id"]), reverse=True)[
        offset : offset + limit
    ]


@app.get("/v1/memories/{memory_id}", response_model=MemoryOut)
def get(memory_id: str) -> dict[str, Any]:
    return MarkdownStore().get(memory_id)


@app.patch("/v1/memories/{memory_id}", response_model=MemoryOut)
def update(memory_id: str, data: MemoryPatch, session_id: str | None = None) -> dict[str, Any]:
    return MarkdownStore().update(memory_id, data, session_id)


@app.delete("/v1/memories/{memory_id}", status_code=204)
def delete(memory_id: str, session_id: str | None = None) -> Response:
    MarkdownStore().delete(memory_id, session_id)
    return Response(status_code=204)


@app.get("/v1/memories/{memory_id}/history", response_model=list[HistoryEntry])
def history(memory_id: str) -> list[dict[str, Any]]:
    return MarkdownStore().history(memory_id)


@app.post("/v1/search", response_model=SearchResponse)
def search(data: SearchRequest) -> dict[str, Any]:
    return {"results": MarkdownStore().search(data)}


@app.post("/v1/sessions/{session_id}/handoff", response_model=HandoffOut, status_code=201)
def handoff(session_id: str, data: HandoffCreate) -> dict[str, Any]:
    return MarkdownStore().handoff(session_id, data)


@app.post("/v1/handoffs/claim-next", response_model=HandoffOut)
def claim(data: HandoffClaimRequest, session_id: str | None = None) -> dict[str, Any]:
    return MarkdownStore().claim(data.workspace_id, data.project_id, data.agent_id, session_id)


@app.post("/v1/handoffs/{handoff_id}/complete", response_model=HandoffOut)
def complete(
    handoff_id: str, data: HandoffCompleteRequest, session_id: str | None = None
) -> dict[str, Any]:
    return MarkdownStore().complete(handoff_id, data.agent_id, session_id)


@app.post("/v1/compactions/preview", response_model=CompactionPreviewOut, status_code=201)
def preview(data: CompactionPreviewRequest) -> dict[str, Any]:
    return MarkdownStore().preview(data)


@app.post("/v1/compactions/{proposal_id}/apply", response_model=MemoryOut)
def compact(proposal_id: str, session_id: str | None = None) -> dict[str, Any]:
    return MarkdownStore().compact(proposal_id, session_id)


@app.post("/v1/memories/{memory_id}/resolve-conflict", response_model=MemoryOut)
def resolve(
    memory_id: str, data: ConflictResolutionRequest, session_id: str | None = None
) -> dict[str, Any]:
    return MarkdownStore().resolve(memory_id, data.related_memory_id, data.decision, session_id)


@app.get("/v1/export")
def export() -> dict[str, Any]:
    return MarkdownStore().export()


@app.post("/v1/import", status_code=201)
def import_data(data: MemoryImportRequest) -> dict[str, Any]:
    return MarkdownStore().import_data(data)


@app.get("/v1/metrics")
def metrics() -> dict[str, Any]:
    with MarkdownStore().transaction() as state:
        items = [m for m in state.memories.values() if m["status"] != "deleted"]
        return {
            "owner_id": "local",
            "total_memories": len(items),
            "total_bytes": sum(len(m["content"].encode()) for m in items),
            "by_status": dict(Counter(m["status"] for m in items)),
            "active_grants": 0,
            "pending_compactions": sum(p["status"] == "pending" for p in state.proposals.values()),
        }
