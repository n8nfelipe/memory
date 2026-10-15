from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    auth_required: bool
    host: str
    port: int
    max_entries_per_owner: int
    max_bytes_per_owner: int
    default_ttl_days: int | None
    allowed_kinds: frozenset[str]
    storage_backend: str = "database"
    repository: Path = Path.home() / ".llm-memory" / "sessions"
    embedding_url: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key: str | None = None


def get_settings() -> Settings:
    config_path = Path(os.getenv("MEMORY_CONFIG", "memory.toml")).resolve()
    config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    storage = config.get("storage", {})
    backend = os.getenv(
        "MEMORY_STORAGE_BACKEND",
        storage.get("backend", "database" if os.getenv("MEMORY_DATABASE_URL") else "markdown"),
    )
    if backend not in {"markdown", "database"}:
        raise ValueError("MEMORY_STORAGE_BACKEND must be markdown or database")
    repository = Path(
        os.getenv(
            "MEMORY_REPOSITORY",
            storage.get("repository", str(Path.home() / ".llm-memory" / "sessions")),
        )
    ).expanduser()
    if not repository.is_absolute():
        repository = config_path.parent / repository
    default_db = (Path.home() / ".llm-memory" / "memory.db").as_posix()
    database_url = os.getenv("MEMORY_DATABASE_URL", f"sqlite:///{default_db}")
    if database_url.startswith("sqlite:///") and "~" in database_url:
        database_url = database_url.replace("~", str(Path.home()), 1)
    ttl_value = os.getenv("MEMORY_DEFAULT_TTL_DAYS", "")
    return Settings(
        database_url=database_url,
        storage_backend=backend,
        repository=repository,
        auth_required=os.getenv("MEMORY_AUTH_REQUIRED", "false").lower() in {"1", "true", "yes"},
        host=os.getenv("MEMORY_HOST", "127.0.0.1"),
        port=int(os.getenv("MEMORY_PORT", "8765")),
        max_entries_per_owner=max(0, int(os.getenv("MEMORY_MAX_ENTRIES_PER_OWNER", "0"))),
        max_bytes_per_owner=max(0, int(os.getenv("MEMORY_MAX_BYTES_PER_OWNER", "0"))),
        default_ttl_days=max(1, int(ttl_value)) if ttl_value else None,
        allowed_kinds=frozenset(
            value.strip()
            for value in os.getenv("MEMORY_ALLOWED_KINDS", "").split(",")
            if value.strip()
        ),
        embedding_url=os.getenv("MEMORY_EMBEDDING_URL") or None,
        embedding_model=os.getenv("MEMORY_EMBEDDING_MODEL", "text-embedding-3-small"),
        embedding_api_key=os.getenv("MEMORY_EMBEDDING_API_KEY") or None,
    )
