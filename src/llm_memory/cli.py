from __future__ import annotations

import os
from pathlib import Path

import typer
import uvicorn
from sqlalchemy import func, select

from llm_memory.config import get_settings
from llm_memory.db import SessionLocal, engine, initialize_database
from llm_memory.models import ApiToken, Memory
from llm_memory.service import create_token, purge_expired

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


@app.command("purge-expired")
def purge_expired_command() -> None:
    """Permanently remove expired memories and their derived summaries."""
    initialize_database()
    with SessionLocal() as session:
        removed = purge_expired(session)
    typer.echo(f"Removed {removed} expired memories")


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
