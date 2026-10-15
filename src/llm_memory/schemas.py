from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Confidence = Literal["asserted", "inferred", "unverified"]


class SourceInput(BaseModel):
    type: str = Field(default="manual", min_length=1, max_length=80)
    ref: str | None = Field(default=None, max_length=500)


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    kind: str = Field(default="fact", min_length=1, max_length=60)
    scope_id: str | None = Field(default=None, max_length=200)
    source: SourceInput = Field(default_factory=SourceInput)
    confidence: Confidence = "unverified"
    tags: list[str] = Field(default_factory=list, max_length=30)
    expires_at: datetime | None = None

    @field_validator("content")
    @classmethod
    def trim_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content cannot be blank")
        return value


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    kind: str | None = Field(default=None, min_length=1, max_length=60)
    confidence: Confidence | None = None
    tags: list[str] | None = Field(default=None, max_length=30)
    expires_at: datetime | None = None
    expected_version: int = Field(ge=1)

    @field_validator("content")
    @classmethod
    def trim_patch_content(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("content cannot be blank")
        return value


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    scope_id: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=5, ge=1, le=50)
    include_archived: bool = False


class CompactionPreviewRequest(BaseModel):
    memory_ids: list[str] = Field(min_length=2, max_length=100)
    proposed_content: str = Field(min_length=1, max_length=100_000)


class GrantCreate(BaseModel):
    recipient_id: str = Field(min_length=1, max_length=200)
    role: Literal["reader", "editor"] = "reader"
    expires_at: datetime | None = None


class ConflictResolutionRequest(BaseModel):
    related_memory_id: str = Field(min_length=1, max_length=36)
    decision: Literal["keep_memory", "keep_related", "keep_both"]


class MemoryImportItem(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    scope_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    kind: str = Field(default="fact", min_length=1, max_length=60)
    source: SourceInput = Field(default_factory=SourceInput)
    confidence: Confidence = "unverified"
    tags: list[str] = Field(default_factory=list, max_length=30)
    status: Literal["active", "archived", "conflict"] = "active"
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)


class MemoryImportRequest(BaseModel):
    schema_version: Literal["1"]
    memories: list[MemoryImportItem] = Field(max_length=10_000)
    relations: list[dict[str, str]] = Field(default_factory=list, max_length=20_000)


class MemoryOut(BaseModel):
    id: str
    owner_id: str
    scope_id: str
    content: str
    kind: str
    source: SourceInput
    confidence: Confidence
    tags: list[str]
    status: str
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int


class MemoryCreated(BaseModel):
    memory: MemoryOut
    warnings: list[str] = Field(default_factory=list)


class SearchHit(BaseModel):
    memory: MemoryOut
    score: float
    reason: str


class SearchResponse(BaseModel):
    results: list[SearchHit]


class CompactionPreviewOut(BaseModel):
    id: str
    memory_ids: list[str]
    proposed_content: str
    estimated_bytes_before: int
    estimated_bytes_after: int
    reduction_ratio: float
    status: str


class GrantOut(BaseModel):
    id: str
    scope_id: str
    recipient_id: str
    role: str
    expires_at: datetime | None
    revoked: bool


class HistoryEntry(BaseModel):
    version: int
    content: str
    changed_by: str
    created_at: datetime


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
