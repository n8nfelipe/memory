"""Persist Opencode sessions in the local memory journal.

The Opencode plugin (``.opencode/plugins/llm-memory-auto.js``) shells out to
``memory capture-opencode --input <json>`` on ``session.created``,
``session.idle`` and ``session.deleted``. The JSON payload carries the
Opencode session id plus the already-fetched text transcript, so the plugin
never streams partial tool calls and no extra model context is consumed.

Only ``user``/``assistant`` text is stored; tool calls and tool results are
ignored, mirroring the Codex capture behavior.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from llm_memory.config import get_settings

MAX_HANDOFF_CHARS = 90_000
MAX_MESSAGE_CHARS = 40_000
SPOOL_ROOT = Path(tempfile.gettempdir()) / "llm-memory-opencode"


def _external(session_id: str) -> str:
    return f"opencode:{session_id}"


def _valid_messages(event: dict[str, Any]) -> list[dict[str, str]]:
    raw = event.get("messages")
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        message_id = item.get("id")
        if role not in {"user", "assistant"}:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        if not isinstance(message_id, str) or not message_id:
            continue
        cleaned.append(
            {"id": message_id, "role": role, "content": content[:MAX_MESSAGE_CHARS]}
        )
    return cleaned


def capture_opencode_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Route an Opencode plugin payload to the configured storage backend."""
    session_id = event.get("opencode_session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if get_settings().storage_backend == "markdown":
        return _capture_markdown(event, session_id)
    return _capture_database(event, session_id)


def _project(event: dict[str, Any]) -> str:
    from llm_memory.scope import project_for_directory

    cwd = event.get("cwd")
    directory = Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
    return project_for_directory(directory)


def _capture_markdown(event: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    from llm_memory.markdown_store import MarkdownStore
    from llm_memory.schemas import AgentSessionCreate, HandoffCreate

    store = MarkdownStore()
    project = _project(event)
    external_id = _external(session_id)
    with store.transaction() as state:
        existing = next(
            (s for s in state.sessions.values() if s.get("external_id") == external_id),
            None,
        )
    hook = event.get("hook")
    if existing is not None and existing.get("status") != "active":
        return None
    if existing is None:
        title = event.get("title")
        label = str(title).strip()[:200] if isinstance(title, str) and title.strip() else ""
        existing = store.start(
            AgentSessionCreate(
                external_id=external_id,
                agent_id="opencode",
                project_id=project,
                title=label or f"Opencode: {project}",
            )
        )
    memory_session_id = str(existing["id"])
    if hook == "session.created":
        return None
    if hook == "session.idle":
        for message in _valid_messages(event):
            store.capture_turn(
                memory_session_id,
                message["role"],
                message["content"],
                f"opencode-msg:{message['id']}",
                source_type="opencode_hook",
            )
        return None
    if hook == "session.deleted":
        memories = store.list_memories(session_id=memory_session_id)
        transcript = "\n\n---\n\n".join(m["content"] for m in memories)
        store.handoff(
            memory_session_id,
            HandoffCreate(
                summary=(
                    transcript[-MAX_HANDOFF_CHARS:]
                    or "Nenhuma mensagem capturada nesta sessão do Opencode."
                )
            ),
        )
    return None


def _spool_paths(session_id: str) -> tuple[Path, Path]:
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return SPOOL_ROOT / f"{key}.jsonl", SPOOL_ROOT / f"{key}.json"


def _write_private(path: Path, text: str, *, append: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as stream:
        stream.write(text)
    path.chmod(0o600)


def _load_meta(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _capture_database(event: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    from llm_memory.continuity import create_handoff, create_session
    from llm_memory.db import SessionLocal, initialize_database
    from llm_memory.schemas import AgentSessionCreate, HandoffCreate
    from llm_memory.service import DEFAULT_OWNER

    hook = event.get("hook")
    spool, meta_path = _spool_paths(session_id)
    if hook == "session.created":
        initialize_database()
        project = _project(event)
        external_id = _external(session_id)
        _write_private(spool, "")
        title = event.get("title")
        label = str(title).strip()[:200] if isinstance(title, str) and title.strip() else ""
        with SessionLocal() as session:
            current = create_session(
                session,
                DEFAULT_OWNER,
                AgentSessionCreate(
                    external_id=external_id,
                    workspace_id="default",
                    project_id=project,
                    agent_id="opencode",
                    title=label or f"Opencode: {project}",
                ),
            )
        _write_private(
            meta_path,
            json.dumps(
                {
                    "service_session_id": current.id,
                    "project_id": project,
                    "external_id": external_id,
                }
            ),
        )
        return None
    metadata = _load_meta(meta_path)
    if metadata is None or not isinstance(metadata.get("service_session_id"), str):
        return None
    if hook == "session.idle":
        try:
            known = {
                (json.loads(line).get("message_id"), json.loads(line).get("role"))
                for line in spool.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        except (OSError, json.JSONDecodeError):
            known = set()
        for message in _valid_messages(event):
            if (message["id"], message["role"]) in known:
                continue
            _write_private(
                spool,
                json.dumps(
                    {
                        "message_id": message["id"],
                        "role": message["role"],
                        "content": message["content"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                append=True,
            )
        return None
    if hook == "session.deleted":
        entries: list[str] = []
        try:
            lines = spool.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("role") in {"user", "assistant"} and isinstance(
                item.get("content"), str
            ):
                entries.append(f"{str(item['role']).capitalize()}:\n{item['content']}")
        transcript = "\n\n---\n\n".join(entries) or (
            "Nenhuma mensagem capturada nesta sessão do Opencode."
        )
        if len(transcript) > MAX_HANDOFF_CHARS:
            transcript = "[início da conversa omitido por limite de tamanho]\n\n" + transcript[
                -MAX_HANDOFF_CHARS:
            ]
        initialize_database()
        with SessionLocal() as session:
            create_handoff(
                session,
                DEFAULT_OWNER,
                str(metadata["service_session_id"]),
                HandoffCreate(summary=transcript),
            )
        spool.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
    return None


def main(input_path: Path) -> dict[str, Any] | None:
    """Entry point for ``memory capture-opencode --input <json>``."""
    event = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(event, dict):
        raise TypeError("Opencode capture input must be a JSON object")
    return capture_opencode_event(event)


if __name__ == "__main__":
    try:
        target = Path(sys.argv[1])
        result = main(target)
        print(json.dumps(result or {}))
    except Exception as exc:  # noqa: BLE001 - hooks must not block the conversation
        print(f"llm-memory Opencode capture failed: {exc}", file=sys.stderr)
