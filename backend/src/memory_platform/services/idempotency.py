"""Transactional write replay, scope revision and content-free activity records."""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from memory_platform.auth.principal import Principal
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.types import WriteOutcome
from memory_platform.tables import activity, operations, scopes

# Write responses contain metadata only. An accidental text field must fail before storage.
_RESPONSE_FIELDS = frozenset(
    {
        "id",
        "scope_id",
        "state",
        "version",
        "trust",
        "indexing_status",
        "deduplicated",
        "superseded_id",
        "conflict_with_id",
        "replayed",
    }
)


def request_hash(operation: str, payload: Mapping[str, Any]) -> str:
    """Hash canonical request JSON; store the digest, never the payload."""
    canonical = json.dumps(
        {"operation": operation, "payload": dict(payload)}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_write(
    conn: Connection,
    principal: Principal,
    *,
    operation: str,
    idempotency_key: str | None,
    payload: Mapping[str, Any],
    request_id: str,
    ttl_hours: int,
    action: Callable[[], WriteOutcome],
) -> WriteOutcome:
    """Run a write once, inside the caller's transaction; never commit here.

    Routes must authorize the requested scope and target before calling this wrapper.
    A replay skips the action, revision increment and activity insert entirely.
    """
    operation_id: UUID | None = None
    if idempotency_key is not None:
        digest = request_hash(operation, payload)
        identity = (
            operations.c.actor_id == principal.actor_id,
            operations.c.operation == operation,
            operations.c.idempotency_key == idempotency_key,
        )
        while operation_id is None:
            # The unique index waits for an uncommitted duplicate to finish.
            operation_id = conn.scalar(
                pg_insert(operations)
                .values(
                    actor_id=principal.actor_id,
                    workspace_id=principal.workspace_id,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    request_hash=digest,
                    expires_at=func.now() + timedelta(hours=ttl_hours),
                )
                .on_conflict_do_nothing(constraint="uq_operations_actor_op_key")
                .returning(operations.c.id)
            )
            if operation_id is not None:
                break
            previous = (
                conn.execute(
                    select(operations, (operations.c.expires_at <= func.now()).label("expired"))
                    .where(*identity)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if previous is None:
                # Another transaction reclaimed an expired row while we waited on its lock.
                continue
            if previous["expired"]:
                conn.execute(delete(operations).where(operations.c.id == previous["id"]))
                continue
            if previous["request_hash"] != digest:
                raise AppError(
                    ErrorCode.idempotency_mismatch, "Idempotency key used for a different request."
                )
            if previous["response_status"] is None or previous["response_body"] is None:
                raise AppError(ErrorCode.dependency_unavailable, "Write result is not available.")
            body = dict(previous["response_body"]) | {"replayed": True}
            return WriteOutcome(
                previous["response_status"], body, operation, None, (), replayed=True
            )

    outcome = action()
    if not set(outcome.body).issubset(_RESPONSE_FIELDS):
        raise ValueError("Write response contains unsupported fields.")
    # Copy through JSON so stored metadata cannot share mutable values with the action.
    body = json.loads(json.dumps(outcome.body)) | {"replayed": False}
    if operation_id is not None:
        conn.execute(
            update(operations)
            .where(operations.c.id == operation_id)
            .values(
                response_status=outcome.status_code,
                response_body=body,
            )
        )
    if outcome.scope_id is not None:
        bump_scope_revision(conn, outcome.scope_id)
    record_activity(
        conn,
        workspace_id=principal.workspace_id,
        actor_id=principal.actor_id,
        action=outcome.action,
        target_ids=outcome.target_ids,
        scope_id=outcome.scope_id,
        request_id=request_id,
        result="ok",
    )
    return replace(outcome, body=body, replayed=False)


def bump_scope_revision(conn: Connection, scope_id: UUID) -> int:
    """Increment the searchable-data revision atomically, returning its new value."""
    value = conn.execute(
        update(scopes)
        .where(scopes.c.id == scope_id)
        .values(revision=scopes.c.revision + 1)
        .returning(scopes.c.revision)
    ).scalar_one()
    return int(value)


def record_activity(
    conn: Connection,
    *,
    workspace_id: UUID,
    actor_id: UUID | None,
    action: str,
    target_ids: Sequence[UUID],
    scope_id: UUID | None,
    request_id: str,
    result: str,
    latency_ms: int | None = None,
) -> None:
    """Insert metadata only; request bodies and responses do not belong in the audit log."""
    conn.execute(
        insert(activity).values(
            workspace_id=workspace_id,
            actor_id=actor_id,
            action=action,
            target_ids=list(target_ids),
            scope_id=scope_id,
            request_id=request_id,
            result=result,
            latency_ms=latency_ms,
        )
    )
