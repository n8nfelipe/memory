"""Small synchronous client for integrating the memory service into Python agents."""

from __future__ import annotations

import json
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from llm_memory.scope import project_for_directory


class MemoryClientError(RuntimeError):
    """Raised when the memory service rejects a request or cannot be reached."""


class MemoryClient:
    """Client for writing memories and retrieving context through the REST API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        token: str | None = None,
        *,
        workspace_id: str = "default",
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.workspace_id = workspace_id
        self.project_id = project_id or project_for_directory()
        self.agent_id = agent_id
        self.session_id: str | None = None

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=15) as response:
                if response.status == 204:
                    return None
                return json.loads(response.read())
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = {"message": str(exc)}
            raise MemoryClientError(f"Memory API returned HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, URLError) as exc:
            raise MemoryClientError(f"Could not reach memory API at {self.base_url}: {exc}") from exc

    def remember(
        self,
        content: str,
        *,
        kind: str = "fact",
        scope_id: str | None = None,
        source_type: str = "agent",
        source_ref: str | None = None,
        confidence: str = "unverified",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Store one durable memory and return the created memory and warnings."""
        source: dict[str, str] = {"type": source_type}
        if source_ref is not None:
            source["ref"] = source_ref
        payload: dict[str, Any] = {
            "content": content,
            "kind": kind,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "source": source,
            "confidence": confidence,
            "tags": tags or [],
        }
        if scope_id is not None:
            payload["scope_id"] = scope_id
        if self.agent_id is not None:
            payload["agent_id"] = self.agent_id
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        return cast(dict[str, Any], self._request("POST", "/v1/memories", payload))

    def search(
        self,
        query: str,
        *,
        scope_id: str | None = None,
        limit: int = 5,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """Return ranked memories relevant to a query."""
        payload: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "include_archived": include_archived,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
        }
        if scope_id is not None:
            payload["scope_id"] = scope_id
        if self.agent_id is not None:
            payload["agent_id"] = self.agent_id
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        result = cast(dict[str, Any], self._request("POST", "/v1/search", payload))
        return cast(list[dict[str, Any]], result["results"])

    def start_session(
        self,
        agent_id: str,
        *,
        title: str = "",
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Start or resume an active, project-scoped agent session."""
        payload: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "agent_id": agent_id,
            "title": title,
        }
        if external_id is not None:
            payload["external_id"] = external_id
        result = cast(dict[str, Any], self._request("POST", "/v1/sessions", payload))
        self.agent_id = agent_id
        self.session_id = result["id"]
        return result

    def create_handoff(
        self,
        summary: str,
        *,
        decisions: list[str] | None = None,
        blockers: list[str] | None = None,
        next_steps: list[str] | None = None,
        to_agent: str | None = None,
    ) -> dict[str, Any]:
        """Close the active session and create its pending handoff."""
        if self.session_id is None:
            raise MemoryClientError("Start a session before creating a handoff")
        payload = {
            "summary": summary,
            "decisions": decisions or [],
            "blockers": blockers or [],
            "next_steps": next_steps or [],
            "to_agent": to_agent,
        }
        result = cast(
            dict[str, Any],
            self._request("POST", f"/v1/sessions/{self.session_id}/handoff", payload),
        )
        self.session_id = None
        return result

    def claim_next_handoff(self) -> dict[str, Any]:
        """Atomically claim the oldest pending handoff in this project."""
        if self.agent_id is None:
            raise MemoryClientError("Set agent_id or start a session before claiming a handoff")
        return cast(
            dict[str, Any],
            self._request("POST", "/v1/handoffs/claim-next", {
                "workspace_id": self.workspace_id,
                "project_id": self.project_id,
                "agent_id": self.agent_id,
            }),
        )

    def complete_handoff(self, handoff_id: str) -> dict[str, Any]:
        """Mark a handoff complete when it was claimed by this agent."""
        if self.agent_id is None:
            raise MemoryClientError("Set agent_id or start a session before completing a handoff")
        return cast(
            dict[str, Any],
            self._request("POST", f"/v1/handoffs/{handoff_id}/complete", {
                "workspace_id": self.workspace_id,
                "project_id": self.project_id,
                "agent_id": self.agent_id,
            }),
        )

    def context(
        self,
        query: str,
        *,
        scope_id: str | None = None,
        limit: int = 5,
        max_chars: int = 6_000,
    ) -> str:
        """Format search hits as bounded context to add to an agent prompt."""
        hits = self.search(query, scope_id=scope_id, limit=limit)
        blocks: list[str] = []
        used = 0
        for hit in hits:
            memory = hit["memory"]
            block = f"- {memory['content']}"
            remaining = max_chars - used
            if remaining <= 0:
                break
            if len(block) > remaining:
                block = block[:remaining]
            blocks.append(block)
            used += len(block) + 1
        return "\n".join(blocks)
