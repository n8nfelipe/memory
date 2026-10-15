from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from llm_memory.config import get_settings


class Base(DeclarativeBase):
    pass


def _build_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite:"):
        path = url.removeprefix("sqlite:///")
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(connection: object, _record: object) -> None:
            cursor = connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def initialize_database() -> None:
    from alembic import command
    from alembic.config import Config
    from llm_memory import models  # noqa: F401

    package_dir = Path(__file__).resolve().parent
    migration_config = Config()
    migration_config.set_main_option("script_location", str(package_dir / "migrations"))
    migration_config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
    command.upgrade(migration_config, "head")
    if engine.dialect.name == "sqlite":
        db_path = Path(engine.url.database or "").expanduser()
        if db_path.exists():
            try:
                db_path.chmod(0o600)
            except OSError:
                pass
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5 "
                "(memory_id UNINDEXED, owner_id UNINDEXED, scope_id UNINDEXED, content, tags)"
            )


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
