"""Direct authentication and dependency checks, independent of unfinished routes."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import Engine, delete, update

from memory_platform.api.deps import (
    get_engine_dep,
    get_idempotency_key,
    get_principal,
    get_request_id,
    get_settings_dep,
)
from memory_platform.auth.principal import resolve_principal
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.tables import credential_grants, credentials
from tests.conftest import (
    WorkspaceInfo,
    insert_actor,
    insert_credential,
    insert_grant,
    insert_scope,
)


def request_for(app: FastAPI) -> Request:
    return Request({"type": "http", "app": app, "headers": []})


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic mem_abc", "Bearer", "Bearer mem_", "Bearer token", "Bearer mem_a b"],
)
def test_malformed_auth_rejected_before_database(authorization: str | None) -> None:
    request = request_for(FastAPI())
    with pytest.raises(AppError) as exc:
        get_principal(request, authorization)
    assert exc.value.code == ErrorCode.unauthenticated
    assert not hasattr(request.state, "principal")


@pytest.mark.parametrize("key", [None, "a", "A0._:-", "x" * 200])
def test_idempotency_header_accepts_valid_key(key: str | None) -> None:
    assert get_idempotency_key(key) == key


@pytest.mark.parametrize("key", ["", "x" * 201, "has space", "💡", "a\n", "a/b"])
def test_idempotency_header_rejects_invalid_key_safely(key: str) -> None:
    with pytest.raises(AppError) as exc:
        get_idempotency_key(key)
    assert exc.value.code == ErrorCode.validation_error
    assert exc.value.details == {
        "errors": [
            {
                "loc": ["header", "Idempotency-Key"],
                "msg": "Invalid Idempotency-Key header.",
                "type": "string_pattern_mismatch",
            }
        ]
    }


def test_resolve_owner_and_dependency_state(engine: Engine, workspace: WorkspaceInfo) -> None:
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
    assert principal.actor_id == workspace.owner_actor_id
    assert principal.workspace_id == workspace.id
    assert principal.credential_id == workspace.owner_credential_id
    assert principal.actor_kind == ActorKind.owner
    assert principal.is_admin is True
    assert principal.grants == {workspace.personal_scope_id: frozenset(Capability)}

    app = FastAPI()
    app.state.engine = engine
    app.state.settings = object()
    request = request_for(app)
    request.state.request_id = "test-request"
    assert get_engine_dep(request) is engine
    assert get_settings_dep(request) is app.state.settings
    assert get_request_id(request) == "test-request"
    assert get_principal(request, f"bearer {workspace.owner_token}") == principal
    assert request.state.principal == principal


def test_resolve_agent_grants_and_no_grant_cache(engine: Engine, workspace: WorkspaceInfo) -> None:
    with engine.begin() as conn:
        actor = insert_actor(conn, workspace.id, ActorKind.agent, "Agent")
        credential, token = insert_credential(conn, workspace.id, actor, is_admin=False)
        scope = insert_scope(conn, workspace.id, "project", "agent-project")
        insert_grant(conn, workspace.id, credential, scope, [Capability.memory_read.value])
        principal = resolve_principal(conn, token)
        assert principal.actor_kind == ActorKind.agent
        assert principal.is_admin is False
        assert principal.grants == {scope: frozenset({Capability.memory_read})}
        conn.execute(
            delete(credential_grants).where(credential_grants.c.credential_id == credential)
        )
        assert resolve_principal(conn, token).grants == {}


@pytest.mark.parametrize("invalid_kind", ["unknown", "revoked", "expired"])
def test_unknown_revoked_expired_share_generic_failure(
    engine: Engine, workspace: WorkspaceInfo, invalid_kind: str
) -> None:
    with engine.begin() as conn:
        token = workspace.owner_token
        if invalid_kind == "unknown":
            token = f"mem_{uuid4().hex}"
        else:
            field = "revoked_at" if invalid_kind == "revoked" else "expires_at"
            conn.execute(
                update(credentials)
                .where(credentials.c.id == workspace.owner_credential_id)
                .values(**{field: datetime.now(UTC) - timedelta(seconds=1)})
            )
        with pytest.raises(AppError) as exc:
            resolve_principal(conn, token)
        assert exc.value.code == ErrorCode.unauthenticated
        assert exc.value.message == "Invalid credentials."
        assert exc.value.details == {}


def test_future_expiry_and_immediate_revocation(engine: Engine, workspace: WorkspaceInfo) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(credentials)
            .where(credentials.c.id == workspace.owner_credential_id)
            .values(expires_at=datetime.now(UTC) + timedelta(hours=1))
        )
        assert (
            resolve_principal(conn, workspace.owner_token).credential_id
            == workspace.owner_credential_id
        )
        conn.execute(
            update(credentials)
            .where(credentials.c.id == workspace.owner_credential_id)
            .values(revoked_at=datetime.now(UTC))
        )
        with pytest.raises(AppError) as exc:
            resolve_principal(conn, workspace.owner_token)
        assert exc.value.code == ErrorCode.unauthenticated
