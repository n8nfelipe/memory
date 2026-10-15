from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from sqlalchemy import func, select

from llm_memory.config import get_settings
from llm_memory.db import SessionLocal, engine, initialize_database
from llm_memory.models import ApiToken, Memory, MemoryRelation
from llm_memory.schemas import MemoryImportRequest
from llm_memory.service import (
    DEFAULT_OWNER,
    apply_compaction,
    create_token,
    memory_dict,
    preview_compaction,
    purge_expired,
)

app = typer.Typer(no_args_is_help=True, help="Manage the local LLM memory service.")
token_app = typer.Typer(no_args_is_help=True, help="Create and revoke API tokens.")
app.add_typer(token_app, name="token")


def _validate_owner_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise typer.BadParameter("Owner identity cannot be empty")
    return value


@app.command()
def init() -> None:
    """Create the database and local data directory."""
    initialize_database()
    if engine.dialect.name == "sqlite":
        path = Path(engine.url.database or "").expanduser()
        if path.exists():
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        typer.echo(f"Initialized local memory database at {path}")
    else:
        typer.echo("Initialized memory database")


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Bind address (defaults to loopback)."),
    port: int | None = typer.Option(None, min=1, max=65535),
) -> None:
    """Run the HTTP API."""
    settings = get_settings()
    bind_host = host or settings.host
    if bind_host not in {"127.0.0.1", "localhost", "::1"} and not settings.auth_required:
        raise typer.BadParameter("Set MEMORY_AUTH_REQUIRED=true before binding outside loopback")
    initialize_database()
    uvicorn.run("llm_memory.api:app", host=bind_host, port=port or settings.port)


@app.command()
def mcp() -> None:
    """Run the local MCP server over stdio."""
    from llm_memory.mcp_server import run

    run()


@app.command()
def inspect() -> None:
    """Show database counts without printing memory contents."""
    initialize_database()
    with SessionLocal() as session:
        total = session.scalar(select(func.count()).select_from(Memory)) or 0
        active = session.scalar(select(func.count()).select_from(Memory).where(Memory.status == "active")) or 0
    typer.echo(f"Memories: {total} total, {active} active")


@app.command()
def policy() -> None:
    """Show effective retention and policy settings (no secrets)."""
    settings = get_settings()
    allowed = ",".join(sorted(settings.allowed_kinds)) if settings.allowed_kinds else "(all)"
    typer.echo(f"database_configured: {'yes' if settings.database_url else 'no'}")
    typer.echo(f"auth_required: {str(settings.auth_required).lower()}")
    typer.echo(f"host: {settings.host}")
    typer.echo(f"port: {settings.port}")
    typer.echo(f"max_entries_per_owner: {settings.max_entries_per_owner}")
    typer.echo(f"max_bytes_per_owner: {settings.max_bytes_per_owner}")
    typer.echo(f"default_ttl_days: {settings.default_ttl_days if settings.default_ttl_days else '(none)'}")
    typer.echo(f"allowed_kinds: {allowed}")


@app.command()
def export(
    output: Annotated[Path | None, typer.Option(help="Write JSON export here (default: stdout).")] = None,
) -> None:
    """Export owned memories and relations as versioned JSON (no grants/tokens)."""
    initialize_database()
    with SessionLocal() as session:
        memories = session.scalars(
            select(Memory).where(Memory.owner_id == DEFAULT_OWNER).order_by(Memory.id)
        ).all()
        memory_ids = {memory.id for memory in memories}
        relations = (
            session.scalars(
                select(MemoryRelation).where(
                    MemoryRelation.source_id.in_(memory_ids),
                    MemoryRelation.derived_id.in_(memory_ids),
                )
            ).all()
            if memory_ids
            else []
        )
        payload = {
            "schema_version": "1",
            "memories": [memory_dict(memory) for memory in memories],
            "relations": [
                {
                    "source_id": relation.source_id,
                    "derived_id": relation.derived_id,
                    "relation": relation.relation,
                }
                for relation in relations
            ],
        }
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if output is None:
        typer.echo(text)
    else:
        output.write_text(text, encoding="utf-8")
        typer.echo(f"Exported {len(payload['memories'])} memories to {output}")


@app.command(name="import")
def import_command(
    input_path: Annotated[Path, typer.Option("--input", help="Versioned JSON file created by export.")],
) -> None:
    """Import memories, preserving IDs and remapping private scopes to local owner."""
    from llm_memory.service import _index_memory

    raw = json.loads(input_path.read_text(encoding="utf-8"))
    data = MemoryImportRequest.model_validate(raw)
    initialize_database()
    settings = get_settings()
    with SessionLocal() as session:
        incoming_ids = {item.id for item in data.memories}
        existing = (
            session.scalars(select(Memory.id).where(Memory.id.in_(incoming_ids))).all()
            if incoming_ids
            else []
        )
        if existing:
            raise typer.BadParameter("One or more memory IDs already exist")
        if settings.allowed_kinds and any(item.kind not in settings.allowed_kinds for item in data.memories):
            raise typer.BadParameter("Import contains a memory kind blocked by policy")
        owner_count = session.scalar(
            select(func.count()).select_from(Memory).where(Memory.owner_id == DEFAULT_OWNER)
        ) or 0
        if settings.max_entries_per_owner and owner_count + len(data.memories) > settings.max_entries_per_owner:
            raise typer.BadParameter("Owner memory count limit would be exceeded")
        for item in data.memories:
            scope_id = (
                f"private:{DEFAULT_OWNER}" if item.scope_id.startswith("private:") else item.scope_id
            )
            memory = Memory(
                id=item.id,
                owner_id=DEFAULT_OWNER,
                scope_id=scope_id,
                content=item.content,
                kind=item.kind,
                source_type=item.source.type,
                source_ref=item.source.ref,
                confidence=item.confidence,
                tags=json.dumps(item.tags, ensure_ascii=False),
                status=item.status,
                expires_at=item.expires_at,
                created_at=item.created_at,
                updated_at=item.updated_at,
                version=item.version,
            )
            session.add(memory)
            session.flush()
            _index_memory(session, memory)
        seen: set[tuple[str, str, str]] = set()
        relation_count = 0
        for relation in data.relations:
            source_id = relation.get("source_id", "")
            derived_id = relation.get("derived_id", "")
            relation_type = relation.get("relation", "summarized_from")
            if source_id not in incoming_ids or derived_id not in incoming_ids:
                continue
            if relation_type not in {"summarized_from", "supersedes", "duplicates", "conflicts_with"}:
                raise typer.BadParameter(f"Unsupported relation type: {relation_type}")
            key = (source_id, derived_id, relation_type)
            if key in seen:
                raise typer.BadParameter("Duplicate relation in import")
            seen.add(key)
            session.add(
                MemoryRelation(
                    id=str(uuid.uuid4()),
                    source_id=source_id,
                    derived_id=derived_id,
                    relation=relation_type,
                )
            )
            relation_count += 1
        session.commit()
    typer.echo(f"Imported {len(data.memories)} memories, {relation_count} relations")


@app.command(name="compact-preview")
def compact_preview(
    memory_id: Annotated[list[str], typer.Option("--memory-id", help="Source memory ID (repeat flag).")],
    content: Annotated[str | None, typer.Option(help="Proposed summary text.")] = None,
    content_file: Annotated[
        Path | None, typer.Option(help="Read proposed summary from file.")
    ] = None,
) -> None:
    """Preview a compaction without archiving sources."""
    proposed = content_file.read_text(encoding="utf-8") if content_file else (content or "")
    if not proposed.strip():
        raise typer.BadParameter("Provide --content or --content-file with non-empty text")
    if len(set(memory_id)) != len(memory_id):
        raise typer.BadParameter("Source IDs must be unique")
    initialize_database()
    with SessionLocal() as session:
        proposal = preview_compaction(session, DEFAULT_OWNER, list(memory_id), proposed)
        ratio = (
            0.0
            if proposal.estimated_bytes_before == 0
            else 1.0 - proposal.estimated_bytes_after / proposal.estimated_bytes_before
        )
    typer.echo(f"id: {proposal.id}")
    typer.echo(f"bytes_before: {proposal.estimated_bytes_before}")
    typer.echo(f"bytes_after: {proposal.estimated_bytes_after}")
    typer.echo(f"reduction_ratio: {ratio:.4f}")
    typer.echo("Review sources before applying; this step changes nothing.")


@app.command(name="compact-apply")
def compact_apply(
    proposal_id: Annotated[str, typer.Option(help="Proposal ID from compact-preview.")],
) -> None:
    """Apply a reviewed compaction proposal (archives sources)."""
    initialize_database()
    with SessionLocal() as session:
        derived = apply_compaction(session, DEFAULT_OWNER, proposal_id)
    typer.echo(f"summary_id: {derived.id}")


@app.command("purge-expired")
def purge_expired_command() -> None:
    """Permanently remove expired memories and their derived summaries."""
    initialize_database()
    with SessionLocal() as session:
        removed = purge_expired(session)
    typer.echo(f"Removed {removed} expired memories")


@app.command("capture-opencode")
def capture_opencode_command(
    input_path: Annotated[Path, typer.Option("--input")],
) -> None:
    """Persist one Opencode plugin payload (session.created/idle/deleted)."""
    from llm_memory.opencode_capture import main as capture_main

    typer.echo(json.dumps(capture_main(input_path) or {}, ensure_ascii=False))


@token_app.command("create")
def token_create(
    owner_id: str = typer.Option(..., callback=_validate_owner_id, help="Owner identity for this token."),
    label: str = typer.Option("", help="Human-readable token label."),
) -> None:
    """Create a token. The secret is printed once."""
    initialize_database()
    with SessionLocal() as session:
        token_id, token = create_token(session, owner_id, label)
    typer.echo(f"id: {token_id}")
    typer.echo(f"token: {token}")
    typer.echo("Store the token securely; it cannot be displayed again.", err=True)


@token_app.command("revoke")
def token_revoke(token_id: str = typer.Argument(...)) -> None:
    """Revoke a token by its internal identifier."""
    initialize_database()
    with SessionLocal() as session:
        token = session.get(ApiToken, token_id)
        if token is None:
            raise typer.BadParameter("Token id not found")
        token.revoked = True
        session.commit()
    typer.echo("Token revoked")
