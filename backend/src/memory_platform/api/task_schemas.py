"""Bounded authored checkpoints; never accept arbitrary transcript payloads."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

TaskStatus = Literal["planned", "running", "blocked", "paused", "completed", "cancelled"]
ShortText = Annotated[str, Field(max_length=2000)]
BoundedList = Annotated[list[ShortText], Field(max_length=32)]


class TaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionRequest(TaskModel):
    scope_id: UUID
    client: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=200)


class CreateEvent(TaskModel):
    session_id: UUID
    event_id: UUID
    sequence: int = Field(ge=1, le=9223372036854775807)


class TaskCreateRequest(CreateEvent):
    scope_id: UUID
    task_id: UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=4000)


class EventRequest(CreateEvent):
    expected_version: int = Field(ge=1, le=9223372036854775807)
    generation: int = Field(ge=1, le=9223372036854775807)


class CheckpointRequest(EventRequest):
    status: TaskStatus | None = None
    summary: str | None = Field(default=None, max_length=4000)
    current_step: ShortText | None = None
    next_step: ShortText | None = None
    blockers: BoundedList | None = None
    artifact_refs: BoundedList | None = None
    results: BoundedList | None = None


class HandoffRequest(EventRequest):
    target_session_id: UUID
