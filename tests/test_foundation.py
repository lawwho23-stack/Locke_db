"""Tests for the foundation code: tokens, errors, principal rules, API schemas, settings.

None of these need the database.
"""

import uuid
from collections.abc import Callable, Sequence
from typing import Annotated, Any
from uuid import UUID

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import Connection, select

from memory_platform.api.schemas import (
    CredentialCreateRequest,
    GrantIn,
    MemoryWriteResponse,
    RememberRequest,
    ScopeCreateRequest,
    UpdateRequest,
)
from memory_platform.auth.principal import Principal
from memory_platform.auth.tokens import (
    TOKEN_PREFIX,
    display_prefix,
    generate_token,
    hash_token,
)
from memory_platform.config import Settings
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import STATUS_BY_CODE, AppError, ErrorCode
from memory_platform.tables import actors, credential_grants, credentials
from tests.conftest import WorkspaceInfo

SCOPE = uuid.uuid4()


def test_token_format_and_hash() -> None:
    token = generate_token()
    assert token.startswith(TOKEN_PREFIX)
    assert len(token) > 40
    assert generate_token() != token
    assert len(hash_token(token)) == 64
    assert hash_token(token) == hash_token(token)
    assert hash_token(token) != hash_token(token + "x")
    assert display_prefix(token) == token[:12]


def test_every_error_code_has_a_status() -> None:
    assert set(STATUS_BY_CODE) == set(ErrorCode)
    error = AppError(ErrorCode.version_conflict, "stale", details={"current_version": 3})
    assert error.http_status == 409
    assert error.details == {"current_version": 3}
    assert AppError(ErrorCode.not_found, "x").details == {}


def _principal(caps: set[Capability]) -> Principal:
    return Principal(
        actor_id=uuid.uuid4(),
        actor_kind=ActorKind.agent,
        workspace_id=uuid.uuid4(),
        credential_id=uuid.uuid4(),
        is_admin=False,
        grants={SCOPE: frozenset(caps)},
    )


def test_principal_require_hides_scopes_without_any_grant() -> None:
    principal = _principal({Capability.memory_read})
    principal.require(SCOPE, Capability.memory_read)  # allowed: no error

    with pytest.raises(AppError) as forbidden:
        principal.require(SCOPE, Capability.memory_write)
    assert forbidden.value.code is ErrorCode.forbidden

    with pytest.raises(AppError) as hidden:
        principal.require(uuid.uuid4(), Capability.memory_read)
    assert hidden.value.code is ErrorCode.not_found


def test_principal_scope_helpers() -> None:
    principal = _principal({Capability.memory_read, Capability.memory_write})
    assert principal.scopes_with(Capability.memory_read) == {SCOPE}
    assert principal.scopes_with(Capability.memory_delete) == frozenset()
    assert principal.has_grant(SCOPE)
    assert not principal.has_grant(uuid.uuid4())


def _remember(**overrides: Any) -> RememberRequest:
    return RememberRequest(**{"scope_id": SCOPE, "type": "fact", "content": "x", **overrides})


def test_remember_request_cleans_input() -> None:
    req = _remember(content="  hello  ", labels=[" a ", "b", "a"])
    assert req.content == "hello"
    assert req.labels == ["a", "b"]
    assert req.importance == 0.5


@pytest.mark.parametrize(
    "bad",
    [
        {"content": "   "},
        {"content": "x" * 8001},
        {"trust": "owner_asserted"},  # clients cannot set trust
        {"state": "active"},  # nor state, nor owner_id / actor_id
        {"actor_id": str(uuid.uuid4())},
        {"fact_key": "Not Valid"},
        {"importance": 1.1},
        {"labels": [str(i) for i in range(21)]},
        {"valid_from": "2026-02-01T00:00:00Z", "valid_until": "2026-01-01T00:00:00Z"},
    ],
)
def test_remember_request_rejects_bad_input(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _remember(**bad)


def test_update_request_needs_a_change_and_tracks_sent_fields() -> None:
    with pytest.raises(ValidationError):
        UpdateRequest(expected_version=1)
    with pytest.raises(ValidationError):
        UpdateRequest(expected_version=1, content=None)
    with pytest.raises(ValidationError):
        UpdateRequest(expected_version=1, state="deleted")  # type: ignore[arg-type]
    # An explicit null on these two means "clear the field", so it counts as sent.
    req = UpdateRequest(expected_version=1, valid_until=None, fact_key=None)
    assert req.provided_fields() == {"valid_until", "fact_key"}


def test_other_request_models() -> None:
    with pytest.raises(ValidationError):
        ScopeCreateRequest(kind="project", name="Bad Name")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        GrantIn(scope_id=SCOPE, capabilities=[])
    with pytest.raises(ValidationError):
        GrantIn(scope_id=SCOPE, capabilities=["memory:read", "memory:read"])  # type: ignore[list-item]
    grant = {"scope_id": SCOPE, "capabilities": ["memory:read"]}
    with pytest.raises(ValidationError):
        CredentialCreateRequest(display_name="bot", grants=[grant, grant])  # type: ignore[list-item]


def test_write_response_has_no_content_field() -> None:
    body = MemoryWriteResponse(
        id=SCOPE,
        scope_id=SCOPE,
        state="active",
        version=1,
        trust="owner_asserted",  # type: ignore[arg-type]
    ).model_dump(mode="json")
    assert "content" not in body
    assert body["indexing_status"] == "not_applicable"
    assert body["replayed"] is False


def test_settings_validates_hmac_key() -> None:
    def build(key: str) -> Settings:
        return Settings(
            _env_file=None, database_url="postgresql://x/y", memory_hmac_key=SecretStr(key)
        )  # type: ignore[call-arg]

    assert build("ab" * 32).hmac_key_bytes == bytes.fromhex("ab" * 32)
    for bad in ("ab" * 31, "zz" * 32, ""):
        with pytest.raises(ValidationError) as exc_info:
            build(bad)
        # The error must never echo the key value.
        assert bad not in str(exc_info.value) or bad == ""


@pytest.fixture
def app() -> FastAPI:
    """Replaces the real `app` fixture in THIS file only: `create_app` is not built yet.

    A tiny app that echoes the Authorization header is enough to test the client fixtures.
    """
    tiny_app = FastAPI()

    @tiny_app.get("/echo")
    def echo(authorization: Annotated[str | None, Header()] = None) -> dict[str, str | None]:
        return {"authorization": authorization}

    return tiny_app


def test_client_fixtures_send_the_bearer_token(
    workspace: WorkspaceInfo,
    owner_client: TestClient,
    client_for_token: Callable[[str], TestClient],
) -> None:
    response = owner_client.get("/echo")
    assert response.status_code == 200
    assert response.json() == {"authorization": f"Bearer {workspace.owner_token}"}
    assert owner_client.token == workspace.owner_token  # type: ignore[attr-defined]
    assert owner_client.actor_id == workspace.owner_actor_id  # type: ignore[attr-defined]
    assert owner_client.credential_id == workspace.owner_credential_id  # type: ignore[attr-defined]

    other = client_for_token("mem_example")
    assert other.get("/echo").json() == {"authorization": "Bearer mem_example"}


def test_make_scope_and_make_agent_client_seed_the_database(
    workspace: WorkspaceInfo,
    make_scope: Callable[[str, str], UUID],
    make_agent_client: Callable[[Sequence[UUID], Sequence[str]], TestClient],
    db_conn: Connection,
) -> None:
    project_scope = make_scope("project", "alpha")
    assert project_scope != workspace.personal_scope_id

    # make_scope grants all 4 capabilities to the owner credential, like the real service.
    owner_grant = db_conn.execute(
        select(credential_grants.c.capabilities).where(
            credential_grants.c.credential_id == workspace.owner_credential_id,
            credential_grants.c.scope_id == project_scope,
        )
    ).scalar_one()
    assert sorted(owner_grant) == sorted(cap.value for cap in Capability)

    agent = make_agent_client([project_scope], ["memory:read", "memory:write"])
    assert agent.token.startswith("mem_")  # type: ignore[attr-defined]
    credential = db_conn.execute(
        select(credentials).where(credentials.c.id == agent.credential_id)  # type: ignore[attr-defined]
    ).one()
    assert credential.is_admin is False
    assert credential.token_hash == hash_token(agent.token)  # type: ignore[attr-defined]
    assert credential.actor_id == agent.actor_id  # type: ignore[attr-defined]
    actor_kind = db_conn.execute(
        select(actors.c.kind).where(actors.c.id == agent.actor_id)  # type: ignore[attr-defined]
    ).scalar_one()
    assert actor_kind == "agent"
    agent_grant = db_conn.execute(
        select(credential_grants.c.capabilities).where(
            credential_grants.c.credential_id == agent.credential_id  # type: ignore[attr-defined]
        )
    ).scalar_one()
    assert sorted(agent_grant) == ["memory:read", "memory:write"]
