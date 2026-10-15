from __future__ import annotations

import json
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from llm_memory.config import get_settings
from llm_memory.db import SessionLocal, initialize_database
from llm_memory.schemas import MemoryCreate
from llm_memory.service import (
    DEFAULT_OWNER,
    create_memory,
    delete_memory,
    memory_dict,
    require_memory,
    search_memories,
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
            return [_jsonable({"memory": memory_dict(memory), "score": score, "reason": reason})
                    for memory, score, reason in rows]

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def memory_get(memory_id: str) -> dict[str, object]:
        """Inspect one local memory, including provenance and version."""
        with SessionLocal() as session:
            return _jsonable(memory_dict(require_memory(session, memory_id, DEFAULT_OWNER)))

    @server.tool()
    def memory_create(content: str, kind: str = "fact", confidence: str = "unverified",
                      tags: list[str] | None = None, source_type: str = "mcp") -> dict[str, object]:
        """Save a memory for the local owner."""
        data = MemoryCreate(content=content, kind=kind, confidence=confidence,
                            tags=tags or [], source={"type": source_type})
        with SessionLocal() as session:
            memory, warnings = create_memory(session, data, DEFAULT_OWNER)
            return _jsonable({"memory": memory_dict(memory), "warnings": warnings})

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

    return server


def run() -> None:
    initialize_database()
    if get_settings().auth_required:
        raise RuntimeError("Local stdio MCP does not accept remote auth tokens; disable auth for this local profile")
    build_server().run()
