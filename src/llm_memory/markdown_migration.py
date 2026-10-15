"""Read a legacy database into session journals without modifying the source."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import MetaData, create_engine, select

from llm_memory.markdown_store import MarkdownStore, State, now, render


def migrate_database(store: MarkdownStore, database_url: str) -> dict[str, int]:
    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        metadata.reflect(bind=engine)
        if "memories" not in metadata.tables:
            raise ValueError("Source is not an initialized memory database")
        tables: dict[str, list[dict[str, Any]]] = {}
        with engine.connect() as connection:
            for name in (
                "memories",
                "agent_sessions",
                "handoffs",
                "memory_versions",
                "memory_relations",
                "compaction_proposals",
            ):
                if name not in metadata.tables:
                    tables[name] = []
                    continue
                table = metadata.tables[name]
                rows = connection.execute(select(table)).mappings().all()
                tables[name] = [json.loads(json.dumps(dict(row), default=str)) for row in rows]
    finally:
        engine.dispose()
    if any(
        row.get("owner_id") != "local"
        for name in ("memories", "agent_sessions", "handoffs")
        for row in tables[name]
    ):
        raise ValueError(
            "Migration supports local-owner databases only; shared access needs review"
        )
    documents: dict[str, dict[str, Any]] = {}
    for item in tables["agent_sessions"]:
        store.filename(item["id"])
        documents[item["id"]] = {"session": item, "events": []}
    # Deterministic synthetic session for records created before sessions existed.
    orphan = str(uuid.uuid5(uuid.NAMESPACE_URL, database_url + "#legacy-session"))
    stamp = min((m["created_at"] for m in tables["memories"]), default=now())
    legacy = {
        "id": orphan,
        "external_id": None,
        "owner_id": "local",
        "workspace_id": "default",
        "project_id": "legacy",
        "agent_id": "migration",
        "title": "Legacy memories",
        "status": "handed_off",
        "started_at": stamp,
        "updated_at": stamp,
        "ended_at": stamp,
    }
    clock = 0

    def append(session_id: str | None, change: dict[str, Any], text: str = "") -> None:
        nonlocal clock
        target = session_id if session_id in documents else orphan
        documents.setdefault(target, {"session": legacy, "events": []})
        clock += 1
        # Stable IDs make rerunning a completed or interrupted migration safe.
        event_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, database_url + json.dumps(change, sort_keys=True, default=str)
            )
        )
        documents[target]["events"].append(
            {"id": event_id, "clock": clock, "created_at": stamp, "changes": [change], "text": text}
        )

    memory_sessions = {}
    for row in sorted(tables["memories"], key=lambda m: m["id"]):
        memory = row.copy()
        memory["source"] = {
            "type": memory.pop("source_type") or "manual",
            "ref": memory.pop("source_ref"),
        }
        memory["tags"] = json.loads(memory["tags"] or "[]")
        memory.setdefault("session_id", None)
        memory_sessions[memory["id"]] = memory["session_id"]
        append(memory["session_id"], store.memory_change(memory, text=True), memory["content"])
    for row in sorted(tables["memory_versions"], key=lambda r: (r["memory_id"], r["version"])):
        item = {key: value for key, value in row.items() if key != "id"}
        append(memory_sessions.get(row["memory_id"]), {"type": "history", "data": item})
    for row in sorted(tables["handoffs"], key=lambda h: h["id"]):
        item = row.copy()
        for key in ("decisions", "blockers", "next_steps"):
            item[key] = json.loads(item[key])
        text = item.pop("summary")
        append(item["session_id"], {"type": "handoff", "data": item, "text": "summary"}, text)
    for row in sorted(tables["memory_relations"], key=lambda r: r["id"]):
        append(
            memory_sessions.get(row["derived_id"]),
            {
                "type": "relation",
                "data": {key: row[key] for key in ("source_id", "derived_id", "relation")},
            },
        )
    for row in sorted(tables["compaction_proposals"], key=lambda r: r["id"]):
        ids = json.loads(row["source_ids"])
        by_id = {m["id"]: m for m in tables["memories"]}
        before = row["estimated_bytes_before"]
        proposal = {
            **row,
            "memory_ids": ids,
            "versions": [by_id[key]["version"] for key in ids if key in by_id],
            "reduction_ratio": round(1 - row["estimated_bytes_after"] / before, 4) if before else 0,
        }
        append(memory_sessions.get(ids[0]) if ids else None, {"type": "proposal", "data": proposal})
    State(documents)  # Verify before writing any file.
    with store.transaction() as state:
        for key, document in documents.items():
            if key in state.documents and render(state.documents[key]) != render(document):
                raise ValueError("Destination session changed; refusing to overwrite it")
        incoming_ids = {m["id"] for m in tables["memories"]}
        other_state = State({k: v for k, v in state.documents.items() if k not in documents})
        if incoming_ids & other_state.memories.keys():
            raise ValueError("Destination already contains one of the imported memories")
        for key, document in documents.items():
            if key not in state.documents:
                store.write(document)
    return {
        "sessions": len(documents),
        "memories": len(tables["memories"]),
        "handoffs": len(tables["handoffs"]),
    }
