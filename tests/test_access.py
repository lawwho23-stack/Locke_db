"""Scope/workspace concealment and capability enforcement at the shared access path."""

from collections.abc import Callable, Sequence
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, insert

from memory_platform.auth.principal import Principal, resolve_principal
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.access import fetch_memory_checked, readable_scope_ids
from memory_platform.tables import credential_grants, memories
from tests.conftest import WorkspaceInfo, insert_scope, seed_workspace


def principal_with(
    workspace: WorkspaceInfo, grants: dict[UUID, frozenset[Capability]]
) -> Principal:
    return Principal(
        actor_id=workspace.owner_actor_id,
        actor_kind=ActorKind.owner,
        workspace_id=workspace.id,
        credential_id=workspace.owner_credential_id,
        is_admin=True,
        grants=grants,
    )


def memory_row(engine: Engine, workspace: WorkspaceInfo, *, state: str = "active") -> UUID:
    memory_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(memories).values(
                id=memory_id,
                workspace_id=workspace.id,
                scope_id=workspace.personal_scope_id,
                type="fact",
                content="Visible only in its granted scope",
                content_hash="test-hash",
                trust="owner_asserted",
                state=state,
                created_by=workspace.owner_actor_id,
            )
        )
    return memory_id


def test_readable_scopes_enforce_visibility_and_capability() -> None:
    readable, writable, hidden = uuid4(), uuid4(), uuid4()
    principal = Principal(
        uuid4(),
        ActorKind.agent,
        uuid4(),
        uuid4(),
        False,
        {
            readable: frozenset({Capability.memory_read}),
            writable: frozenset({Capability.memory_write}),
        },
    )
    assert readable_scope_ids(principal, None) == [readable]
    assert readable_scope_ids(principal, []) == []
    assert readable_scope_ids(principal, [readable]) == [readable]
    for scope, code in [(hidden, ErrorCode.not_found), (writable, ErrorCode.forbidden)]:
        with pytest.raises(AppError) as exc:
            readable_scope_ids(principal, [scope])
        assert exc.value.code == code


@pytest.mark.parametrize("for_update", [False, True])
def test_fetch_granted_memory(engine: Engine, workspace: WorkspaceInfo, for_update: bool) -> None:
    memory_id = memory_row(engine, workspace)
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
        row = fetch_memory_checked(
            conn, principal, memory_id, Capability.memory_read, for_update=for_update
        )
        assert row["id"] == memory_id
        assert row["workspace_id"] == workspace.id


@pytest.mark.parametrize("condition", ["missing", "deleted", "other-workspace", "hidden-scope"])
def test_fetch_conceals_missing_deleted_and_ungranted(
    engine: Engine, workspace: WorkspaceInfo, condition: str
) -> None:
    if condition == "missing":
        memory_id = uuid4()
    elif condition == "other-workspace":
        memory_id = memory_row(engine, seed_workspace(engine, "other"))
    else:
        memory_id = memory_row(
            engine, workspace, state="deleted" if condition == "deleted" else "active"
        )
    grants = (
        {} if condition == "hidden-scope" else {workspace.personal_scope_id: frozenset(Capability)}
    )
    principal = principal_with(workspace, grants)
    with engine.begin() as conn, pytest.raises(AppError) as exc:
        fetch_memory_checked(conn, principal, memory_id, Capability.memory_read)
    assert exc.value.code == ErrorCode.not_found
    assert exc.value.details == {}


@pytest.mark.parametrize("cap", [Capability.memory_write, Capability.memory_delete])
def test_fetch_visible_scope_missing_capability_is_forbidden(
    engine: Engine, workspace: WorkspaceInfo, cap: Capability
) -> None:
    memory_id = memory_row(engine, workspace)
    principal = principal_with(
        workspace, {workspace.personal_scope_id: frozenset({Capability.memory_read})}
    )
    with engine.begin() as conn, pytest.raises(AppError) as exc:
        fetch_memory_checked(conn, principal, memory_id, cap)
    assert exc.value.code == ErrorCode.forbidden


def test_owner_admin_does_not_bypass_scope_grants(engine: Engine, workspace: WorkspaceInfo) -> None:
    with engine.begin() as conn:
        hidden_scope = insert_scope(conn, workspace.id, "project", "ungranted")
        principal = resolve_principal(conn, workspace.owner_token)
        with pytest.raises(AppError) as exc:
            principal.require(hidden_scope, Capability.memory_write)
        assert exc.value.code == ErrorCode.not_found


def test_http_project_credential_cannot_access_personal_scope(
    engine: Engine,
    workspace: WorkspaceInfo,
    make_scope: Callable[[str, str], UUID],
    make_agent_client: Callable[[Sequence[UUID], Sequence[str]], TestClient],
) -> None:
    personal_memory = memory_row(engine, workspace)
    project = make_scope("project", "agent-only-project")
    agent = make_agent_client([project], [cap.value for cap in Capability])
    path = f"/v1/memories/{personal_memory}"
    responses = [
        agent.get(path),
        agent.patch(path, json={"expected_version": 1, "content": "Cannot change personal"}),
        agent.delete(path),
        agent.post(
            "/v1/memories",
            json={
                "scope_id": str(workspace.personal_scope_id),
                "type": "fact",
                "content": "Denied",
            },
        ),
        agent.get("/v1/memories", params={"scope_id": str(workspace.personal_scope_id)}),
    ]
    for response in responses:
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert "Visible only in its granted scope" not in response.text
    assert agent.get("/v1/memories").json()["items"] == []
    assert {item["id"] for item in agent.get("/v1/scopes").json()["items"]} == {str(project)}
    allowed = agent.post(
        "/v1/memories",
        json={"scope_id": str(project), "type": "fact", "content": "Project access works"},
    )
    assert allowed.status_code == 201


def test_http_cross_workspace_ids_are_concealed(engine: Engine, owner_client: TestClient) -> None:
    other = seed_workspace(engine, "other-http-workspace")
    other_memory = memory_row(engine, other)
    path = f"/v1/memories/{other_memory}"
    responses = [
        owner_client.get(path),
        owner_client.patch(path, json={"expected_version": 1, "content": "Denied update"}),
        owner_client.delete(path),
        owner_client.post(
            "/v1/memories",
            json={"scope_id": str(other.personal_scope_id), "type": "fact", "content": "Denied"},
        ),
        owner_client.get("/v1/memories", params={"scope_id": str(other.personal_scope_id)}),
    ]
    for response in responses:
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert "Visible only in its granted scope" not in response.text


def test_http_write_only_scope_is_visible_but_not_readable(
    workspace: WorkspaceInfo,
    make_agent_client: Callable[[Sequence[UUID], Sequence[str]], TestClient],
) -> None:
    agent = make_agent_client([workspace.personal_scope_id], [Capability.memory_write.value])
    written = agent.post(
        "/v1/memories",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "type": "fact",
            "content": "Write-only",
        },
    )
    assert written.status_code == 201
    for response in [
        agent.get(f"/v1/memories/{written.json()['id']}"),
        agent.get("/v1/memories", params={"scope_id": str(workspace.personal_scope_id)}),
    ]:
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
    default_list = agent.get("/v1/memories")
    assert default_list.status_code == 200
    assert default_list.json() == {"items": [], "next_cursor": None}
    visible_scopes = agent.get("/v1/scopes")
    assert visible_scopes.status_code == 200
    assert visible_scopes.json()["items"][0]["capabilities"] == ["memory:write"]


def test_http_owner_admin_cannot_bypass_missing_grant(
    engine: Engine, workspace: WorkspaceInfo, owner_client: TestClient
) -> None:
    memory_id = memory_row(engine, workspace)
    with engine.begin() as conn:
        conn.execute(
            delete(credential_grants).where(
                credential_grants.c.credential_id == workspace.owner_credential_id,
                credential_grants.c.scope_id == workspace.personal_scope_id,
            )
        )
    path = f"/v1/memories/{memory_id}"
    for response in [
        owner_client.get(path),
        owner_client.patch(path, json={"expected_version": 1, "content": "Denied"}),
        owner_client.delete(path),
        owner_client.post(
            "/v1/memories",
            json={
                "scope_id": str(workspace.personal_scope_id),
                "type": "fact",
                "content": "Denied",
            },
        ),
        owner_client.get("/v1/memories", params={"scope_id": str(workspace.personal_scope_id)}),
    ]:
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    assert owner_client.get("/v1/memories").json()["items"] == []


def test_http_revoked_credential_is_rejected_on_later_requests(
    workspace: WorkspaceInfo,
    owner_client: TestClient,
    client_for_token: Callable[[str], TestClient],
) -> None:
    issued = owner_client.post(
        "/v1/credentials",
        json={
            "display_name": "Revocation test agent",
            "grants": [
                {"scope_id": str(workspace.personal_scope_id), "capabilities": ["memory:read"]}
            ],
        },
    )
    assert issued.status_code == 201
    agent = client_for_token(issued.json()["token"])
    assert agent.get("/v1/scopes").status_code == 200
    assert owner_client.delete(f"/v1/credentials/{issued.json()['id']}").status_code == 200
    for path in ["/v1/scopes", "/v1/memories"]:
        response = agent.get(path)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "authorization", ["Bearer wrong_prefix", "Bearer mem_", "Basic mem_token", "Bearer mem_a b", ""]
)
def test_http_invalid_authorization_is_401(app: FastAPI, authorization: str) -> None:
    response = TestClient(app).get("/v1/scopes", headers={"Authorization": authorization})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_http_nonadmin_cannot_administer_scopes_or_credentials(
    workspace: WorkspaceInfo,
    make_agent_client: Callable[[Sequence[UUID], Sequence[str]], TestClient],
) -> None:
    agent = make_agent_client([workspace.personal_scope_id], [cap.value for cap in Capability])
    responses = [
        agent.post("/v1/scopes", json={"kind": "project", "name": "cannot-create"}),
        agent.post(
            "/v1/credentials",
            json={
                "display_name": "Cannot issue",
                "grants": [
                    {"scope_id": str(workspace.personal_scope_id), "capabilities": ["memory:read"]}
                ],
            },
        ),
        agent.delete(f"/v1/credentials/{uuid4()}"),
    ]
    for response in responses:
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
