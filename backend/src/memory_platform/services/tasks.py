"""Shared task ownership and durable replay, all within the caller transaction."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import Connection, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from memory_platform.api.task_schemas import (
    CheckpointRequest,
    CreateEvent,
    EventRequest,
    HandoffRequest,
    SessionRequest,
    TaskCreateRequest,
)
from memory_platform.auth.principal import Principal
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.tables import scopes
from memory_platform.task_tables import task_events, task_sessions, tasks


def _not_found() -> AppError:
    return AppError(ErrorCode.not_found, "Not found.")


def _conflict() -> AppError:
    return AppError(ErrorCode.version_conflict, "Task ownership, version or sequence changed.")


def _scope(conn: Connection, principal: Principal, scope_id: UUID, cap: Capability) -> None:
    principal.require(scope_id, cap)
    if (
        conn.execute(
            select(scopes.c.id).where(
                scopes.c.id == scope_id, scopes.c.workspace_id == principal.workspace_id
            )
        ).first()
        is None
    ):
        raise _not_found()


def _session(
    conn: Connection,
    principal: Principal,
    session_id: UUID,
    *,
    write: bool = False,
    lock: bool = False,
) -> dict[str, Any]:
    query = select(task_sessions).where(
        task_sessions.c.id == session_id, task_sessions.c.workspace_id == principal.workspace_id
    )
    row = conn.execute(query.with_for_update() if lock else query).mappings().first()
    if row is None:
        raise _not_found()
    principal.require(row["scope_id"], Capability.task_write if write else Capability.task_read)
    if write and (
        row["actor_id"] != principal.actor_id or row["credential_id"] != principal.credential_id
    ):
        raise AppError(ErrorCode.forbidden, "Session belongs to another credential.")
    return dict(row)


def _session_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "observed_status": "stale"
        if row["last_seen_at"] < datetime.now(UTC) - timedelta(seconds=180)
        else "active",
        "heartbeat_interval_seconds": 60,
    }


def register_session(conn: Connection, principal: Principal, req: SessionRequest) -> dict[str, Any]:
    _scope(conn, principal, req.scope_id, Capability.task_write)
    conn.execute(
        pg_insert(task_sessions)
        .values(
            id=uuid4(),
            workspace_id=principal.workspace_id,
            scope_id=req.scope_id,
            actor_id=principal.actor_id,
            credential_id=principal.credential_id,
            client=req.client,
            external_id=req.external_id,
        )
        .on_conflict_do_nothing(constraint="uq_task_sessions_identity")
    )
    row = (
        conn.execute(
            select(task_sessions).where(
                task_sessions.c.credential_id == principal.credential_id,
                task_sessions.c.scope_id == req.scope_id,
                task_sessions.c.client == req.client,
                task_sessions.c.external_id == req.external_id,
            )
        )
        .mappings()
        .one()
    )
    return _session_view(dict(row))


def session_detail(
    conn: Connection, principal: Principal, session_id: UUID, *, heartbeat: bool = False
) -> dict[str, Any]:
    row = _session(conn, principal, session_id, write=heartbeat, lock=heartbeat)
    if heartbeat:
        row["last_seen_at"] = datetime.now(UTC)
        conn.execute(
            update(task_sessions)
            .where(task_sessions.c.id == session_id)
            .values(last_seen_at=row["last_seen_at"])
        )
    return _session_view(row)


def list_sessions(
    conn: Connection, principal: Principal, scope_id: UUID, limit: int = 100
) -> list[dict[str, Any]]:
    _scope(conn, principal, scope_id, Capability.task_read)
    rows = conn.execute(
        select(task_sessions)
        .where(
            task_sessions.c.workspace_id == principal.workspace_id,
            task_sessions.c.scope_id == scope_id,
        )
        .order_by(task_sessions.c.created_at.desc(), task_sessions.c.id)
        .limit(limit)
    ).mappings()
    return [_session_view(dict(row)) for row in rows]


def _task(
    conn: Connection,
    principal: Principal,
    task_id: UUID,
    *,
    write: bool = False,
    lock: bool = False,
    include_deleted: bool = False,
) -> dict[str, Any]:
    query = select(tasks).where(
        tasks.c.id == task_id, tasks.c.workspace_id == principal.workspace_id
    )
    row = conn.execute(query.with_for_update() if lock else query).mappings().first()
    if row is None:
        raise _not_found()
    principal.require(row["scope_id"], Capability.task_write if write else Capability.task_read)
    if row["deleted_at"] is not None and not include_deleted:
        raise _not_found()
    return dict(row)


def read_task(conn: Connection, principal: Principal, task_id: UUID) -> dict[str, Any]:
    return _task(conn, principal, task_id)


def list_tasks(
    conn: Connection, principal: Principal, scope_id: UUID, limit: int = 100
) -> list[dict[str, Any]]:
    _scope(conn, principal, scope_id, Capability.task_read)
    rows = conn.execute(
        select(tasks)
        .where(
            tasks.c.workspace_id == principal.workspace_id,
            tasks.c.scope_id == scope_id,
            tasks.c.deleted_at.is_(None),
        )
        .order_by(tasks.c.updated_at.desc(), tasks.c.id)
        .limit(limit)
    ).mappings()
    return [dict(row) for row in rows]


def list_events(
    conn: Connection, principal: Principal, task_id: UUID, limit: int = 100
) -> list[dict[str, Any]]:
    _task(conn, principal, task_id)
    return [
        dict(row)
        for row in conn.execute(
            select(
                task_events.c.id,
                task_events.c.task_id,
                task_events.c.session_id,
                task_events.c.sequence,
                task_events.c.kind,
                task_events.c.checkpoint,
                task_events.c.created_at,
            )
            .where(task_events.c.task_id == task_id)
            .order_by(task_events.c.created_at.desc(), task_events.c.id)
            .limit(limit)
        ).mappings()
    ]


def _fingerprint(req: CreateEvent, kind: str, task_id: UUID | None) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "kind": kind,
                "task_id": str(task_id) if task_id else None,
                "payload": req.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _replay(
    conn: Connection, principal: Principal, session: dict[str, Any], req: CreateEvent, digest: str
) -> dict[str, Any] | None:
    # A global event id collision never replays another caller's result.
    row = (
        conn.execute(select(task_events).where(task_events.c.id == req.event_id)).mappings().first()
    )
    if row is not None:
        if (
            row["workspace_id"] != principal.workspace_id
            or row["session_id"] != session["id"]
            or row["payload_hash"] != digest
        ):
            raise AppError(ErrorCode.idempotency_mismatch, "Event id was already used.")
        return {
            **row["result"],
            "task_id": UUID(row["result"]["task_id"]),
            "event_id": UUID(row["result"]["event_id"]),
        }
    if req.sequence <= session["last_sequence"]:
        raise _conflict()
    return None


def _record(
    conn: Connection,
    principal: Principal,
    session: dict[str, Any],
    req: CreateEvent,
    digest: str,
    kind: str,
    row: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "task_id": row["id"],
        "version": row["version"],
        "generation": row["generation"],
        "event_id": req.event_id,
    }
    serialized = {
        key: str(value) if isinstance(value, UUID) else value for key, value in result.items()
    }
    conn.execute(
        insert(task_events).values(
            id=req.event_id,
            workspace_id=principal.workspace_id,
            scope_id=row["scope_id"],
            task_id=row["id"],
            session_id=session["id"],
            sequence=req.sequence,
            kind=kind,
            payload_hash=digest,
            result=serialized,
            checkpoint=checkpoint,
        )
    )
    conn.execute(
        update(task_sessions)
        .where(task_sessions.c.id == session["id"])
        .values(last_sequence=req.sequence, last_seen_at=datetime.now(UTC))
    )
    return result


def _lock_event(conn: Connection, event_id: UUID) -> None:
    # Serialize cross-session use of the same id before locking session/task rows.
    lock_id = int.from_bytes(hashlib.sha256(event_id.bytes).digest()[:8], "big", signed=True)
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})


def create_task(
    conn: Connection, principal: Principal, req: TaskCreateRequest, *, settings: Any = None
) -> dict[str, Any]:
    _scope(conn, principal, req.scope_id, Capability.task_write)
    if settings is not None:
        from memory_platform.services.quotas import enforce_quota, lock_workspace

        lock_workspace(conn, principal.workspace_id)
    _lock_event(conn, req.event_id)
    session = _session(conn, principal, req.session_id, write=True, lock=True)
    if session["scope_id"] != req.scope_id:
        raise _not_found()
    digest = _fingerprint(req, "created", None)
    replay = _replay(conn, principal, session, req, digest)
    if replay is not None:
        # Authorization and deletion are checked before returning old metadata.
        _task(conn, principal, replay["task_id"], write=True)
        return replay
    task_id = req.task_id or uuid4()
    # Caller-chosen ids are useful for offline queues, but cannot replace a tombstone.
    exists = conn.execute(select(tasks.c.id).where(tasks.c.id == task_id)).first()
    if exists:
        raise _conflict()
    if settings is not None:
        enforce_quota(conn, principal.workspace_id, settings, "tasks")
    inserted = (
        conn.execute(
            pg_insert(tasks)
            .values(
                id=task_id,
                workspace_id=principal.workspace_id,
                scope_id=req.scope_id,
                title=req.title,
                goal=req.goal,
                owner_session_id=session["id"],
            )
            .on_conflict_do_nothing(index_elements=[tasks.c.id])
            .returning(tasks)
        )
        .mappings()
        .first()
    )
    if inserted is None:
        raise _conflict()
    row = dict(inserted)
    return _record(
        conn,
        principal,
        session,
        req,
        digest,
        "created",
        row,
        {"title": req.title, "goal": req.goal},
    )


def write_event(
    conn: Connection, principal: Principal, task_id: UUID, req: EventRequest, kind: str
) -> dict[str, Any]:
    _lock_event(conn, req.event_id)
    session = _session(conn, principal, req.session_id, write=True, lock=True)
    row = _task(conn, principal, task_id, write=True, lock=True, include_deleted=True)
    if session["scope_id"] != row["scope_id"]:
        raise _not_found()
    digest = _fingerprint(req, kind, task_id)
    replay = _replay(conn, principal, session, req, digest)
    if replay is not None:
        if row["deleted_at"] is not None and kind != "delete":
            raise _not_found()
        return replay
    if row["deleted_at"] is not None:
        raise _not_found()
    if req.expected_version != row["version"] or req.generation != row["generation"]:
        raise _conflict()
    change: dict[str, Any] = {}
    checkpoint: dict[str, Any] = {}
    if kind == "recover":
        if principal.actor_kind != ActorKind.owner or not principal.is_admin:
            raise AppError(ErrorCode.forbidden, "Owner administrator required.")
        change.update(
            owner_session_id=session["id"],
            handoff_session_id=None,
            generation=row["generation"] + 1,
        )
    elif kind == "accept_handoff":
        if row["handoff_session_id"] != session["id"]:
            raise _conflict()
        change.update(
            owner_session_id=session["id"],
            handoff_session_id=None,
            generation=row["generation"] + 1,
        )
    else:
        if row["owner_session_id"] != session["id"]:
            raise _conflict()
        if kind == "checkpoint":
            checkpoint = cast(CheckpointRequest, req).model_dump(
                mode="json",
                exclude_unset=True,
                exclude={"session_id", "event_id", "sequence", "expected_version", "generation"},
            )
            if any(value is None for value in checkpoint.values()):
                raise AppError(ErrorCode.validation_error, "Checkpoint fields cannot be null.")
            change.update(checkpoint)
        elif kind == "handoff":
            target = cast(HandoffRequest, req).target_session_id
            target_row = conn.execute(
                select(task_sessions.c.id).where(
                    task_sessions.c.id == target,
                    task_sessions.c.scope_id == row["scope_id"],
                    task_sessions.c.workspace_id == principal.workspace_id,
                )
            ).first()
            if target_row is None:
                raise _not_found()
            if target == session["id"]:
                raise AppError(ErrorCode.invalid_transition, "Handoff requires another session.")
            change["handoff_session_id"] = target
        elif kind == "delete":
            change.update(
                deleted_at=datetime.now(UTC),
                title="",
                goal="",
                summary="",
                current_step="",
                next_step="",
                blockers=[],
                artifact_refs=[],
                results=[],
                handoff_session_id=None,
            )
            conn.execute(
                update(task_events).where(task_events.c.task_id == task_id).values(checkpoint={})
            )
        else:
            raise AppError(ErrorCode.bad_request, "Unsupported task event.")
    change.update(version=row["version"] + 1, updated_at=datetime.now(UTC))
    conn.execute(update(tasks).where(tasks.c.id == task_id).values(**change))
    row.update(change)
    return _record(conn, principal, session, req, digest, kind, row, checkpoint)
