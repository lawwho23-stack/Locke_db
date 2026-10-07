"""Forgetting serializes against writes and scrubs every historical representation."""

import importlib
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, select, text

from memory_platform.tables import memories, memory_versions
from tests.conftest import WorkspaceInfo


def test_forget_waits_for_inflight_remember(
    monkeypatch: pytest.MonkeyPatch,
    owner_client: TestClient,
    workspace: WorkspaceInfo,
    make_agent_client: Callable[[Sequence[UUID], Sequence[str]], TestClient],
    client_for_token: Callable[[str], TestClient],
    db_conn: Connection,
) -> None:
    module = importlib.import_module("memory_platform.services.remember")
    req = {
        "scope_id": str(workspace.personal_scope_id),
        "type": "fact",
        "content": "private race text",
    }
    mid = owner_client.post("/v1/memories", json=req).json()["id"]
    agent = make_agent_client([workspace.personal_scope_id], ["memory:write"])
    checked, release, forget_started = Event(), Event(), Event()
    original = module.check_suppression

    def pause(*args, **kwargs):
        original(*args, **kwargs)
        checked.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(module, "check_suppression", pause)

    def write():
        return client_for_token(agent.token).post("/v1/memories", json=req)

    def forget():
        forget_started.set()
        return client_for_token(workspace.owner_token).delete(f"/v1/memories/{mid}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write)
        assert checked.wait(timeout=5)
        forgetter = pool.submit(forget)
        assert forget_started.wait(timeout=5)
        try:
            deadline = time.monotonic() + 2
            waiting = False
            while time.monotonic() < deadline and not forgetter.done():
                waiting = bool(
                    db_conn.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks "
                            "WHERE locktype='advisory' AND NOT granted)"
                        )
                    )
                )
                if waiting:
                    break
                time.sleep(0.01)
            assert waiting, "Forget must wait for the in-flight suppression check/write"
        finally:
            release.set()
        assert writer.result(timeout=5).status_code == 200
        assert forgetter.result(timeout=5).status_code == 200
    monkeypatch.setattr(module, "check_suppression", original)
    assert agent.post("/v1/memories", json=req).json()["error"]["code"] == "memory_suppressed"
    rows = (
        db_conn.execute(select(memories).where(memories.c.scope_id == workspace.personal_scope_id))
        .mappings()
        .all()
    )
    assert all(row["content"] is None and row["state"] == "deleted" for row in rows)


def test_forget_scrubs_history_and_old_fact_keys(
    owner_client: TestClient,
    workspace: WorkspaceInfo,
    db_conn: Connection,
    make_agent_client: Callable[[Sequence[UUID], Sequence[str]], TestClient],
) -> None:
    req = {
        "scope_id": str(workspace.personal_scope_id),
        "type": "fact",
        "content": "အဟောင်း",
        "fact_key": "old",
        "labels": ["secret-label"],
    }
    mid = owner_client.post("/v1/memories", json=req).json()["id"]
    assert (
        owner_client.patch(
            f"/v1/memories/{mid}",
            json={"expected_version": 1, "content": "new text", "fact_key": "new"},
        ).status_code
        == 200
    )
    assert owner_client.delete(f"/v1/memories/{mid}").status_code == 200
    rows = (
        db_conn.execute(select(memory_versions).where(memory_versions.c.memory_id == UUID(mid)))
        .mappings()
        .all()
    )
    assert len(rows) == 3 and all(
        r["content"] is None and r["fact_key"] is None and r["labels"] == [] for r in rows
    )
    agent = make_agent_client([workspace.personal_scope_id], ["memory:write"])
    for content, key in [
        ("အဟောင်း", None),
        ("NEW TEXT", None),
        ("different", "old"),
        ("different", "new"),
    ]:
        response = agent.post("/v1/memories", json={**req, "content": content, "fact_key": key})
        assert (
            response.status_code == 409 and response.json()["error"]["code"] == "memory_suppressed"
        )


def test_colliding_key_updates_have_no_deadlock(
    owner_client: TestClient,
    workspace: WorkspaceInfo,
    client_for_token: Callable[[str], TestClient],
) -> None:
    from threading import Barrier

    ids = []
    for key in ("a", "b"):
        ids.append(
            owner_client.post(
                "/v1/memories",
                json={
                    "scope_id": str(workspace.personal_scope_id),
                    "type": "fact",
                    "content": key,
                    "fact_key": key,
                },
            ).json()["id"]
        )
    barrier = Barrier(2)

    def change(index):
        barrier.wait(timeout=5)
        return client_for_token(workspace.owner_token).patch(
            f"/v1/memories/{ids[index]}",
            json={"expected_version": 1, "fact_key": ("b", "a")[index]},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(change, [0, 1]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert (
        next(r for r in responses if r.status_code == 409).json()["error"]["code"]
        == "version_conflict"
    )
