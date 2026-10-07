"""Selected capture is opt-in, credential-bound and never accepts hidden reasoning."""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Connection, insert, select, update

from memory_platform.auth.principal import Principal
from memory_platform.capture_tables import capture_events, session_summaries
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.tasks import _lock_event, _session

_SECRET = re.compile(
    r"(?i)\b(?:bearer\s+[a-z0-9._-]+|(?:mem_|sk-)[a-z0-9_-]{8,}|"
    r"(?:api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+)"
)


def redact(text: str) -> str:
    """Defense in depth for common secrets; callers must select safe excerpts."""
    return _SECRET.sub("[REDACTED]", text)


class SelectedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    sequence: int = Field(gt=0)
    kind: Literal["message", "tool_result"]
    author: Literal["user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=16384)
    tool_name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("content")
    @classmethod
    def bounded(cls, value: str) -> str:
        if len(value.encode()) > 16384 or "\x00" in value:
            raise ValueError("Selected content exceeds byte limit or contains NUL.")
        return value

    @model_validator(mode="after")
    def valid_author(self) -> "SelectedEvent":
        if (self.kind == "tool_result") != (self.author == "tool"):
            raise ValueError("Tool results require tool authorship.")
        if self.kind == "message" and self.tool_name is not None:
            raise ValueError("Messages cannot have tool_name.")
        return self


class SessionStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    summary: str = Field(max_length=16384)

    @field_validator("summary")
    @classmethod
    def bounded(cls, value: str) -> str:
        if len(value.encode()) > 16384 or "\x00" in value:
            raise ValueError("Summary exceeds byte limit or contains NUL.")
        return value


def append_event(
    conn: Connection, principal: Principal, session_id: UUID, req: SelectedEvent
) -> dict[str, Any]:
    _lock_event(conn, req.event_id)
    session = _session(conn, principal, session_id, write=True, lock=True)
    payload = req.model_dump(mode="json")
    payload["content"] = redact(req.content)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    existing = (
        conn.execute(select(capture_events).where(capture_events.c.id == req.event_id))
        .mappings()
        .first()
    )
    if existing is not None:
        if (
            existing["session_id"] != session_id
            or existing["workspace_id"] != principal.workspace_id
            or existing["payload_hash"] != digest
        ):
            raise AppError(ErrorCode.idempotency_mismatch, "Selected event id already used.")
        return {"id": existing["id"], "sequence": existing["sequence"], "deduplicated": True}
    previous = conn.execute(
        select(capture_events.c.sequence)
        .where(capture_events.c.session_id == session_id)
        .order_by(capture_events.c.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()
    if previous is not None and req.sequence <= previous:
        raise AppError(ErrorCode.version_conflict, "Selected event sequence changed.")
    conn.execute(
        insert(capture_events).values(
            id=req.event_id,
            workspace_id=principal.workspace_id,
            scope_id=session["scope_id"],
            session_id=session_id,
            sequence=req.sequence,
            kind=req.kind,
            author=req.author,
            content=payload["content"],
            tool_name=req.tool_name,
            payload_hash=digest,
        )
    )
    return {"id": req.event_id, "sequence": req.sequence, "deduplicated": False}


def list_events(
    conn: Connection, principal: Principal, session_id: UUID, limit: int = 100
) -> list[dict[str, Any]]:
    _session(conn, principal, session_id)
    return [
        dict(row)
        for row in conn.execute(
            select(
                capture_events.c.id,
                capture_events.c.session_id,
                capture_events.c.sequence,
                capture_events.c.kind,
                capture_events.c.author,
                capture_events.c.content,
                capture_events.c.tool_name,
                capture_events.c.created_at,
            )
            .where(capture_events.c.session_id == session_id)
            .order_by(capture_events.c.sequence.desc())
            .limit(limit)
        ).mappings()
    ]


def session_state(conn: Connection, principal: Principal, session_id: UUID) -> dict[str, Any]:
    _session(conn, principal, session_id)
    row = (
        conn.execute(select(session_summaries).where(session_summaries.c.session_id == session_id))
        .mappings()
        .first()
    )
    return dict(row) if row else {"session_id": session_id, "summary": "", "version": 0}


def update_state(
    conn: Connection, principal: Principal, session_id: UUID, req: SessionStateRequest
) -> dict[str, Any]:
    session = _session(conn, principal, session_id, write=True, lock=True)
    current = session_state(conn, principal, session_id)
    if current["version"] != req.expected_version:
        raise AppError(ErrorCode.version_conflict, "Session summary version changed.")
    values = {
        "summary": redact(req.summary),
        "version": req.expected_version + 1,
        "updated_at": datetime.now(UTC),
    }
    if req.expected_version == 0:
        conn.execute(
            insert(session_summaries).values(
                session_id=session_id,
                workspace_id=principal.workspace_id,
                scope_id=session["scope_id"],
                **values,
            )
        )
    else:
        conn.execute(
            update(session_summaries)
            .where(session_summaries.c.session_id == session_id)
            .values(**values)
        )
    return {"session_id": session_id, **values}
