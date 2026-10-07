"""Force an absent-row fact-key race through real HTTP transactions and the unique index."""

import importlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Connection, Engine, func, select

from memory_platform.enums import MemoryType
from memory_platform.services.supersede import ActiveFact
from memory_platform.tables import (
    activity,
    memories,
    memory_evidence,
    memory_relations,
    memory_versions,
    operations,
    scopes,
)
from tests.conftest import WorkspaceInfo


def test_absent_fact_key_collision_rolls_back_and_retry_rereads(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    workspace: WorkspaceInfo,
    client_for_token: Callable[[str], TestClient],
    owner_client: TestClient,
) -> None:
    remember_module = importlib.import_module("memory_platform.services.remember")
    original = remember_module.find_active_fact_for_update
    barrier = Barrier(2)
    payloads = [
        {
            "scope_id": str(workspace.personal_scope_id),
            "type": "fact",
            "content": f"Private competing fact {index}",
            "fact_key": "concurrent.fact",
        }
        for index in range(2)
    ]

    def both_observe_absent(
        conn: Connection,
        *,
        scope_id: UUID,
        mtype: MemoryType,
        fact_key: str,
    ) -> ActiveFact | None:
        active = original(conn, scope_id=scope_id, mtype=mtype, fact_key=fact_key)
        assert active is None
        # Waiting AFTER the read forces both transactions past the missing-row lookup.
        barrier.wait(timeout=10)
        return active

    def post(index: int) -> Response:
        with client_for_token(workspace.owner_token) as client:
            return client.post(
                "/v1/memories",
                json=payloads[index],
                headers={"Idempotency-Key": f"fact-race-{index}"},
            )

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(remember_module, "find_active_fact_for_update", both_observe_absent)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(post, range(2)))
    assert sorted(result.status_code for result in results) == [201, 409]
    winner_index = next(index for index, result in enumerate(results) if result.status_code == 201)
    loser_index = 1 - winner_index
    winner_id = UUID(results[winner_index].json()["id"])
    error = results[loser_index]
    assert error.json()["error"]["code"] == "fact_key_race"
    assert error.headers["Retry-After"] == "1"
    assert error.json()["error"]["retry_after"] == 1
    assert all(payload["content"] not in str(error.json()) for payload in payloads)
    scope_memory_ids = select(memories.c.id).where(
        memories.c.scope_id == workspace.personal_scope_id
    )
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
                .where(memory_versions.c.memory_id.in_(scope_memory_ids))
            )
            == 1
        )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(memory_evidence)
                .where(memory_evidence.c.memory_id.in_(scope_memory_ids))
            )
            == 1
        )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(memory_relations)
                .where(memory_relations.c.workspace_id == workspace.id)
            )
            == 0
        )
        assert (
            conn.scalar(select(scopes.c.revision).where(scopes.c.id == workspace.personal_scope_id))
            == 1
        )
        operation_rows = (
            conn.execute(select(operations).where(operations.c.workspace_id == workspace.id))
            .mappings()
            .all()
        )
        assert len(operation_rows) == 1
        assert operation_rows[0]["idempotency_key"] == f"fact-race-{winner_index}"
        assert (
            conn.scalar(
                select(func.count())
                .select_from(activity)
                .where(
                    activity.c.workspace_id == workspace.id,
                    activity.c.result == "ok",
                )
            )
            == 1
        )
    current = owner_client.get(
        "/v1/memories", params={"scope_id": str(workspace.personal_scope_id)}
    )
    assert current.status_code == 200
    assert [item["id"] for item in current.json()["items"]] == [str(winner_id)]
    # The failed operation's key was rolled back, so retry uses today's active fact.
    retry = post(loser_index)
    assert retry.status_code == 201 and retry.json()["superseded_id"] == str(winner_id)
    assert retry.json()["replayed"] is False
    with engine.connect() as conn:
        rows = (
            conn.execute(select(memories).where(memories.c.scope_id == workspace.personal_scope_id))
            .mappings()
            .all()
        )
        assert len(rows) == 2
        assert sorted(row["state"] for row in rows) == ["active", "superseded"]
        assert (
            conn.scalar(
                select(func.count())
                .select_from(memory_versions)
                .where(memory_versions.c.memory_id.in_(scope_memory_ids))
            )
            == 3
        )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(memory_evidence)
                .where(memory_evidence.c.memory_id.in_(scope_memory_ids))
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
                .select_from(operations)
                .where(operations.c.workspace_id == workspace.id)
            )
            == 2
        )
