"""Authorized direct reads and keyset pagination over the effective state."""

import base64
import binascii
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Connection, and_, or_, select, tuple_
from sqlalchemy.engine import RowMapping

from memory_platform.api.schemas import (
    EvidenceOut,
    MemoryDetail,
    MemoryListResponse,
    MemoryOut,
    VersionOut,
)
from memory_platform.auth.principal import Principal
from memory_platform.core.policy import effective_state
from memory_platform.enums import Capability, MemoryState, MemoryType
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.access import fetch_memory_checked, readable_scope_ids
from memory_platform.services.evidence import current_source_evidence, source_evidence_rows
from memory_platform.tables import memories, memory_evidence, memory_versions


def _memory_out(row: RowMapping, now: datetime) -> MemoryOut:
    values = dict(row)
    values["state"] = effective_state(MemoryState(row["state"]), row["valid_until"], now)
    return MemoryOut.model_validate(values)


def get_memory(conn: Connection, principal: Principal, memory_id: UUID) -> MemoryDetail:
    row = fetch_memory_checked(conn, principal, memory_id, Capability.memory_read)
    if row["trust"] == "source_extracted" and not source_evidence_rows(conn, memory_id):
        raise AppError(ErrorCode.not_found, "Not found.")
    evidence = (
        conn.execute(
            select(memory_evidence)
            .where(memory_evidence.c.memory_id == memory_id)
            .order_by(memory_evidence.c.created_at, memory_evidence.c.id)
        )
        .mappings()
        .all()
    )
    versions = (
        conn.execute(
            select(memory_versions)
            .where(memory_versions.c.memory_id == memory_id)
            .order_by(memory_versions.c.version)
        )
        .mappings()
        .all()
    )
    return MemoryDetail(
        **_memory_out(row, datetime.now(UTC)).model_dump(),
        evidence=[
            EvidenceOut.model_validate(e)
            for e in [*evidence, *source_evidence_rows(conn, memory_id)]
        ],
        versions=[VersionOut.model_validate(v) for v in versions],
    )


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        if len(cursor) > 512:
            raise ValueError
        decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        data = json.loads(decoded)
        if not isinstance(data, dict) or set(data) != {"u", "i"}:
            raise ValueError
        if not isinstance(data["u"], str) or not isinstance(data["i"], str):
            raise ValueError
        dt = datetime.fromisoformat(data["u"])
        if dt.tzinfo is None:
            raise ValueError
        return dt, UUID(data["i"])
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError):
        raise AppError(ErrorCode.bad_request, "Invalid cursor.") from None


def list_memories(
    conn: Connection,
    principal: Principal,
    *,
    scope_ids: Sequence[UUID] | None,
    label: str | None,
    mtype: MemoryType | None,
    state: MemoryState,
    limit: int,
    cursor: str | None,
    q: str | None = None,
) -> MemoryListResponse:
    scope_filter = readable_scope_ids(principal, scope_ids)
    if state == MemoryState.deleted or not 1 <= limit <= 100:
        raise AppError(ErrorCode.validation_error, "Invalid list parameters.")
    now = datetime.now(UTC)
    stmt = select(memories).where(
        memories.c.workspace_id == principal.workspace_id,
        memories.c.scope_id.in_(scope_filter),
        memories.c.state != MemoryState.deleted.value,
        or_(memories.c.trust != "source_extracted", current_source_evidence()),
    )
    if state == MemoryState.active:
        stmt = stmt.where(
            memories.c.state == MemoryState.active.value,
            or_(memories.c.valid_until.is_(None), memories.c.valid_until > now),
        )
    elif state == MemoryState.expired:
        stmt = stmt.where(
            or_(
                memories.c.state == MemoryState.expired.value,
                and_(memories.c.state == MemoryState.active.value, memories.c.valid_until <= now),
            )
        )
    else:
        stmt = stmt.where(memories.c.state == state.value)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(memories.c.content.ilike("%" + escaped + "%", escape="\\"))
    if label is not None:
        stmt = stmt.where(memories.c.labels.contains([label]))
    if mtype is not None:
        stmt = stmt.where(memories.c.type == mtype.value)
    if cursor is not None:
        dt, mid = _decode_cursor(cursor)
        stmt = stmt.where(tuple_(memories.c.updated_at, memories.c.id) < tuple_(dt, mid))
    rows = (
        conn.execute(
            stmt.order_by(memories.c.updated_at.desc(), memories.c.id.desc()).limit(limit + 1)
        )
        .mappings()
        .all()
    )
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps({"u": last["updated_at"].isoformat(), "i": str(last["id"])}).encode()
            )
            .decode()
            .rstrip("=")
        )
    return MemoryListResponse(
        items=[_memory_out(row, now) for row in rows[:limit]], next_cursor=next_cursor
    )
