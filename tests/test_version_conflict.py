"""Concurrent HTTP updates reject a stale version without partial writes."""

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


def test_concurrent_patch_expected_version_changes_once(
    engine: Engine,
    workspace: WorkspaceInfo,
    owner_client: TestClient,
    client_for_token: Callable[[str], TestClient],
) -> None:
    created = owner_client.post(
        "/v1/memories",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "type": "fact",
            "content": "Original fact",
        },
    )
    assert created.status_code == 201
    memory_id = UUID(created.json()["id"])
    barrier = Barrier(2)

    def patch(index: int) -> Response:
        with client_for_token(workspace.owner_token) as client:
            barrier.wait(timeout=10)
            return client.patch(
                f"/v1/memories/{memory_id}",
                json={
                    "expected_version": 1,
                    "content": f"Concurrent change {index}",
                },
                headers={"Idempotency-Key": f"version-race-{index}"},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(patch, range(2)))
    assert sorted(result.status_code for result in results) == [200, 409]
    success = next(result for result in results if result.status_code == 200)
    conflict = next(result for result in results if result.status_code == 409)
    assert success.json()["version"] == 2
    assert conflict.json()["error"]["code"] == "version_conflict"
    assert conflict.json()["error"]["details"] == {"current_version": 2}
    with engine.connect() as conn:
        stored = conn.execute(select(memories).where(memories.c.id == memory_id)).mappings().one()
        assert stored["version"] == 2
        assert stored["content"] in {"Concurrent change 0", "Concurrent change 1"}
        assert (
            conn.scalar(
                select(func.count())
                .select_from(memory_versions)
                .where(memory_versions.c.memory_id == memory_id)
            )
            == 2
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
        assert (
            conn.scalar(
                select(func.count())
                .select_from(operations)
                .where(
                    operations.c.workspace_id == workspace.id,
                    operations.c.operation == "memory.update",
                )
            )
            == 1
        )
