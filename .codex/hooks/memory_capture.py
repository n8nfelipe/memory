"""Persist Codex user prompts and final answers as a local session handoff."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from llm_memory.config import get_settings

if get_settings().storage_backend == "database":
    from sqlalchemy import desc, select

    from llm_memory.continuity import create_handoff, create_session
    from llm_memory.db import SessionLocal, initialize_database
    from llm_memory.models import Handoff
    from llm_memory.schemas import AgentSessionCreate, HandoffCreate
    from llm_memory.scope import project_for_directory
    from llm_memory.service import DEFAULT_OWNER

MAX_HANDOFF_CHARS = 90_000
MAX_MESSAGE_CHARS = 40_000
SPOOL_ROOT = Path(tempfile.gettempdir()) / "llm-memory-codex"


def _read_event() -> dict[str, Any]:
    value = json.load(sys.stdin)
    if not isinstance(value, dict):
        raise TypeError("Codex hook input must be a JSON object")
    return value


def _key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _paths(session_id: str) -> tuple[Path, Path]:
    key = _key(session_id)
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


def _capture_turn(event: dict[str, Any], role: str, content: object) -> None:
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not isinstance(content, str) or not content.strip():
        return
    spool, meta_path = _paths(session_id)
    if _load_meta(meta_path) is None:
        return
    turn_id = event.get("turn_id")
    if not isinstance(turn_id, str):
        turn_id = "unknown"
    # A turn can produce multiple Stop events; write each role once per turn.
    try:
        for line in spool.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item.get("turn_id") == turn_id and item.get("role") == role:
                return
    except (OSError, json.JSONDecodeError):
        pass
    item = {"turn_id": turn_id, "role": role, "content": content[:MAX_MESSAGE_CHARS]}
    _write_private(spool, json.dumps(item, ensure_ascii=False) + "\n", append=True)


def _format_transcript(spool: Path, session_id: str) -> str:
    entries: list[dict[str, Any]] = []
    try:
        lines = spool.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str):
            entries.append(item)
    rendered = [
        f"{entry['role'].capitalize()} (turn {entry['turn_id']}):\n{entry['content']}"
        for entry in entries
    ]
    transcript = "\n\n---\n\n".join(rendered)
    if not transcript:
        transcript = "Nenhuma mensagem foi capturada nesta sessão do Codex."
    prefix = f"Transcrição automática da sessão Codex {session_id}:\n\n"
    combined = prefix + transcript
    if len(combined) > MAX_HANDOFF_CHARS:
        combined = prefix + "[início da conversa omitido por limite de tamanho]\n\n" + combined[-(
            MAX_HANDOFF_CHARS - len(prefix) - 60
        ):]
    return combined


def _session_start(event: dict[str, Any]) -> None:
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    initialize_database()
    cwd = event.get("cwd")
    directory = Path(cwd) if isinstance(cwd, str) else ROOT
    project_id = project_for_directory(directory)
    external_id = f"codex:{session_id}:{uuid.uuid4().hex}"
    spool, meta_path = _paths(session_id)
    _write_private(spool, "")

    with SessionLocal() as session:
        current = create_session(
            session,
            DEFAULT_OWNER,
            AgentSessionCreate(
                external_id=external_id,
                workspace_id="default",
                project_id=project_id,
                agent_id="codex",
                title=f"Codex: {project_id}",
            ),
        )
        latest = session.scalar(
            select(Handoff)
            .where(Handoff.owner_id == DEFAULT_OWNER, Handoff.project_id == project_id)
            .order_by(desc(Handoff.created_at))
            .limit(1)
        )
        previous_summary = latest.summary if latest else ""

    metadata = {
        "service_session_id": current.id,
        "project_id": project_id,
        "external_id": external_id,
    }
    _write_private(meta_path, json.dumps(metadata))
    if previous_summary:
        context = (
            f"Saved conversation from the latest Codex session in project {project_id}:\n"
            f"{previous_summary[-7_000:]}"
        )
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }))


def _session_end(event: dict[str, Any]) -> None:
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    spool, meta_path = _paths(session_id)
    metadata = _load_meta(meta_path)
    if metadata is None or not isinstance(metadata.get("service_session_id"), str):
        return
    initialize_database()
    with SessionLocal() as session:
        create_handoff(
            session,
            DEFAULT_OWNER,
            metadata["service_session_id"],
            HandoffCreate(summary=_format_transcript(spool, session_id)),
        )
    spool.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)


def main() -> None:
    event = _read_event()
    if get_settings().storage_backend == "markdown":
        from llm_memory.capture import capture_event
        result = capture_event(event)
        print(json.dumps(result or {}))
        return
    hook_name = event.get("hook_event_name")
    if hook_name == "SessionStart":
        _session_start(event)
    elif hook_name == "UserPromptSubmit":
        _capture_turn(event, "user", event.get("prompt"))
    elif hook_name == "Stop":
        _capture_turn(event, "assistant", event.get("last_assistant_message"))
        print("{}")
    elif hook_name == "SessionEnd":
        _session_end(event)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - hooks must not block the conversation
        print(f"llm-memory Codex hook failed: {exc}", file=sys.stderr)
