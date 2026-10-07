"""Shared pytest fixtures (contract section 9).

How isolation works
-------------------
- One throw-away database per pytest run, named `memtest_<8 hex>`, is created on the local
  Docker server (`TEST_DATABASE_URL`, port 5433). Alembic builds the schema in it. At the end
  it is dropped with `WITH (FORCE)`. Parallel pytest runs never share a database.
- Every TEST gets its OWN workspace (no table truncation). Rows from different tests live
  side by side and cannot see each other.
- Seeding uses direct table inserts (plain helper functions below). It never calls a
  service, so these fixtures work even when the services are still stubs.
"""

import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Connection, Engine, create_engine, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from memory_platform.api.app import create_app
from memory_platform.auth.tokens import display_prefix, generate_token, hash_token
from memory_platform.config import REPO_ROOT, Settings
from memory_platform.db import make_engine, normalize_url
from memory_platform.enums import ActorKind, Capability, ScopeKind
from memory_platform.tables import (
    actors,
    credential_grants,
    credentials,
    scopes,
    workspaces,
)

BACKEND_DIR: Path = REPO_ROOT / "backend"

# A fixed key, so tests can compute the same HMACs the app computes.
TEST_HMAC_KEY_HEX = "0123456789abcdef" * 4  # 64 hex characters

ALL_CAPABILITIES: list[str] = [cap.value for cap in Capability]

# Tests create and drop databases, so they must only ever talk to a LOCAL server.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


# --- Seeding helpers (plain functions; fixtures below call them) ------------------------------


@dataclass(frozen=True)
class WorkspaceInfo:
    id: UUID
    owner_actor_id: UUID
    owner_credential_id: UUID
    owner_token: str
    personal_scope_id: UUID


class MemoryTestClient(TestClient):
    """A `TestClient` that remembers the bearer token it was created with.

    `actor_id` and `credential_id` are filled in for owner and agent clients (handy for
    checking `created_by` or evidence rows). `client_for_token` clients leave them as None.
    """

    token: str = ""
    actor_id: UUID | None = None
    credential_id: UUID | None = None


def insert_workspace(conn: Connection, name: str = "test-workspace") -> UUID:
    workspace_id = uuid.uuid4()
    conn.execute(insert(workspaces).values(id=workspace_id, name=name))
    return workspace_id


def insert_actor(conn: Connection, workspace_id: UUID, kind: ActorKind, display_name: str) -> UUID:
    actor_id = uuid.uuid4()
    conn.execute(
        insert(actors).values(
            id=actor_id, workspace_id=workspace_id, kind=kind.value, display_name=display_name
        )
    )
    return actor_id


def insert_credential(
    conn: Connection, workspace_id: UUID, actor_id: UUID, *, is_admin: bool
) -> tuple[UUID, str]:
    """Insert a credential. Returns `(credential_id, plain_token)`; only the hash is stored."""
    token = generate_token()
    credential_id = uuid.uuid4()
    conn.execute(
        insert(credentials).values(
            id=credential_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            token_hash=hash_token(token),
            token_prefix=display_prefix(token),
            is_admin=is_admin,
        )
    )
    return credential_id, token


def insert_scope(conn: Connection, workspace_id: UUID, kind: str, name: str) -> UUID:
    scope_id = uuid.uuid4()
    conn.execute(
        insert(scopes).values(
            id=scope_id, workspace_id=workspace_id, kind=ScopeKind(kind).value, name=name
        )
    )
    return scope_id


def insert_grant(
    conn: Connection,
    workspace_id: UUID,
    credential_id: UUID,
    scope_id: UUID,
    capabilities: Sequence[str],
) -> None:
    conn.execute(
        insert(credential_grants).values(
            credential_id=credential_id,
            scope_id=scope_id,
            workspace_id=workspace_id,
            # Capability(...) rejects typos here, with a clear error, before the database does.
            capabilities=[Capability(cap).value for cap in capabilities],
        )
    )


def seed_workspace(engine: Engine, name: str = "test-workspace") -> WorkspaceInfo:
    """Create a workspace with an owner, an admin credential and a `personal` scope.

    The owner gets explicit full grants on the scope, just like in production: there is no
    "owner skips the filter" rule.
    """
    with engine.begin() as conn:
        workspace_id = insert_workspace(conn, name)
        owner_actor_id = insert_actor(conn, workspace_id, ActorKind.owner, "Owner")
        credential_id, token = insert_credential(conn, workspace_id, owner_actor_id, is_admin=True)
        scope_id = insert_scope(conn, workspace_id, "personal", "personal")
        insert_grant(conn, workspace_id, credential_id, scope_id, ALL_CAPABILITIES)
    return WorkspaceInfo(
        id=workspace_id,
        owner_actor_id=owner_actor_id,
        owner_credential_id=credential_id,
        owner_token=token,
        personal_scope_id=scope_id,
    )


# --- Database and settings (one per pytest run) -----------------------------------------------


class _TestEnv(BaseSettings):
    """Reads only TEST_DATABASE_URL (from the environment or the repo-root .env)."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", extra="ignore", hide_input_in_errors=True
    )
    test_database_url: str


def make_alembic_config(url: str) -> Config:
    """Alembic config that targets `url`. `env.py` reads `config.attributes["url"]` first."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["url"] = url
    return config


@pytest.fixture(scope="session")
def test_db_url() -> Iterator[str]:
    """Create `memtest_<8hex>` on the local test server, migrate it, drop it at the end."""
    admin_url = normalize_url(_TestEnv().test_database_url)  # type: ignore[call-arg]
    host = make_url(admin_url).host
    if host not in _LOCAL_HOSTS:
        pytest.exit(f"TEST_DATABASE_URL must point to a local server, got host {host!r}")

    db_name = f"memtest_{uuid.uuid4().hex[:8]}"
    # CREATE/DROP DATABASE cannot run inside a transaction, hence AUTOCOMMIT.
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    url = make_url(admin_url).set(database=db_name).render_as_string(hide_password=False)
    try:
        command.upgrade(make_alembic_config(url), "head")
        yield url
    finally:
        # FORCE disconnects anyone still connected, so the drop cannot hang.
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture(scope="session")
def alembic_config(test_db_url: str) -> Config:
    return make_alembic_config(test_db_url)


@pytest.fixture(scope="session")
def settings(test_db_url: str) -> Settings:
    # `_env_file=None`: ignore .env completely, so tests can never pick up the Neon URLs.
    # `migration_database_url=None` makes that explicit.
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=test_db_url,
        migration_database_url=None,
        test_database_url=None,
        memory_hmac_key=SecretStr(TEST_HMAC_KEY_HEX),
    )


@pytest.fixture(scope="session")
def engine(settings: Settings) -> Iterator[Engine]:
    # Small pool on purpose: many agents may run pytest at once on one server.
    eng = make_engine(
        settings.database_url,
        pool_size=2,
        max_overflow=2,
        prepare_threshold=settings.db_prepare_threshold,
    )
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def _raw_engine(settings: Settings) -> Iterator[Engine]:
    """Separate engine for `db_conn`, so raw checks never use one of the app engine's slots."""
    eng = create_engine(
        normalize_url(settings.database_url), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    yield eng
    eng.dispose()


@pytest.fixture
def db_conn(_raw_engine: Engine) -> Iterator[Connection]:
    """A connection in autocommit mode, for raw SQL checks (for example a forget sweep)."""
    with _raw_engine.connect() as conn:
        yield conn


# --- App and per-test data --------------------------------------------------------------------


@pytest.fixture
def app(settings: Settings, engine: Engine) -> FastAPI:
    return create_app(settings=settings, engine=engine)


@pytest.fixture
def workspace(engine: Engine) -> WorkspaceInfo:
    """A fresh workspace for this test: owner, admin credential, `personal` scope, grants."""
    return seed_workspace(engine)


def _client(
    app: FastAPI,
    token: str,
    *,
    actor_id: UUID | None = None,
    credential_id: UUID | None = None,
) -> MemoryTestClient:
    client = MemoryTestClient(app, headers={"Authorization": f"Bearer {token}"})
    client.token = token
    client.actor_id = actor_id
    client.credential_id = credential_id
    return client


@pytest.fixture
def owner_client(app: FastAPI, workspace: WorkspaceInfo) -> TestClient:
    return _client(
        app,
        workspace.owner_token,
        actor_id=workspace.owner_actor_id,
        credential_id=workspace.owner_credential_id,
    )


@pytest.fixture
def client_for_token(app: FastAPI) -> Callable[[str], TestClient]:
    """Build a fresh client for a token (use one client per thread in concurrency tests)."""

    def factory(token: str) -> TestClient:
        return _client(app, token)

    return factory


@pytest.fixture
def make_scope(engine: Engine, workspace: WorkspaceInfo) -> Callable[[str, str], UUID]:
    """`make_scope(kind, name) -> scope_id`: a new scope in the test workspace.

    Like the real service, it grants all 4 capabilities to every non-revoked admin
    credential in the workspace (the owner's).
    """

    def factory(kind: str, name: str) -> UUID:
        with engine.begin() as conn:
            scope_id = insert_scope(conn, workspace.id, kind, name)
            admin_credential_ids = (
                conn.execute(
                    select(credentials.c.id).where(
                        credentials.c.workspace_id == workspace.id,
                        credentials.c.is_admin.is_(True),
                        credentials.c.revoked_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            for credential_id in admin_credential_ids:
                insert_grant(conn, workspace.id, credential_id, scope_id, ALL_CAPABILITIES)
        return scope_id

    return factory


@pytest.fixture
def make_agent_client(
    app: FastAPI, engine: Engine, workspace: WorkspaceInfo
) -> Callable[[Sequence[UUID], Sequence[str]], TestClient]:
    """`make_agent_client(scope_ids, capabilities) -> client`.

    Creates a new AGENT actor with a non-admin credential that holds `capabilities`
    (for example `["memory:read", "memory:write"]`) on every scope in `scope_ids`.
    The token is available as `client.token`.
    """
    counter = 0

    def factory(scope_ids: Sequence[UUID], capabilities: Sequence[str]) -> TestClient:
        nonlocal counter
        counter += 1
        with engine.begin() as conn:
            actor_id = insert_actor(conn, workspace.id, ActorKind.agent, f"agent-{counter}")
            credential_id, token = insert_credential(conn, workspace.id, actor_id, is_admin=False)
            for scope_id in scope_ids:
                insert_grant(conn, workspace.id, credential_id, scope_id, capabilities)
        return _client(app, token, actor_id=actor_id, credential_id=credential_id)

    return factory
