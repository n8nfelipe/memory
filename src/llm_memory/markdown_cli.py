from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from llm_memory.config import get_settings
from llm_memory.markdown_store import MarkdownStore
from llm_memory.schemas import CompactionPreviewRequest, MemoryImportRequest

app = typer.Typer(no_args_is_help=True, help="Manage session Markdown memory journals.")
token_app = typer.Typer(no_args_is_help=True)
app.add_typer(token_app, name="token")


@app.command()
def init(
    remote: str | None = typer.Option(None, help="Clone this remote into MEMORY_REPOSITORY."),
) -> None:
    """Initialize a local Git repository, or clone an existing remote."""
    store = MarkdownStore()
    if remote:
        if store.root.exists() and any(store.root.iterdir()):
            raise typer.BadParameter(
                "Clone destination must be empty; configure its origin with Git"
            )
        store.root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--", remote, str(store.root)], check=True)
    store.initialize()
    typer.echo(f"Session repository: {store.root}")


@app.command()
def serve(
    host: str | None = typer.Option(None), port: int | None = typer.Option(None, min=1, max=65535)
) -> None:
    """Run the local HTTP API without a database."""
    settings = get_settings()
    bind = host or settings.host
    if bind not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("Markdown storage supports loopback access only")
    MarkdownStore().initialize()
    uvicorn.run("llm_memory.api:app", host=bind, port=port or settings.port)


@app.command()
def mcp() -> None:
    """Run the local MCP server over stdio."""
    from llm_memory.markdown_mcp import run

    run()


@app.command()
def inspect() -> None:
    """Validate journals and report counts without printing contents."""
    with MarkdownStore().transaction() as state:
        memories = [m for m in state.memories.values() if m["status"] != "deleted"]
        typer.echo(f"Sessions: {len(state.sessions)}")
        typer.echo(
            f"Memories: {len(memories)} total, "
            f"{sum(m['status'] == 'active' for m in memories)} active"
        )


@app.command()
def policy() -> None:
    """Show configured storage location and local policies."""
    settings = get_settings()
    typer.echo(f"storage_backend: {settings.storage_backend}")
    typer.echo(f"repository: {settings.repository}")
    for key in (
        "max_entries_per_owner",
        "max_bytes_per_owner",
        "default_ttl_days",
        "allowed_kinds",
    ):
        typer.echo(f"{key}: {getattr(settings, key)}")
    typer.echo("search: lexical; deletion: tombstone (content retained in journals and Git)")


@app.command()
def export(output: Annotated[Path | None, typer.Option()] = None) -> None:
    """Export current memories and relations as versioned JSON."""
    text = json.dumps(MarkdownStore().export(), ensure_ascii=False, indent=2)
    if output:
        output.write_text(text, encoding="utf-8")
        typer.echo(f"Exported to {output}")
    else:
        typer.echo(text)


@app.command(name="import")
def import_command(input_path: Annotated[Path, typer.Option("--input")]) -> None:
    """Import an export as a new journal; use migrate-database to retain original sessions."""
    data = MemoryImportRequest.model_validate_json(input_path.read_text(encoding="utf-8"))
    typer.echo(json.dumps(MarkdownStore().import_data(data)))


@app.command("migrate-database")
def migrate_database(
    database_url: Annotated[
        str,
        typer.Option(help="Read-only source database URL. Existing source data is never modified."),
    ],
) -> None:
    """Migrate local sessions, memories, history, relations and handoffs to Markdown."""
    from llm_memory.markdown_migration import migrate_database as migrate

    typer.echo(json.dumps(migrate(MarkdownStore(), database_url)))


@app.command("compact-preview")
def compact_preview(
    memory_id: Annotated[list[str], typer.Option("--memory-id")],
    content: Annotated[str | None, typer.Option()] = None,
    content_file: Annotated[Path | None, typer.Option()] = None,
) -> None:
    text = content_file.read_text(encoding="utf-8") if content_file else content or ""
    result = MarkdownStore().preview(
        CompactionPreviewRequest(
            memory_ids=memory_id,
            proposed_content=text,
        )
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("compact-apply")
def compact_apply(proposal_id: Annotated[str, typer.Option()]) -> None:
    typer.echo(json.dumps(MarkdownStore().compact(proposal_id), ensure_ascii=False))


@app.command("purge-expired")
def purge_expired() -> None:
    """Hide expired memories and derivatives; original journal content remains."""
    typer.echo(f"Marked {MarkdownStore().purge_expired()} expired memories as deleted")


@app.command("capture-opencode")
def capture_opencode(
    input_path: Annotated[Path, typer.Option("--input")],
) -> None:
    """Persist one Opencode plugin payload (session.created/idle/deleted)."""
    from llm_memory.opencode_capture import main as capture_main

    typer.echo(json.dumps(capture_main(input_path) or {}, ensure_ascii=False))


@app.command()
def commit(
    session_id: Annotated[str, typer.Option()],
    message: Annotated[str, typer.Option()] = "Update memory session",
) -> None:
    """Commit only this session's file; does not push or include other staged files."""
    typer.echo(MarkdownStore().commit(session_id, message))


@token_app.command("create")
def token_create(owner_id: str = typer.Option(...), label: str = typer.Option("")) -> None:
    """Token authentication is available only in database mode."""
    if not owner_id.strip():
        raise typer.BadParameter("Owner identity cannot be empty")
    raise typer.BadParameter("Markdown storage is local only; tokens require database mode")


@token_app.command("revoke")
def token_revoke(token_id: str) -> None:
    raise typer.BadParameter("Markdown storage is local only; tokens require database mode")
