from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from llm_memory.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Memory(Base):
    __tablename__ = "memories"
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {  # noqa: RUF012
        "version_id_col": version,
        "version_id_generator": False,
    }

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    scope_id: Mapped[str] = mapped_column(String(200), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(200), index=True, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(200), index=True, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(200), index=True, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(60), default="fact")
    source_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confidence: Mapped[str] = mapped_column(String(30), default="unverified")
    tags: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryVersion(Base):
    __tablename__ = "memory_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryRelation(Base):
    __tablename__ = "memory_relations"
    __table_args__ = (UniqueConstraint("source_id", "derived_id", "relation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    derived_id: Mapped[str] = mapped_column(String(36), index=True)
    relation: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Grant(Base):
    __tablename__ = "grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(200), index=True)
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    recipient_id: Mapped[str] = mapped_column(String(200), index=True)
    role: Mapped[str] = mapped_column(String(20))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(100), default="")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CompactionProposal(Base):
    __tablename__ = "compaction_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    scope_id: Mapped[str] = mapped_column(String(200), index=True)
    source_ids: Mapped[str] = mapped_column(Text)
    proposed_content: Mapped[str] = mapped_column(Text)
    estimated_bytes_before: Mapped[int] = mapped_column(Integer)
    estimated_bytes_after: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryEmbedding(Base):
    __tablename__ = "memory_embeddings"

    memory_id: Mapped[str] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(200))
    vector: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (UniqueConstraint("owner_id", "external_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    workspace_id: Mapped[str] = mapped_column(String(200), default="default", index=True)
    project_id: Mapped[str] = mapped_column(String(200), index=True)
    agent_id: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Handoff(Base):
    __tablename__ = "handoffs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    workspace_id: Mapped[str] = mapped_column(String(200), index=True)
    project_id: Mapped[str] = mapped_column(String(200), index=True)
    from_agent: Mapped[str] = mapped_column(String(200))
    to_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    summary: Mapped[str] = mapped_column(Text)
    decisions: Mapped[str] = mapped_column(Text, default="[]")
    blockers: Mapped[str] = mapped_column(Text, default="[]")
    next_steps: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    claimed_by_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
