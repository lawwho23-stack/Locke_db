"""HTTP retries replay committed writes without repeating their effects."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, func, select

from memory_platform.tables import (
    activity,
    memories,
    memory_evidence,
    memory_versions,
    operations,
    scopes,
)
from tests.conftest import WorkspaceInfo


def test_concurrent_post_same_key_has_one_logical_write(
    engine: Engine,
    workspace: WorkspaceInfo,
    client_for_token: Callable[[str], TestClient],
) -> None:
    payload = {
        "scope_id": str(workspace.personal_scope_id),
        "type": "fact",
        "content": "Private concurrent fact",
    }
    barrier = Barrier(2)

    def post(_: int) -> Response:
        with client_for_token(workspace.owner_token) as client:
            barrier.wait(timeout=10)
            return client.post(
                "/v1/memories", json=payload, headers={"Idempotency-Key": "concurrent-post"}
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(post, range(2)))
    assert [result.status_code for result in results] == [201, 201]
    assert sorted(result.json()["replayed"] for result in results) == [False, True]
    assert results[0].json() | {"replayed": False} == results[1].json() | {"replayed": False}
    memory_id = UUID(results[0].json()["id"])
    with engine.connect() as conn:
        assert (
            conn.scalar(
                select(func.count())
                .select_from(memories)
                .where(memories.c.scope_id == workspace.personal_scope_id)
            )
            == 1
        )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(memory_versions)
                .where(memory_versions.c.memory_id == memory_id)
            )
            == 1
        )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(memory_evidence)
                .where(memory_evidence.c.memory_id == memory_id)
            )
            == 1
        )
        assert (
            conn.scalar(select(scopes.c.revision).where(scopes.c.id == workspace.personal_scope_id))
            == 1
        )
        logs = (
            conn.execute(select(activity).where(activity.c.workspace_id == workspace.id))
            .mappings()
            .all()
        )
        assert len(logs) == 1 and logs[0]["result"] == "ok"
        rows = (
            conn.execute(select(operations).where(operations.c.workspace_id == workspace.id))
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert "Private concurrent fact" not in str([dict(row) for row in rows + logs])


def test_patch_replay_precedes_stale_version_and_mismatch_is_safe(
    engine: Engine,
    workspace: WorkspaceInfo,
    owner_client: TestClient,
) -> None:
    created = owner_client.post(
        "/v1/memories",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "type": "fact",
            "content": "Original",
        },
    )
    assert created.status_code == 201
    memory_id = UUID(created.json()["id"])
    path = f"/v1/memories/{memory_id}"
    payload = {"expected_version": 1, "content": "Private changed content"}
    headers = {"Idempotency-Key": "patch-replay"}
    first = owner_client.patch(path, json=payload, headers=headers)
    replay = owner_client.patch(path, json=payload, headers=headers)
    mismatch = owner_client.patch(
        path, json=payload | {"content": "Other content"}, headers=headers
    )
    assert first.status_code == replay.status_code == 200
    assert first.json()["replayed"] is False and replay.json()["replayed"] is True
    assert first.json() | {"replayed": True} == replay.json()
    assert (
        mismatch.status_code == 409 and mismatch.json()["error"]["code"] == "idempotency_mismatch"
    )
    with engine.connect() as conn:
        assert conn.scalar(select(memories.c.version).where(memories.c.id == memory_id)) == 2
        assert (
            conn.scalar(
                select(func.count())
                .select_from(memory_versions)
                .where(memory_versions.c.memory_id == memory_id)
            )
            == 2
        )
        assert (
            conn.scalar(select(scopes.c.revision).where(scopes.c.id == workspace.personal_scope_id))
            == 2
        )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(activity)
                .where(
                    activity.c.workspace_id == workspace.id,
                    activity.c.action == "memory.update",
                    activity.c.result == "ok",
                )
            )
            == 1
        )
        operation = (
            conn.execute(
                select(operations).where(
                    operations.c.workspace_id == workspace.id,
                    operations.c.operation == "memory.update",
                )
            )
            .mappings()
            .one()
        )
        assert "Private changed content" not in str(dict(operation))


def test_delete_replay_after_tombstone_does_not_repeat_forget(
    engine: Engine,
    workspace: WorkspaceInfo,
    owner_client: TestClient,
) -> None:
    created = owner_client.post(
        "/v1/memories",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "type": "fact",
            "content": "Forget this text",
        },
    )
    assert created.status_code == 201
    memory_id = UUID(created.json()["id"])
    path = f"/v1/memories/{memory_id}"
    headers = {"Idempotency-Key": "delete-replay"}
    first = owner_client.delete(path, headers=headers)
    assert first.status_code == 200
    assert owner_client.get(path).status_code == 404
    replay = owner_client.delete(path, headers=headers)
    assert replay.status_code == 200
    assert first.json()["replayed"] is False
    assert replay.json() == first.json() | {"replayed": True}
    with engine.connect() as conn:
        row = conn.execute(select(memories).where(memories.c.id == memory_id)).mappings().one()
        assert row["state"] == "deleted" and row["content"] is None and row["version"] == 2
        assert (
            conn.scalar(select(scopes.c.revision).where(scopes.c.id == workspace.personal_scope_id))
            == 2
        )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(activity)
                .where(
                    activity.c.workspace_id == workspace.id,
                    activity.c.action == "memory.forget",
                    activity.c.result == "ok",
                )
            )
            == 1
        )
        operation = (
            conn.execute(
                select(operations).where(
                    operations.c.workspace_id == workspace.id,
                    operations.c.operation == "memory.forget",
                )
            )
            .mappings()
            .one()
        )
        assert "Forget this text" not in str(dict(operation))
