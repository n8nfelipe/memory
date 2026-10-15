from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from llm_memory.markdown_store import MarkdownStore
from llm_memory.schemas import (
    AgentSessionCreate,
    CompactionPreviewRequest,
    Confidence,
    HandoffCreate,
    MemoryCreate,
    MemoryPatch,
    SearchRequest,
    SourceInput,
)
from llm_memory.scope import project_for_directory


def build_server() -> MCPServer:
    server = MCPServer("LLM Memory")
    store = MarkdownStore()

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def memory_search(
        query: str,
        limit: int = 5,
        scope_id: str | None = None,
        workspace_id: str = "default",
        project_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search Markdown memories across sessions in this project."""
        return store.search(
            SearchRequest(
                query=query,
                limit=max(1, min(limit, 50)),
                scope_id=scope_id,
                workspace_id=workspace_id,
                project_id=project_id or project_for_directory(),
                agent_id=agent_id,
                session_id=session_id,
            )
        )

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def memory_get(memory_id: str) -> dict[str, Any]:
        """Read a memory and its provenance."""
        return store.get(memory_id)

    @server.tool()
    def memory_create(
        content: str,
        kind: str = "fact",
        confidence: Confidence = "unverified",
        tags: list[str] | None = None,
        source_type: str = "mcp",
        workspace_id: str = "default",
        project_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a memory to the current session's Markdown file."""
        return store.create(
            MemoryCreate(
                content=content,
                kind=kind,
                confidence=confidence,
                tags=tags or [],
                source=SourceInput(type=source_type),
                workspace_id=workspace_id,
                project_id=project_id or project_for_directory(),
                agent_id=agent_id,
                session_id=session_id,
            )
        )

    @server.tool()
    def memory_session_start(
        agent_id: str,
        title: str = "",
        workspace_id: str = "default",
        project_id: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a session with its own Markdown file; subsequent writes use this session."""
        return store.start(
            AgentSessionCreate(
                agent_id=agent_id,
                title=title,
                workspace_id=workspace_id,
                project_id=project_id or project_for_directory(),
                external_id=external_id,
            )
        )

    @server.tool()
    def memory_session_handoff(
        session_id: str,
        summary: str,
        decisions: list[str] | None = None,
        blockers: list[str] | None = None,
        next_steps: list[str] | None = None,
        to_agent: str | None = None,
    ) -> dict[str, Any]:
        """Close a session, retaining its Markdown journal and recording a handoff."""
        return store.handoff(
            session_id,
            HandoffCreate(
                summary=summary,
                decisions=decisions or [],
                blockers=blockers or [],
                next_steps=next_steps or [],
                to_agent=to_agent,
            ),
        )

    @server.tool()
    def memory_handoff_claim(
        agent_id: str,
        project_id: str | None = None,
        workspace_id: str = "default",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Claim a pending handoff. Claims are atomic within this local checkout."""
        return store.claim(
            workspace_id, project_id or project_for_directory(), agent_id, session_id
        )

    @server.tool()
    def memory_handoff_complete(
        handoff_id: str, agent_id: str, session_id: str | None = None
    ) -> dict[str, Any]:
        """Complete a handoff claimed by this agent."""
        return store.complete(handoff_id, agent_id, session_id)

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def memory_delete(
        memory_id: str, confirm: bool, session_id: str | None = None
    ) -> dict[str, str]:
        """Hide a memory and its derivatives. Content remains in journals and Git history."""
        if not confirm:
            raise ValueError("Set confirm=true after reviewing the memory")
        store.delete(memory_id, session_id)
        return {"status": "deleted", "memory_id": memory_id}

    @server.tool()
    def memory_update(
        memory_id: str,
        expected_version: int,
        content: str | None = None,
        kind: str | None = None,
        confidence: Confidence | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a revision in the current session without modifying the original session."""
        return store.update(
            memory_id,
            MemoryPatch(
                content=content,
                kind=kind,
                confidence=confidence,
                tags=tags,
                expected_version=expected_version,
            ),
            session_id,
        )

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def memory_history(memory_id: str) -> list[dict[str, Any]]:
        """Read recorded versions across sessions."""
        return store.history(memory_id)

    @server.tool()
    def memory_compaction_preview(memory_ids: list[str], proposed_content: str) -> dict[str, Any]:
        """Record a proposal for review; source memories remain active."""
        return store.preview(
            CompactionPreviewRequest(
                memory_ids=memory_ids,
                proposed_content=proposed_content,
            )
        )

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def memory_compaction_apply(
        proposal_id: str, confirm: bool, session_id: str | None = None
    ) -> dict[str, Any]:
        """Apply a reviewed proposal and archive its source memories."""
        if not confirm:
            raise ValueError("Set confirm=true after reviewing the proposal")
        return store.compact(proposal_id, session_id)

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def memory_resolve_conflict(
        memory_id: str,
        related_memory_id: str,
        decision: str,
        confirm: bool,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve two conflicting memories by recording the decision in this session."""
        if not confirm:
            raise ValueError("Set confirm=true after reviewing both memories")
        return store.resolve(memory_id, related_memory_id, decision, session_id)

    return server


def run() -> None:
    build_server().run()
