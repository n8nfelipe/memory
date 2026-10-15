"""Session-owned Markdown journals. No database or persistent search index.

Each transaction replaces just its writer session's file. Events refer to older
memories without editing their originating files. A Lamport clock orders events;
concurrent revisions merged from different clones fail closed instead of silently
losing a correction. flock coordinates processes sharing one local checkout only.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from llm_memory.config import Settings, get_settings
from llm_memory.schemas import (
    AgentSessionCreate,
    CompactionPreviewRequest,
    HandoffCreate,
    MemoryCreate,
    MemoryImportRequest,
    MemoryPatch,
    SearchRequest,
)

Record = dict[str, Any]
OWNER = "local"
HEADER = "<!-- llm-memory-session: "
EVENT = "<!-- llm-memory-event: "


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def fail(code: str, message: str, status: int = 409) -> None:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def encode(value: object) -> str:
    # Keep metadata on one line, even when user text contains HTML comment endings.
    return json.dumps(value, ensure_ascii=True).replace("-->", "\\u002d\\u002d>")


def expired(memory: Record) -> bool:
    value = memory.get("expires_at")
    if not value:
        return False
    date = datetime.fromisoformat(value)
    return date.replace(tzinfo=date.tzinfo or UTC) <= datetime.now(UTC)


def render(document: Record) -> str:
    parts = [HEADER + encode(document["session"]) + " -->\n\n# Sessão\n"]
    for event in document["events"]:
        meta = copy.deepcopy(event)
        content = meta.pop("text", "")
        parts.append("\n" + EVENT + encode(meta) + " -->\n")
        parts.append(content + "\n<!-- /llm-memory-event:" + event["id"] + " -->\n")
    return "".join(parts)


def parse(text: str, path: Path) -> Record:
    try:
        header, rest = text.split("\n\n# Sessão\n", 1)
        if not header.startswith(HEADER) or not header.endswith(" -->"):
            raise ValueError("missing session header")
        document = {"session": json.loads(header[len(HEADER) : -4]), "events": []}
        while rest:
            if not rest.startswith("\n" + EVENT):
                raise ValueError("invalid event header or unresolved Git conflict")
            line, rest = rest[1:].split("\n", 1)
            meta = json.loads(line[len(EVENT) : -4])
            marker = "\n<!-- /llm-memory-event:" + meta["id"] + " -->\n"
            content, rest = rest.split(marker, 1)
            meta["text"] = content
            document["events"].append(meta)
        return document
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Invalid session Markdown: {path}: {exc}") from exc


class State:
    def __init__(self, documents: dict[str, Record]):
        self.documents = documents
        self.sessions: dict[str, Record] = {
            key: copy.deepcopy(doc["session"]) for key, doc in documents.items()
        }
        self.memories: dict[str, Record] = {}
        self.history: dict[str, list[Record]] = {}
        self.handoffs: dict[str, Record] = {}
        self.proposals: dict[str, Record] = {}
        self.relations: list[Record] = []
        events = sorted(
            (event for doc in documents.values() for event in doc["events"]),
            key=lambda event: (event["clock"], event["id"]),
        )
        self.clock = max((event["clock"] for event in events), default=0)
        seen: set[str] = set()
        for event in events:
            if event["id"] in seen:
                fail("duplicate_event", "Duplicate event IDs in repository")
            seen.add(event["id"])
            self.apply(event)

    def apply(self, event: Record) -> None:
        for change in event["changes"]:
            kind = change["type"]
            data = copy.deepcopy(change["data"])
            if change.get("text"):
                data[change["text"]] = event["text"]
            key = data.get("id", "")
            if kind == "memory":
                previous = self.memories.get(key)
                if change.get("base", 0) != (previous["version"] if previous else 0):
                    fail(
                        "concurrent_revision",
                        f"Concurrent revisions for memory {key}; review Git merge",
                    )
                self.memories[key] = data
                self.history.setdefault(key, []).append(
                    {
                        "version": data["version"],
                        "content": data["content"],
                        "changed_by": OWNER,
                        "created_at": data["updated_at"],
                    }
                )
            elif kind == "session":
                self.sessions[key] = data
            elif kind == "handoff":
                previous = self.handoffs.get(key)
                if change.get("base") != (previous["status"] if previous else None):
                    fail(
                        "concurrent_handoff",
                        f"Concurrent handoff changes for {key}; review Git merge",
                    )
                self.handoffs[key] = data
            elif kind == "proposal":
                self.proposals[key] = data
            elif kind == "relation":
                self.relations.append(data)
            elif kind == "history":
                self.history.setdefault(data.pop("memory_id"), []).append(data)
            else:
                raise ValueError(f"Unknown event type: {kind}")

    def get(self, memory_id: str) -> Record:
        memory = self.memories.get(memory_id)
        if memory is None or memory["status"] == "deleted" or expired(memory):
            fail("not_found", "Memory not found", 404)
        assert memory is not None
        return copy.deepcopy(memory)


class MarkdownStore:
    def __init__(self, root: Path | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.root = (root or self.settings.repository).resolve()
        self.current_session: str | None = None
        if self.settings.auth_required:
            raise ValueError("Markdown storage is local only; use database storage for token auth")
        if self.settings.embedding_url:
            raise ValueError("Markdown storage uses lexical search; unset MEMORY_EMBEDDING_URL")

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (self.root / ".git").exists():
            subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        (self.root / "sessions").mkdir(exist_ok=True, mode=0o700)

    @contextmanager
    def transaction(self) -> Iterator[State]:
        self.initialize()
        # git-path supports both ordinary clones and linked worktrees.
        result = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "--git-path", "llm-memory.lock"],
            capture_output=True,
            text=True,
            check=True,
        )
        lock_path = Path(result.stdout.strip())
        if not lock_path.is_absolute():
            lock_path = self.root / lock_path
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                documents = {}
                for path in sorted((self.root / "sessions").glob("*.md")):
                    if path.is_symlink():
                        raise ValueError(f"Session file cannot be a symlink: {path}")
                    doc = parse(path.read_text(encoding="utf-8"), path)
                    key = doc["session"]["id"]
                    if path.name != self.filename(key) or key in documents:
                        raise ValueError(f"Invalid or duplicate session ID: {path}")
                    documents[key] = doc
                yield State(documents)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def filename(session_id: str) -> str:
        # No user-controlled path segments, including imported IDs.
        return str(uuid.UUID(session_id)) + ".md"

    def write(self, document: Record) -> None:
        path = self.root / "sessions" / self.filename(document["session"]["id"])
        fd, temporary = tempfile.mkstemp(prefix=".session-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(render(document))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def start(self, data: AgentSessionCreate) -> Record:
        with self.transaction() as state:
            if data.external_id:
                for item in state.sessions.values():
                    if item["external_id"] != data.external_id:
                        continue
                    if item["status"] != "active":
                        fail("session_closed", "External session is already closed")
                    if any(
                        item[key] != getattr(data, key)
                        for key in ("workspace_id", "project_id", "agent_id")
                    ):
                        fail("session_scope_mismatch", "External session belongs to another scope")
                    self.current_session = item["id"]
                    return item
            item = {
                **data.model_dump(mode="json"),
                "id": new_id(),
                "owner_id": OWNER,
                "status": "active",
                "started_at": now(),
                "updated_at": now(),
                "ended_at": None,
            }
            self.write({"session": item, "events": []})
            self.current_session = item["id"]
            return item

    def writer(
        self, state: State, session_id: str | None = None, context: Record | None = None
    ) -> Record:
        context = context or {}
        # A revision retains original provenance but belongs to the current writer.
        if "version" in context:
            context = {key: context.get(key) for key in ("workspace_id", "project_id")}
        selected = session_id or self.current_session
        if selected:
            item = state.sessions.get(selected)
            if item is None or item["status"] != "active":
                if session_id:
                    fail("session_not_found", "Active session not found", 404)
                selected = None
            elif any(
                context.get(key) not in (None, item[key])
                for key in ("workspace_id", "project_id", "agent_id")
            ):
                if session_id:
                    fail("session_scope_mismatch", "Memory scope must match its session", 422)
                selected = None
        if not selected:
            item = {
                "id": new_id(),
                "owner_id": OWNER,
                "external_id": None,
                "workspace_id": context.get("workspace_id") or "default",
                "project_id": context.get("project_id") or "default",
                "agent_id": context.get("agent_id") or "local",
                "title": "Implicit session",
                "status": "active",
                "started_at": now(),
                "updated_at": now(),
                "ended_at": None,
            }
            selected = item["id"]
            state.sessions[selected] = item
            state.documents[selected] = {"session": item.copy(), "events": []}
        self.current_session = selected
        return state.sessions[selected]

    def append(self, state: State, writer: Record, changes: list[Record], text: str = "") -> None:
        event = {
            "id": new_id(),
            "clock": state.clock + 1,
            "created_at": now(),
            "changes": changes,
            "text": text,
        }
        state.apply(event)
        state.clock += 1
        document = state.documents[writer["id"]]
        document["events"].append(event)
        self.write(document)

    def check_policy(
        self, state: State, incoming: list[Record], replacing: str | None = None
    ) -> None:
        existing = [
            m for m in state.memories.values() if m["status"] != "deleted" and m["id"] != replacing
        ]
        settings = self.settings
        if settings.allowed_kinds and any(
            m["kind"] not in settings.allowed_kinds for m in incoming
        ):
            fail("kind_not_allowed", "Memory kind is blocked by policy", 422)
        if (
            settings.max_entries_per_owner
            and len(existing + incoming) > settings.max_entries_per_owner
        ):
            fail("memory_limit_exceeded", "Memory count limit reached", 413)
        size = sum(len(m["content"].encode()) for m in existing + incoming)
        if settings.max_bytes_per_owner and size > settings.max_bytes_per_owner:
            fail("memory_limit_exceeded", "Memory size limit reached", 413)

    @staticmethod
    def memory_change(memory: Record, base: int = 0, text: bool = False) -> Record:
        data = memory.copy()
        if text:
            data.pop("content")
        return {"type": "memory", "base": base, "data": data, "text": "content" if text else None}

    def create(self, data: MemoryCreate) -> Record:
        with self.transaction() as state:
            writer = self.writer(state, data.session_id, data.model_dump())
            item = {
                **data.model_dump(mode="json"),
                "id": new_id(),
                "owner_id": OWNER,
                "scope_id": data.scope_id or f"private:{OWNER}",
                "status": "active",
                "created_at": now(),
                "updated_at": now(),
                "version": 1,
                **{key: writer[key] for key in ("workspace_id", "project_id", "agent_id")},
                "session_id": writer["id"],
            }
            if not item["expires_at"] and self.settings.default_ttl_days:
                item["expires_at"] = (
                    datetime.now(UTC) + timedelta(days=self.settings.default_ttl_days)
                ).isoformat()
            self.check_policy(state, [item])
            warnings = [
                f"possible_exact_duplicate:{m['id']}"
                for m in state.memories.values()
                if m["content"].casefold() == item["content"].casefold()
                and m["status"] == "active"
                and m["project_id"] == item["project_id"]
            ]
            self.append(state, writer, [self.memory_change(item, text=True)], item["content"])
            return {"memory": item, "warnings": warnings}

    def get(self, memory_id: str) -> Record:
        with self.transaction() as state:
            return state.get(memory_id)

    def update(self, memory_id: str, patch: MemoryPatch, session_id: str | None = None) -> Record:
        with self.transaction() as state:
            item = state.get(memory_id)
            if item["version"] != patch.expected_version:
                fail("version_conflict", "Memory version changed; read it again")
            writer = self.writer(state, session_id, item)
            changes = patch.model_dump(
                mode="json", exclude_unset=True, exclude={"expected_version"}
            )
            item.update({k: v for k, v in changes.items() if v is not None or k == "expires_at"})
            item.update(version=item["version"] + 1, updated_at=now())
            self.check_policy(state, [item], replacing=memory_id)
            self.append(
                state,
                writer,
                [self.memory_change(item, patch.expected_version, True)],
                item["content"],
            )
            return item

    def history(self, memory_id: str) -> list[Record]:
        with self.transaction() as state:
            state.get(memory_id)
            return sorted(state.history[memory_id], key=lambda row: row["version"])

    def list_memories(self, **filters: Any) -> list[Record]:
        with self.transaction() as state:
            return [
                m
                for m in state.memories.values()
                if not expired(m)
                and m["status"] != "deleted"
                and all(value is None or m.get(key) == value for key, value in filters.items())
            ]

    def search(self, data: SearchRequest) -> list[Record]:
        terms = set(re.findall(r"\w+", data.query.casefold()))
        filters = data.model_dump(exclude={"query", "limit", "include_archived"})
        hits: list[Record] = []
        for memory in self.list_memories(**filters):
            if memory["status"] not in (
                {"active", "conflict", "archived"}
                if data.include_archived
                else {"active", "conflict"}
            ):
                continue
            words = set(
                re.findall(r"\w+", (memory["content"] + " " + " ".join(memory["tags"])).casefold())
            )
            score = len(terms & words) / max(len(terms), 1)
            if score:
                hits.append({"memory": memory, "score": score, "reason": "token_overlap"})
        return sorted(hits, key=lambda hit: (-hit["score"], hit["memory"]["id"]))[: data.limit]

    def delete(self, memory_id: str, session_id: str | None = None) -> None:
        with self.transaction() as state:
            item = state.get(memory_id)
            writer = self.writer(state, session_id, item)
            targets = {memory_id}
            while True:
                expanded = targets | {
                    r["derived_id"] for r in state.relations if r["source_id"] in targets
                }
                if expanded == targets:
                    break
                targets = expanded
            changes = []
            for key in sorted(targets):
                memory = state.memories[key].copy()
                base = memory["version"]
                memory.update(status="deleted", version=base + 1, updated_at=now())
                changes.append(self.memory_change(memory, base))
            self.append(state, writer, changes)

    def handoff(self, session_id: str, data: HandoffCreate) -> Record:
        with self.transaction() as state:
            writer = self.writer(state, session_id)
            item = {
                **data.model_dump(mode="json"),
                "id": new_id(),
                "session_id": session_id,
                "owner_id": OWNER,
                "workspace_id": writer["workspace_id"],
                "project_id": writer["project_id"],
                "from_agent": writer["agent_id"],
                "status": "pending",
                "claimed_by_agent": None,
                "claimed_at": None,
                "created_at": now(),
                "completed_at": None,
            }
            closed = {**writer, "status": "handed_off", "updated_at": now(), "ended_at": now()}
            payload = item.copy()
            payload.pop("summary")
            self.append(
                state,
                writer,
                [
                    {"type": "session", "data": closed},
                    {"type": "handoff", "data": payload, "text": "summary"},
                ],
                data.summary,
            )
            self.current_session = None
            return item

    def claim(
        self, workspace_id: str, project_id: str, agent_id: str, session_id: str | None = None
    ) -> Record:
        with self.transaction() as state:
            candidates = sorted(
                (
                    h
                    for h in state.handoffs.values()
                    if h["status"] == "pending"
                    and h["workspace_id"] == workspace_id
                    and h["project_id"] == project_id
                    and h["to_agent"] in (None, agent_id)
                ),
                key=lambda h: (h["created_at"], h["id"]),
            )
            if not candidates:
                fail("no_pending_handoff", "No pending handoff for this project", 404)
            writer = self.writer(
                state,
                session_id,
                {"workspace_id": workspace_id, "project_id": project_id, "agent_id": agent_id},
            )
            item = {
                **candidates[0],
                "status": "claimed",
                "claimed_by_agent": agent_id,
                "claimed_at": now(),
            }
            self.append(state, writer, [{"type": "handoff", "base": "pending", "data": item}])
            return item

    def complete(self, handoff_id: str, agent_id: str, session_id: str | None = None) -> Record:
        with self.transaction() as state:
            item = state.handoffs.get(handoff_id)
            if item is None:
                fail("handoff_not_found", "Handoff not found", 404)
            assert item is not None
            if item["status"] != "claimed" or item["claimed_by_agent"] != agent_id:
                fail("handoff_not_claimed", "Handoff must be claimed by this agent first")
            writer = self.writer(
                state,
                session_id,
                {
                    "workspace_id": item["workspace_id"],
                    "project_id": item["project_id"],
                    "agent_id": agent_id,
                },
            )
            item = {**item, "status": "completed", "completed_at": now()}
            self.append(state, writer, [{"type": "handoff", "base": "claimed", "data": item}])
            return item

    def preview(self, data: CompactionPreviewRequest) -> Record:
        with self.transaction() as state:
            if len(set(data.memory_ids)) != len(data.memory_ids):
                fail("duplicate_source", "Source IDs must be unique", 422)
            sources = [state.get(key) for key in data.memory_ids]
            if any(m["status"] != "active" for m in sources):
                fail("invalid_state", "All sources must be active")
            if len({(m["scope_id"], m["workspace_id"], m["project_id"]) for m in sources}) != 1:
                fail("scope_mismatch", "Sources must share scope, workspace and project", 422)
            before = sum(len(m["content"].encode()) for m in sources)
            after = len(data.proposed_content.encode())
            result = {
                "id": new_id(),
                **data.model_dump(),
                "estimated_bytes_before": before,
                "estimated_bytes_after": after,
                "reduction_ratio": round(1 - after / before, 4),
                "status": "pending",
            }
            writer = self.writer(state, context=sources[0])
            self.append(
                state,
                writer,
                [
                    {
                        "type": "proposal",
                        "data": {**result, "versions": [m["version"] for m in sources]},
                    }
                ],
            )
            return result

    def compact(self, proposal_id: str, session_id: str | None = None) -> Record:
        with self.transaction() as state:
            proposal = state.proposals.get(proposal_id)
            if proposal is None:
                fail("not_found", "Compaction proposal not found", 404)
            assert proposal is not None
            if proposal["status"] != "pending":
                fail("already_applied", "Proposal is already applied")
            request = CompactionPreviewRequest(
                memory_ids=proposal["memory_ids"], proposed_content=proposal["proposed_content"]
            )
            sources = [state.get(key) for key in request.memory_ids]
            if len(set(request.memory_ids)) != len(sources):
                fail("duplicate_source", "Source IDs must be unique", 422)
            if (
                any(m["status"] != "active" for m in sources)
                or [m["version"] for m in sources] != proposal["versions"]
            ):
                fail("stale_proposal", "Sources changed; preview again")
            if len({(m["scope_id"], m["workspace_id"], m["project_id"]) for m in sources}) != 1:
                fail("scope_mismatch", "Sources must share scope, workspace and project", 422)
            writer = self.writer(state, session_id, sources[0])
            result = {
                **sources[0],
                "id": new_id(),
                "content": request.proposed_content,
                "session_id": writer["id"],
                "agent_id": writer["agent_id"],
                "kind": "summary",
                "confidence": "inferred",
                "version": 1,
                "source": {"type": "compaction", "ref": None},
                "tags": [],
                "created_at": now(),
                "updated_at": now(),
            }
            self.check_policy(state, [result])
            changes = [self.memory_change(result, text=True)]
            for item in sources:
                changes.append(
                    self.memory_change(
                        {
                            **item,
                            "status": "archived",
                            "version": item["version"] + 1,
                            "updated_at": now(),
                        },
                        item["version"],
                    )
                )
                changes.append(
                    {
                        "type": "relation",
                        "data": {
                            "source_id": item["id"],
                            "derived_id": result["id"],
                            "relation": "summarized_from",
                        },
                    }
                )
            changes.append({"type": "proposal", "data": {**proposal, "status": "applied"}})
            self.append(state, writer, changes, result["content"])
            return result

    def export(self) -> Record:
        with self.transaction() as state:
            memories = [m for m in state.memories.values() if m["status"] != "deleted"]
            ids = {m["id"] for m in memories}
            return {
                "schema_version": "1",
                "memories": memories,
                "relations": [
                    r for r in state.relations if r["source_id"] in ids and r["derived_id"] in ids
                ],
            }

    def import_data(self, data: MemoryImportRequest) -> Record:
        with self.transaction() as state:
            ids = {m.id for m in data.memories}
            if len(ids) != len(data.memories) or ids & state.memories.keys():
                fail("duplicate_ids", "Import contains duplicate or existing memory IDs")
            memories = [{**m.model_dump(mode="json"), "owner_id": OWNER} for m in data.memories]
            self.check_policy(state, memories)
            changes = []
            for memory in memories:
                if memory["scope_id"].startswith("private:"):
                    memory["scope_id"] = f"private:{OWNER}"
                changes.append(self.memory_change(memory))
            relations = []
            for relation in data.relations:
                if relation.get("source_id") not in ids or relation.get("derived_id") not in ids:
                    fail("invalid_relation", "Import relation references unknown memory", 422)
                if (
                    relation.get("relation")
                    not in {"summarized_from", "supersedes", "duplicates", "conflicts_with"}
                    or relation in relations
                ):
                    fail("invalid_relation", "Invalid or duplicate import relation", 422)
                relations.append(relation)
                changes.append({"type": "relation", "data": relation})
            writer = self.writer(state)
            self.append(state, writer, changes)
            return {"imported": len(memories), "relations": len(relations)}

    def commit(self, session_id: str, message: str) -> str:
        with self.transaction() as state:
            if session_id not in state.sessions:
                fail("session_not_found", "Session not found", 404)
            path = "sessions/" + self.filename(session_id)
            prefix = ["git", "-C", str(self.root)]
            subprocess.run([*prefix, "add", "--", path], check=True, capture_output=True)
            result = subprocess.run(
                [*prefix, "commit", "--only", "-m", message, "--", path],
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()

    def resolve(
        self, memory_id: str, related_id: str, decision: str, session_id: str | None = None
    ) -> Record:
        if decision not in {"keep_memory", "keep_related", "keep_both"}:
            fail("invalid_decision", "Unsupported conflict decision", 422)
        with self.transaction() as state:
            first, second = state.get(memory_id), state.get(related_id)
            if first["id"] == second["id"] or any(
                first[k] != second[k] for k in ("scope_id", "workspace_id", "project_id")
            ):
                fail("invalid_pair", "Memories must be distinct and share a scope", 422)
            writer = self.writer(state, session_id, first)
            changes = []
            for item, keep in (
                (first, decision != "keep_related"),
                (second, decision != "keep_memory"),
            ):
                base = item["version"]
                item.update(
                    status="active" if keep else "deleted", version=base + 1, updated_at=now()
                )
                changes.append(self.memory_change(item, base))
            if decision == "keep_both":
                changes.append(
                    {
                        "type": "relation",
                        "data": {
                            "source_id": memory_id,
                            "derived_id": related_id,
                            "relation": "conflicts_with",
                        },
                    }
                )
            self.append(state, writer, changes)
            return second if decision == "keep_related" else first

    def purge_expired(self) -> int:
        with self.transaction() as state:
            targets = {
                m["id"] for m in state.memories.values() if m["status"] != "deleted" and expired(m)
            }
            count = len(targets)
            if not targets:
                return 0
            while True:
                expanded = targets | {
                    r["derived_id"] for r in state.relations if r["source_id"] in targets
                }
                if expanded == targets:
                    break
                targets = expanded
            writer = self.writer(state)
            changes = []
            for key in sorted(targets):
                item = state.memories[key]
                changes.append(
                    self.memory_change(
                        {
                            **item,
                            "status": "deleted",
                            "version": item["version"] + 1,
                            "updated_at": now(),
                        },
                        item["version"],
                    )
                )
            self.append(state, writer, changes)
            return count

    def capture_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        tag: str | None,
        source_type: str = "codex_hook",
    ) -> None:
        with self.transaction() as state:
            writer = self.writer(state, session_id)
            if tag and any(
                m["session_id"] == session_id and tag in m["tags"] for m in state.memories.values()
            ):
                return
            item = {
                "id": new_id(),
                "owner_id": OWNER,
                "scope_id": f"private:{OWNER}",
                "workspace_id": writer["workspace_id"],
                "project_id": writer["project_id"],
                "agent_id": writer["agent_id"],
                "session_id": session_id,
                "content": f"## {role.capitalize()}\n\n{content.strip()}",
                "kind": "conversation",
                "source": {"type": source_type, "ref": tag},
                "confidence": "asserted",
                "tags": [role, *([tag] if tag else [])],
                "status": "active",
                "expires_at": None,
                "created_at": now(),
                "updated_at": now(),
                "version": 1,
            }
            self.check_policy(state, [item])
            self.append(state, writer, [self.memory_change(item, text=True)], item["content"])
