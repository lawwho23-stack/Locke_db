"""Owner correction, conflict drafts and promotion preserve a single current fact."""

from collections.abc import Callable, Sequence
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import Connection, select

from memory_platform.tables import memory_relations
from tests.conftest import WorkspaceInfo


def test_correction_then_forget_does_not_reactivate_predecessor(
    owner_client: TestClient,
    workspace: WorkspaceInfo,
    db_conn: Connection,
) -> None:
    req = {
        "scope_id": str(workspace.personal_scope_id),
        "type": "preference",
        "content": "old",
        "fact_key": "preference",
    }
    first = owner_client.post("/v1/memories", json=req).json()
    second = owner_client.post("/v1/memories", json={**req, "content": "new"}).json()
    assert second["superseded_id"] == first["id"]
    old = owner_client.get(f"/v1/memories/{first['id']}").json()
    assert old["state"] == "superseded" and old["version"] == 2
    assert [v["reason"] for v in old["versions"]] == ["created", "superseded"]
    assert [m["id"] for m in owner_client.get("/v1/memories").json()["items"]] == [second["id"]]
    relation = (
        db_conn.execute(
            select(memory_relations).where(memory_relations.c.from_id == UUID(second["id"]))
        )
        .mappings()
        .one()
    )
    assert relation["to_id"] == UUID(first["id"]) and relation["type"] == "supersedes"
    assert (
        owner_client.patch(
            f"/v1/memories/{first['id']}", json={"expected_version": 2, "content": "invalid"}
        ).status_code
        == 409
    )
    assert owner_client.delete(f"/v1/memories/{second['id']}").status_code == 200
    assert owner_client.get("/v1/memories").json()["items"] == []
    assert owner_client.get(f"/v1/memories/{first['id']}").json()["state"] == "superseded"


def test_agent_draft_promotion_and_suppression_on_update(
    owner_client: TestClient,
    workspace: WorkspaceInfo,
    make_agent_client: Callable[[Sequence[UUID], Sequence[str]], TestClient],
) -> None:
    agent = make_agent_client([workspace.personal_scope_id], ["memory:read", "memory:write"])
    req = {
        "scope_id": str(workspace.personal_scope_id),
        "type": "procedure",
        "content": "owner procedure",
        "fact_key": "procedure",
    }
    active = owner_client.post("/v1/memories", json=req).json()
    draft = agent.post("/v1/memories", json={**req, "content": "agent proposed procedure"}).json()
    assert draft["state"] == "draft" and draft["conflict_with_id"] == active["id"]
    path = f"/v1/memories/{draft['id']}"
    assert agent.patch(path, json={"expected_version": 1, "state": "active"}).status_code == 403
    assert agent.patch(path, json={"expected_version": 1, "fact_key": None}).status_code == 403
    promoted = owner_client.patch(path, json={"expected_version": 1, "state": "active"}).json()
    assert promoted["state"] == "active" and promoted["trust"] == "owner_asserted"
    assert promoted["superseded_id"] == active["id"]
    detail = owner_client.get(path).json()
    assert detail["versions"][-1]["reason"] == "promoted" and len(detail["evidence"]) == 2
    forgotten = owner_client.post(
        "/v1/memories",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "type": "fact",
            "content": "forgotten target",
        },
    ).json()
    assert owner_client.delete(f"/v1/memories/{forgotten['id']}").status_code == 200
    denied = agent.patch(path, json={"expected_version": 2, "content": "FORGOTTEN TARGET"})
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "memory_suppressed"
    assert owner_client.get(path).json()["version"] == 2


def test_expired_filter_and_pagination_over_http(
    owner_client: TestClient, workspace: WorkspaceInfo
) -> None:
    req = {"scope_id": str(workspace.personal_scope_id), "type": "fact", "labels": ["example"]}
    expected = set()
    for i in range(3):
        expected.add(
            owner_client.post("/v1/memories", json={**req, "content": f"entry {i}"}).json()["id"]
        )
    expired = owner_client.post(
        "/v1/memories", json={**req, "content": "expired", "valid_until": "2000-01-01T00:00:00Z"}
    ).json()["id"]
    params = {"limit": 2, "label": "example", "type": "fact"}
    first = owner_client.get("/v1/memories", params=params).json()
    second = owner_client.get(
        "/v1/memories", params={**params, "cursor": first["next_cursor"]}
    ).json()
    assert {m["id"] for m in first["items"] + second["items"]} == expected
    assert len(first["items"]) == 2 and len(second["items"]) == 1 and second["next_cursor"] is None
    rows = owner_client.get("/v1/memories", params={"state": "expired"}).json()["items"]
    assert len(rows) == 1 and rows[0]["id"] == expired and rows[0]["state"] == "expired"
