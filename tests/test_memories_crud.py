"""HTTP acceptance for the durable core and its safe error envelopes."""

from collections.abc import Callable, Sequence
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update

from memory_platform.enums import Capability
from memory_platform.tables import activity, credential_grants, operations, scopes
from tests.conftest import WorkspaceInfo


def payload(ws: WorkspaceInfo, content: str = "private memory") -> dict[str, str]:
    return {"scope_id": str(ws.personal_scope_id), "type": "fact", "content": content}


def test_http_lifecycle(owner_client: TestClient, workspace: WorkspaceInfo, engine: Engine) -> None:
    assert owner_client.get("/health").json() == {"status": "ok"}
    headers = {"Idempotency-Key": "create-1", "X-Request-ID": "acceptance"}
    created = owner_client.post("/v1/memories", json=payload(workspace), headers=headers)
    assert created.status_code == 201, created.text
    assert created.headers["X-Request-ID"] == "acceptance"
    mid = created.json()["id"]
    assert "content" not in created.json()
    replay = owner_client.post("/v1/memories", json=payload(workspace), headers=headers)
    assert replay.status_code == 201 and replay.json() == {**created.json(), "replayed": True}
    assert (
        owner_client.post(
            "/v1/memories", json=payload(workspace, "different"), headers=headers
        ).json()["error"]["code"]
        == "idempotency_mismatch"
    )
    detail = owner_client.get(f"/v1/memories/{mid}").json()
    assert detail["content"] == "private memory" and len(detail["versions"]) == 1
    assert len(detail["evidence"]) == 1
    duplicate = owner_client.post("/v1/memories", json=payload(workspace))
    assert duplicate.status_code == 200 and duplicate.json()["deduplicated"]
    updated = owner_client.patch(
        f"/v1/memories/{mid}", json={"expected_version": 1, "content": "changed", "fact_key": "key"}
    )
    assert updated.status_code == 200 and updated.json()["version"] == 2
    conflict = owner_client.patch(
        f"/v1/memories/{mid}", json={"expected_version": 1, "content": "stale"}
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["details"] == {
        "current_version": 2
    }
    deletion = owner_client.delete(f"/v1/memories/{mid}", headers={"Idempotency-Key": "delete-1"})
    assert deletion.status_code == 200 and deletion.json()["version"] == 3
    replay = owner_client.delete(f"/v1/memories/{mid}", headers={"Idempotency-Key": "delete-1"})
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert owner_client.get(f"/v1/memories/{mid}").status_code == 404
    assert owner_client.get("/v1/memories").json()["items"] == []
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(scopes.c.revision).where(scopes.c.id == workspace.personal_scope_id)
            ).scalar_one()
            == 3
        )
        stored = (
            conn.execute(
                select(operations.c.response_body).where(operations.c.workspace_id == workspace.id)
            )
            .scalars()
            .all()
        )
        assert all("content" not in body for body in stored)
        assert conn.execute(
            select(activity.c.id).where(activity.c.workspace_id == workspace.id)
        ).all()


def test_grant_loss_prevents_replay(
    owner_client: TestClient,
    workspace: WorkspaceInfo,
    engine: Engine,
    make_agent_client: Callable[[Sequence[UUID], Sequence[str]], TestClient],
) -> None:
    agent = make_agent_client([workspace.personal_scope_id], ["memory:read", "memory:write"])
    created = agent.post(
        "/v1/memories", json=payload(workspace), headers={"Idempotency-Key": "same"}
    )
    assert created.status_code == 201
    with engine.begin() as conn:
        conn.execute(
            update(credential_grants)
            .where(credential_grants.c.credential_id == agent.credential_id)
            .values(capabilities=["memory:read"])
        )
    denied = agent.post(
        "/v1/memories", json=payload(workspace), headers={"Idempotency-Key": "same"}
    )
    assert denied.status_code == 403


@pytest.mark.parametrize(
    "path,status,code",
    [
        ("/missing", 404, "not_found"),
        ("/v1/memories?state=deleted", 422, "validation_error"),
        ("/v1/memories?cursor=invalid", 400, "bad_request"),
        ("/v1/memories?limit=0", 422, "validation_error"),
    ],
)
def test_error_envelope(owner_client: TestClient, path: str, status: int, code: str) -> None:
    response = owner_client.get(path, headers={"X-Request-ID": "errors"})
    assert response.status_code == status
    error = response.json()["error"]
    assert set(error) == {"code", "message", "request_id", "details", "retry_after"}
    assert (
        error["code"] == code
        and error["request_id"] == response.headers["X-Request-ID"] == "errors"
    )


def test_validation_never_echoes_memory(owner_client: TestClient, workspace: WorkspaceInfo) -> None:
    secret = "DO_NOT_ECHO_SECRET"
    for bad in [
        {**payload(workspace), "trust": secret},
        {**payload(workspace), "content": {"secret": secret}},
        {**payload(workspace), "type": secret},
    ]:
        response = owner_client.post("/v1/memories", json=bad)
        assert response.status_code == 422 and secret not in response.text
        for error in response.json()["error"]["details"]["errors"]:
            assert set(error) == {"loc", "msg", "type"}
    response = owner_client.post(
        "/v1/memories", content='{"content":"secret",', headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422 and "secret" not in response.text


def test_body_limit_and_auth(
    app: object, owner_client: TestClient, workspace: WorkspaceInfo
) -> None:
    anon = TestClient(app)
    assert anon.get("/health").status_code == 200
    assert anon.get("/v1/memories").status_code == 401
    oversized = owner_client.post("/v1/memories", content=b"x" * 65537)
    assert oversized.status_code == 413 and oversized.json()["error"]["code"] == "payload_too_large"
    assert oversized.headers["X-Request-ID"]

    def chunks():
        yield b"x" * 40000
        yield b"y" * 30000

    assert owner_client.post("/v1/memories", content=chunks()).status_code == 413
    response = owner_client.get("/health", headers={"X-Request-ID": "invalid id!"})
    assert response.headers["X-Request-ID"] != "invalid id!"


def test_admin_http(
    owner_client: TestClient,
    workspace: WorkspaceInfo,
    client_for_token: Callable[[str], TestClient],
) -> None:
    scope = owner_client.post("/v1/scopes", json={"kind": "project", "name": "demo"})
    assert scope.status_code == 201
    sid = scope.json()["id"]
    assert set(scope.json()["capabilities"]) == {cap.value for cap in Capability}
    assert (
        owner_client.post("/v1/scopes", json={"kind": "project", "name": "demo"}).status_code == 400
    )
    credential = owner_client.post(
        "/v1/credentials",
        json={
            "display_name": "demo-agent",
            "grants": [
                {"scope_id": sid, "capabilities": ["memory:read", "memory:write", "memory:delete"]}
            ],
        },
    )
    assert credential.status_code == 201
    agent = client_for_token(credential.json()["token"])
    assert agent.get("/v1/scopes").json()["items"][0]["id"] == sid
    assert agent.get(f"/v1/memories?scope_id={workspace.personal_scope_id}").status_code == 404
    assert agent.post("/v1/scopes", json={"kind": "project", "name": "denied"}).status_code == 403
    assert (
        owner_client.delete(f"/v1/credentials/{workspace.owner_credential_id}").status_code == 400
    )
    revoke = owner_client.delete(f"/v1/credentials/{credential.json()['id']}")
    assert revoke.status_code == 200
    assert agent.get("/v1/scopes").status_code == 401
    assert owner_client.delete(f"/v1/credentials/{credential.json()['id']}").json() == revoke.json()
