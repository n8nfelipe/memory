from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from llm_memory import (
    models,  # noqa: F401
    service,
)
from llm_memory.config import Settings
from llm_memory.db import Base


@pytest.fixture
def session_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[sessionmaker[Session]]:
    database_path = tmp_path / "test-memory.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE VIRTUAL TABLE memory_fts USING fts5 "
            "(memory_id UNINDEXED, owner_id UNINDEXED, scope_id UNINDEXED, content, tags)"
        ))
    monkeypatch.setattr(service, "engine", engine)
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: Settings(
            database_url=f"sqlite:///{database_path}",
            auth_required=False,
            host="127.0.0.1",
            port=8765,
            max_entries_per_owner=0,
            max_bytes_per_owner=0,
            default_ttl_days=None,
            allowed_kinds=frozenset(),
        ),
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Generator[Session]:
    with session_factory() as active_session:
        yield active_session
