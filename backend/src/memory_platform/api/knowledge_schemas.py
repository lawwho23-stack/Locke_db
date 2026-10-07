"""Bounded inputs for document ingestion and source-grounded recall."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4000)
    scope_ids: list[UUID] | None = Field(default=None, max_length=100)
    limit: int = Field(default=8, ge=1, le=20)
    token_budget: int = Field(default=3000, ge=64, le=16000)
    semantic: bool = True
    session_id: UUID | None = None


class RecallItem(BaseModel):
    kind: Literal["memory", "source"]
    id: UUID
    scope_id: UUID
    version: int
    content: str
    citation: str
    trust: str
    score: float
    truncated: bool = False


class SessionEvent(BaseModel):
    sequence: int
    kind: str
    author: str
    content: str
    tool_name: str | None = None


class SessionContext(BaseModel):
    session_id: UUID
    scope_id: UUID
    summary: str = Field(max_length=16384)
    summary_version: int = 0
    recent_events: list[SessionEvent] = Field(default_factory=list, max_length=5)


class RecallResponse(BaseModel):
    items: list[RecallItem]
    context: str
    estimated_tokens: int
    token_estimator: str = "UTF-8 bytes / 3, rounded up; not a model tokenizer"
    semantic_used: bool
    truncated: bool
    warnings: list[str] = Field(default_factory=list)
    session: SessionContext | None = None


class SourceUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_id: UUID
    title: str = Field(min_length=1, max_length=300)
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(max_length=27962028)


class SourceReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(max_length=27962028)
