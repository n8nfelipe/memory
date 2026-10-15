"""Add agent sessions, handoffs, contextual scopes, and optional embeddings."""

from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, String, Text, UniqueConstraint, inspect

from llm_memory.models import utcnow

revision = "0002_sessions_scopes_embeddings"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "memories" in existing_tables:
        existing_columns = {column["name"] for column in inspector.get_columns("memories")}
        with op.batch_alter_table("memories") as batch:
            for name in ("workspace_id", "project_id", "agent_id", "session_id"):
                if name not in existing_columns:
                    batch.add_column(Column(name, String(200 if name != "session_id" else 36), nullable=True))
        indexes = {item["name"] for item in inspect(bind).get_indexes("memories")}
        for name in ("workspace_id", "project_id", "agent_id", "session_id"):
            index_name = f"ix_memories_{name}"
            if index_name not in indexes:
                op.create_index(index_name, "memories", [name])

    if "memory_embeddings" not in existing_tables:
        op.create_table(
            "memory_embeddings",
            Column("memory_id", String(36), ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True),
            Column("model", String(200), nullable=False),
            Column("vector", Text, nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False, default=utcnow),
        )
    if "agent_sessions" not in existing_tables:
        op.create_table(
            "agent_sessions",
            Column("id", String(36), primary_key=True),
            Column("owner_id", String(200), nullable=False),
            Column("external_id", String(200), nullable=True),
            Column("workspace_id", String(200), nullable=False, server_default="default"),
            Column("project_id", String(200), nullable=False),
            Column("agent_id", String(200), nullable=False),
            Column("title", String(300), nullable=False, server_default=""),
            Column("status", String(30), nullable=False, server_default="active"),
            Column("started_at", DateTime(timezone=True), nullable=False, default=utcnow),
            Column("updated_at", DateTime(timezone=True), nullable=False, default=utcnow),
            Column("ended_at", DateTime(timezone=True), nullable=True),
            UniqueConstraint("owner_id", "external_id"),
        )
        for name in ("owner_id", "workspace_id", "project_id", "agent_id", "status"):
            op.create_index(f"ix_agent_sessions_{name}", "agent_sessions", [name])
    if "handoffs" not in existing_tables:
        op.create_table(
            "handoffs",
            Column("id", String(36), primary_key=True),
            Column("session_id", String(36), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False),
            Column("owner_id", String(200), nullable=False),
            Column("workspace_id", String(200), nullable=False),
            Column("project_id", String(200), nullable=False),
            Column("from_agent", String(200), nullable=False),
            Column("to_agent", String(200), nullable=True),
            Column("summary", Text, nullable=False),
            Column("decisions", Text, nullable=False, server_default="[]"),
            Column("blockers", Text, nullable=False, server_default="[]"),
            Column("next_steps", Text, nullable=False, server_default="[]"),
            Column("status", String(30), nullable=False, server_default="pending"),
            Column("claimed_by_agent", String(200), nullable=True),
            Column("claimed_at", DateTime(timezone=True), nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False, default=utcnow),
            Column("completed_at", DateTime(timezone=True), nullable=True),
        )
        for name in ("session_id", "owner_id", "workspace_id", "project_id", "status"):
            op.create_index(f"ix_handoffs_{name}", "handoffs", [name])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "handoffs" in existing_tables:
        op.drop_table("handoffs")
    if "agent_sessions" in existing_tables:
        op.drop_table("agent_sessions")
    if "memory_embeddings" in existing_tables:
        op.drop_table("memory_embeddings")
    if "memories" in existing_tables:
        existing_columns = {column["name"] for column in inspector.get_columns("memories")}
        with op.batch_alter_table("memories") as batch:
            for name in ("workspace_id", "project_id", "agent_id", "session_id"):
                index_name = f"ix_memories_{name}"
                if name in existing_columns:
                    if index_name in {item["name"] for item in inspector.get_indexes("memories")}:
                        batch.drop_index(index_name)
                    batch.drop_column(name)
