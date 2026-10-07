"""Scope, credential and admin command checks independent of HTTP routes."""

from collections.abc import Iterator
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine, func, select, text, update

from memory_platform import admin
from memory_platform.api.schemas import CredentialCreateRequest, GrantIn, ScopeCreateRequest
from memory_platform.auth.tokens import hash_token
from memory_platform.config import Settings
from memory_platform.enums import ActorKind, Capability, ScopeKind
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.credentials import (
    bootstrap_workspace,
    create_credential,
    revoke_credential,
)
from memory_platform.services.scopes import create_scope, list_scopes
from memory_platform.tables import (
    activity,
    actors,
    credential_grants,
    credentials,
    metadata,
    scopes,
    workspaces,
)
from tests.conftest import WorkspaceInfo, insert_actor, insert_credential, seed_workspace
from tests.test_idempotency_unit import principal_for


@pytest.fixture
def empty_install(engine: Engine) -> Iterator[Connection]:
    # A transaction-local schema gives bootstrap an empty installation without touching others.
    with engine.connect() as conn, conn.begin() as transaction:
        schema = f"bootstrap_{uuid4().hex}"
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        metadata.create_all(conn, checkfirst=False)
        yield conn
        transaction.rollback()


def test_bootstrap_once_empty_install(empty_install: Connection) -> None:
    workspace_id, token = bootstrap_workspace(empty_install, owner_name="Owner")
    assert empty_install.scalar(select(workspaces.c.id)) == workspace_id
    assert empty_install.scalar(select(actors.c.kind)) == ActorKind.owner.value
    assert empty_install.scalar(select(scopes.c.name)) == "personal"
    assert empty_install.scalar(select(scopes.c.grant_revision)) == 1
    row = empty_install.execute(select(credentials)).mappings().one()
    assert row["token_hash"] == hash_token(token)
    assert row["is_admin"] is True and token not in str(dict(row))
    assert set(empty_install.scalar(select(credential_grants.c.capabilities))) == {
        cap.value for cap in Capability
    }
    with pytest.raises(AppError) as exc:
        bootstrap_workspace(empty_install, owner_name="Another")
    assert exc.value.code == ErrorCode.bad_request


def test_scope_grants_owner_admins_only(engine: Engine, workspace: WorkspaceInfo) -> None:
    principal = principal_for(workspace)
    with engine.begin() as conn:
        extra, _ = insert_credential(conn, workspace.id, workspace.owner_actor_id, is_admin=True)
        revoked, _ = insert_credential(conn, workspace.id, workspace.owner_actor_id, is_admin=True)
        conn.execute(
            update(credentials).where(credentials.c.id == revoked).values(revoked_at=func.now())
        )
        agent_id = insert_actor(conn, workspace.id, ActorKind.agent, "Agent")
        agent_admin, _ = insert_credential(conn, workspace.id, agent_id, is_admin=True)
        result = create_scope(
            conn,
            principal,
            ScopeCreateRequest(kind=ScopeKind.project, name="project"),
            request_id="scope",
        )
        grants = (
            conn.execute(
                select(credential_grants.c.credential_id).where(
                    credential_grants.c.scope_id == result.id
                )
            )
            .scalars()
            .all()
        )
        assert set(grants) == {principal.credential_id, extra}
        assert revoked not in grants and agent_admin not in grants
        assert conn.scalar(select(scopes.c.grant_revision).where(scopes.c.id == result.id)) == 1
        assert set(result.capabilities) == set(Capability)
    with pytest.raises(AppError) as exc, engine.begin() as conn:
        create_scope(
            conn,
            principal,
            ScopeCreateRequest(kind=ScopeKind.project, name="project"),
            request_id="duplicate",
        )
    assert exc.value.code == ErrorCode.bad_request
    with engine.begin() as conn:
        visible = list_scopes(
            conn, replace(principal, grants={result.id: frozenset({Capability.memory_read})})
        )
    assert [item.id for item in visible.items] == [result.id]
    assert visible.items[0].capabilities == [Capability.memory_read]


def test_credential_storage_grants_revocation(engine: Engine, workspace: WorkspaceInfo) -> None:
    principal = principal_for(workspace)
    req = CredentialCreateRequest(
        display_name="Assistant",
        grants=[
            GrantIn(scope_id=workspace.personal_scope_id, capabilities=[Capability.memory_read])
        ],
    )
    with engine.begin() as conn:
        created = create_credential(conn, principal, req, request_id="create")
        row = (
            conn.execute(select(credentials).where(credentials.c.id == created.id)).mappings().one()
        )
        assert row["token_hash"] == hash_token(created.token)
        assert created.token not in str(dict(row))
        assert row["is_admin"] is False
        assert conn.scalar(select(actors.c.kind).where(actors.c.id == created.actor_id)) == "agent"
        assert (
            conn.scalar(
                select(scopes.c.grant_revision).where(scopes.c.id == workspace.personal_scope_id)
            )
            == 1
        )
        revoked = revoke_credential(conn, principal, created.id, request_id="revoke")
        repeated = revoke_credential(conn, principal, created.id, request_id="repeat")
        assert repeated.revoked_at == revoked.revoked_at
        assert (
            conn.scalar(
                select(scopes.c.grant_revision).where(scopes.c.id == workspace.personal_scope_id)
            )
            == 2
        )
        assert (
            conn.scalar(
                select(func.count())
                .select_from(activity)
                .where(activity.c.workspace_id == workspace.id)
            )
            == 2
        )
    with pytest.raises(AppError) as exc, engine.begin() as conn:
        revoke_credential(conn, principal, principal.credential_id, request_id="self")
    assert exc.value.code == ErrorCode.bad_request


def test_credentials_workspace_validation_before_writes(
    engine: Engine, workspace: WorkspaceInfo
) -> None:
    other = seed_workspace(engine)
    principal = principal_for(workspace)
    req = CredentialCreateRequest(
        display_name="Hidden",
        grants=[GrantIn(scope_id=other.personal_scope_id, capabilities=[Capability.memory_read])],
    )
    with engine.begin() as conn:
        before = conn.scalar(select(func.count()).select_from(actors))
        with pytest.raises(AppError) as exc:
            create_credential(conn, principal, req, request_id="hidden")
        assert exc.value.code == ErrorCode.not_found
        assert conn.scalar(select(func.count()).select_from(actors)) == before
        with pytest.raises(AppError) as exc:
            revoke_credential(conn, principal, other.owner_credential_id, request_id="hidden")
        assert exc.value.code == ErrorCode.not_found


@pytest.mark.parametrize("service", ["scope", "create", "revoke"])
def test_admin_required(engine: Engine, workspace: WorkspaceInfo, service: str) -> None:
    principal = replace(principal_for(workspace), is_admin=False)
    with pytest.raises(AppError) as exc, engine.begin() as conn:
        if service == "scope":
            create_scope(
                conn,
                principal,
                ScopeCreateRequest(kind=ScopeKind.project, name="blocked"),
                request_id="forbidden",
            )
        elif service == "create":
            create_credential(
                conn,
                principal,
                CredentialCreateRequest(
                    display_name="Assistant",
                    grants=[
                        GrantIn(
                            scope_id=workspace.personal_scope_id,
                            capabilities=[Capability.memory_read],
                        )
                    ],
                ),
                request_id="forbidden",
            )
        else:
            revoke_credential(conn, principal, uuid4(), request_id="forbidden")
    assert exc.value.code == ErrorCode.forbidden


def test_cli_failure_hides_exception_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> None:
        raise RuntimeError("postgresql://private-user:password@private-host/db")

    monkeypatch.setattr(admin, "get_settings", fail)
    assert admin.main(["bootstrap", "--owner-name", "Owner"]) == 1
    captured = capsys.readouterr()
    assert "password" not in captured.err and "private-user" not in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("migration_url", [None, "postgresql://admin@localhost/migration"])
def test_cli_uses_configured_url_and_prints_token_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    empty_install: Connection,
    migration_url: str | None,
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    configured = settings.model_copy(update={"migration_database_url": migration_url})
    urls: list[str] = []
    fake_engine = SimpleNamespace(begin=lambda: nullcontext(empty_install), dispose=lambda: None)

    def engine_factory(url: str, **kwargs: object) -> object:
        urls.append(url)
        return fake_engine

    monkeypatch.setattr(admin, "get_settings", lambda: configured)
    monkeypatch.setattr(admin, "make_engine", engine_factory)
    assert admin.main(["bootstrap", "--owner-name", "Owner"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.count("mem_") == 1 and "store it now" in output.out
    assert urls == [migration_url or configured.database_url]
    assert admin.main(["create-scope", "--kind", "project", "--name", "cli-project"]) == 0
    output = capsys.readouterr()
    assert "cli-project" in output.out and "mem_" not in output.out
    assert (
        empty_install.scalar(select(scopes.c.id).where(scopes.c.name == "cli-project")) is not None
    )
    assert admin.main(["bootstrap", "--owner-name", "Owner"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "already exists" in output.err


def test_bootstrap_concurrent_empty_install(engine: Engine) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    schema = f"bootstrap_concurrent_{uuid4().hex}"
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        metadata.create_all(conn, checkfirst=False)
    barrier = Barrier(2)

    def bootstrap() -> bool:
        barrier.wait(timeout=10)
        try:
            with engine.begin() as conn:
                conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                bootstrap_workspace(conn, owner_name="Owner")
            return True
        except AppError as exc:
            assert exc.code == ErrorCode.bad_request
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(lambda _: bootstrap(), range(2))) == [False, True]
        with engine.begin() as conn:
            conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            assert conn.scalar(select(func.count()).select_from(workspaces)) == 1
            assert conn.scalar(select(func.count()).select_from(actors)) == 1
            assert conn.scalar(select(func.count()).select_from(credentials)) == 1
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
