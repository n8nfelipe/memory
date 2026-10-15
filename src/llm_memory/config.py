from __future__ import annotations

import os
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


def get_settings() -> Settings:
    default_db = (Path.home() / ".llm-memory" / "memory.db").as_posix()
    database_url = os.getenv("MEMORY_DATABASE_URL", f"sqlite:///{default_db}")
    if database_url.startswith("sqlite:///") and "~" in database_url:
        database_url = database_url.replace("~", str(Path.home()), 1)
    ttl_value = os.getenv("MEMORY_DEFAULT_TTL_DAYS", "")
    return Settings(
        database_url=database_url,
        auth_required=os.getenv("MEMORY_AUTH_REQUIRED", "false").lower() in {"1", "true", "yes"},
        host=os.getenv("MEMORY_HOST", "127.0.0.1"),
        port=int(os.getenv("MEMORY_PORT", "8765")),
        max_entries_per_owner=max(0, int(os.getenv("MEMORY_MAX_ENTRIES_PER_OWNER", "0"))),
        max_bytes_per_owner=max(0, int(os.getenv("MEMORY_MAX_BYTES_PER_OWNER", "0"))),
        default_ttl_days=max(1, int(ttl_value)) if ttl_value else None,
        allowed_kinds=frozenset(
            value.strip() for value in os.getenv("MEMORY_ALLOWED_KINDS", "").split(",") if value.strip()
        ),
    )
