"""Write wrapper checks call services directly, including concurrent key reclamation."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select, update

from memory_platform.auth.principal import Principal
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.idempotency import request_hash, run_write
from memory_platform.services.types import WriteOutcome
from memory_platform.tables import activity, operations, scopes
from tests.conftest import WorkspaceInfo


def principal_for(workspace: WorkspaceInfo) -> Principal:
    return Principal(
        workspace.owner_actor_id,
        ActorKind.owner,
        workspace.id,
        workspace.owner_credential_id,
        True,
        {workspace.personal_scope_id: frozenset(Capability)},
    )


def test_request_hash_is_canonical_and_operation_specific() -> None:
    expected = hashlib.sha256(
        json.dumps(
            {"operation": "remember", "payload": {"a": 1, "b": "မြန်မာ"}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert request_hash("remember", {"b": "မြန်မာ", "a": 1}) == expected
    assert request_hash("forget", {"a": 1, "b": "မြန်မာ"}) != expected


def test_replay_mismatch_and_rollback(engine: Engine, workspace: WorkspaceInfo) -> None:
    principal = principal_for(workspace)
    body = {"id": str(uuid4()), "scope_id": str(workspace.personal_scope_id), "version": 1}
    calls = 0

    def action() -> WriteOutcome:
        nonlocal calls
        calls += 1
        return WriteOutcome(201, body, "memory.remember", workspace.personal_scope_id, ())

    def write(payload: dict[str, str]) -> WriteOutcome:
        with engine.begin() as conn:
            return run_write(
                conn,
                principal,
                operation="remember",
                idempotency_key="one",
                payload=payload,
                request_id="write",
                ttl_hours=24,
                action=action,
            )

    first = write({"content": "private request text"})
    replay = write({"content": "private request text"})
    assert first.body == body | {"replayed": False}
    assert replay.body == body | {"replayed": True}
    assert replay.replayed is True and calls == 1
    with pytest.raises(AppError) as exc:
        write({"content": "different"})
    assert exc.value.code == ErrorCode.idempotency_mismatch
    with engine.connect() as conn:
        assert (
            conn.scalar(select(scopes.c.revision).where(scopes.c.id == workspace.personal_scope_id))
            == 1
        )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(activity)
                .where(activity.c.workspace_id == workspace.id)
            )
            == 1
        )
        stored = (
            conn.execute(select(operations).where(operations.c.actor_id == principal.actor_id))
            .mappings()
            .one()
        )
        assert "private request text" not in str(dict(stored))

    with pytest.raises(RuntimeError), engine.begin() as conn:
        run_write(
            conn,
            principal,
            operation="remember",
            idempotency_key="rolled-back",
            payload={},
            request_id="rollback",
            ttl_hours=24,
            action=action,
        )
        raise RuntimeError("force rollback")
    with engine.connect() as conn:
        assert (
            conn.scalar(
                select(func.count())
                .select_from(operations)
                .where(operations.c.idempotency_key == "rolled-back")
            )
            == 0
        )
        assert (
            conn.scalar(select(scopes.c.revision).where(scopes.c.id == workspace.personal_scope_id))
            == 1
        )


@pytest.mark.parametrize("expired", [False, True])
def test_concurrent_duplicate_and_expired_reclaim(
    engine: Engine,
    workspace: WorkspaceInfo,
    expired: bool,
) -> None:
    principal = principal_for(workspace)
    body = {"id": str(uuid4()), "version": 1}
    barrier = Barrier(2)

    def perform(key: str) -> WriteOutcome:
        with engine.begin() as conn:
            return run_write(
                conn,
                principal,
                operation="remember",
                idempotency_key=key,
                payload={},
                request_id="concurrent",
                ttl_hours=24,
                action=lambda: WriteOutcome(
                    201, body, "memory.remember", workspace.personal_scope_id, ()
                ),
            )

    if expired:
        perform("concurrent-key")
        with engine.begin() as conn:
            conn.execute(
                update(operations)
                .where(operations.c.actor_id == principal.actor_id)
                .values(expires_at=func.now() - timedelta(seconds=1))
            )

    def task() -> WriteOutcome:
        barrier.wait(timeout=10)
        return perform("concurrent-key")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: task(), range(2)))
    assert sorted(item.replayed for item in results) == [False, True]
    with engine.connect() as conn:
        assert conn.scalar(
            select(scopes.c.revision).where(scopes.c.id == workspace.personal_scope_id)
        ) == (2 if expired else 1)
        assert (
            conn.scalar(
                select(func.count())
                .select_from(operations)
                .where(operations.c.actor_id == principal.actor_id)
            )
            == 1
        )


def test_unkeyed_write_and_no_search_change(engine: Engine, workspace: WorkspaceInfo) -> None:
    principal = principal_for(workspace)
    with engine.begin() as conn:
        run_write(
            conn,
            principal,
            operation="remember",
            idempotency_key=None,
            payload={},
            request_id="dedupe",
            ttl_hours=24,
            action=lambda: WriteOutcome(200, {"id": str(uuid4())}, "memory.remember", None, ()),
        )
    with engine.connect() as conn:
        assert (
            conn.scalar(select(scopes.c.revision).where(scopes.c.id == workspace.personal_scope_id))
            == 0
        )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(activity)
                .where(activity.c.workspace_id == workspace.id)
            )
            == 1
        )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(operations)
                .where(operations.c.actor_id == principal.actor_id)
            )
            == 0
        )


def test_content_cannot_enter_stored_response(engine: Engine, workspace: WorkspaceInfo) -> None:
    with pytest.raises(ValueError), engine.begin() as conn:
        run_write(
            conn,
            replace(principal_for(workspace)),
            operation="remember",
            idempotency_key="unsafe",
            payload={},
            request_id="unsafe",
            ttl_hours=24,
            action=lambda: WriteOutcome(201, {"content": "private"}, "memory.remember", None, ()),
        )
