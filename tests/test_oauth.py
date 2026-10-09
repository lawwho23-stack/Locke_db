"""MCP OAuth bridge: register, owner approve, PKCE exchange, refresh, use, revoke."""

import base64
import hashlib
import secrets

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tests.conftest import WorkspaceInfo

REDIRECT = "http://localhost:4321/callback"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _register(app: FastAPI, workspace: WorkspaceInfo) -> str:
    # Registration is public; any client (even unauthenticated) may register.
    from tests.conftest import MemoryTestClient  # noqa: F401  (type clarity)

    anon = TestClient(app)
    response = anon.post(
        "/oauth/register",
        json={"client_name": "OpenCode", "redirect_uris": [REDIRECT]},
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def _approve(
    owner_client: TestClient,
    client_id: str,
    scope_id: str,
    verifier: str,
    caps: str = "memory:read memory:write",
) -> str:
    response = owner_client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            "scope_id": scope_id,
            "capabilities": caps,
            "response_mode": "json",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["code"]


def test_oauth_full_flow(app: FastAPI, workspace: WorkspaceInfo, owner_client: TestClient) -> None:
    client_id = _register(app, workspace)
    verifier = secrets.token_urlsafe(32)
    code = _approve(owner_client, client_id, str(workspace.personal_scope_id), verifier)

    anon = TestClient(app)
    token_resp = anon.post(
        "/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
        },
    )
    assert token_resp.status_code == 200, token_resp.text
    tokens = token_resp.json()
    access = tokens["access_token"]
    assert access.startswith("mcp_at_")
    assert tokens["token_type"] == "Bearer"

    # The OAuth access token works like a scoped credential on REST.
    oauth_client = TestClient(app, headers={"Authorization": f"Bearer {access}"})
    scopes = oauth_client.get("/v1/scopes")
    assert scopes.status_code == 200, scopes.text
    assert any(s["id"] == str(workspace.personal_scope_id) for s in scopes.json()["items"])

    remember = oauth_client.post(
        "/v1/memories",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "type": "fact",
            "content": "OAuth device remembers this fact",
        },
    )
    assert remember.status_code == 201, remember.text

    # Reusing the code must fail (single-use).
    replay = anon.post(
        "/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
        },
    )
    assert replay.status_code == 400

    # Refresh rotates; the old refresh token dies on reuse.
    refresh_resp = anon.post(
        "/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refresh_resp.status_code == 200, refresh_resp.text
    rotated = refresh_resp.json()
    assert rotated["access_token"] != access
    reuse = anon.post(
        "/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert reuse.status_code == 400

    # The rotated access token still works.
    oauth2 = TestClient(app, headers={"Authorization": f"Bearer {rotated['access_token']}"})
    assert oauth2.get("/v1/scopes").status_code == 200


def test_oauth_pkce_mismatch(
    app: FastAPI, workspace: WorkspaceInfo, owner_client: TestClient
) -> None:
    client_id = _register(app, workspace)
    verifier = secrets.token_urlsafe(32)
    code = _approve(owner_client, client_id, str(workspace.personal_scope_id), verifier)
    anon = TestClient(app)
    bad = anon.post(
        "/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": secrets.token_urlsafe(32),
            "redirect_uri": REDIRECT,
        },
    )
    assert bad.status_code == 400


def test_oauth_revoke_and_credential_cascade(
    app: FastAPI, engine: Engine, workspace: WorkspaceInfo, owner_client: TestClient
) -> None:
    client_id = _register(app, workspace)
    verifier = secrets.token_urlsafe(32)
    code = _approve(owner_client, client_id, str(workspace.personal_scope_id), verifier)
    anon = TestClient(app)
    tokens = anon.post(
        "/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
        },
    ).json()
    access = tokens["access_token"]

    # Public revocation kills the access token.
    assert anon.post("/oauth/revoke", json={"token": access}).status_code == 200
    oauth_client = TestClient(app, headers={"Authorization": f"Bearer {access}"})
    assert oauth_client.get("/v1/scopes").status_code == 401

    # A fresh device token dies when its underlying credential is revoked.
    verifier2 = secrets.token_urlsafe(32)
    code2 = _approve(owner_client, client_id, str(workspace.personal_scope_id), verifier2)
    tokens2 = anon.post(
        "/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code2,
            "code_verifier": verifier2,
            "redirect_uri": REDIRECT,
        },
    ).json()
    access2 = tokens2["access_token"]

    from sqlalchemy import select

    from memory_platform.auth.tokens import hash_token
    from memory_platform.oauth_tables import oauth_access_tokens

    with engine.begin() as conn:
        row = (
            conn.execute(
                select(oauth_access_tokens.c.credential_id).where(
                    oauth_access_tokens.c.token_hash == hash_token(access2)
                )
            )
            .mappings()
            .one()
        )
    credential_id = str(row["credential_id"])
    revoke = owner_client.delete(f"/v1/credentials/{credential_id}")
    assert revoke.status_code == 200, revoke.text
    oauth_client2 = TestClient(app, headers={"Authorization": f"Bearer {access2}"})
    assert oauth_client2.get("/v1/scopes").status_code == 401


def test_oauth_metadata_and_authorize_guard(
    app: FastAPI, workspace: WorkspaceInfo, make_agent_client: object
) -> None:
    anon = TestClient(app)
    assert anon.get("/.well-known/oauth-protected-resource").status_code == 200
    assert anon.get("/.well-known/oauth-authorization-server").status_code == 200

    # Bad redirect URIs are rejected at registration.
    bad = anon.post(
        "/oauth/register",
        json={"client_name": "Evil", "redirect_uris": ["https://evil.test/cb#frag"]},
    )
    assert bad.status_code == 400

    # Non-admin callers cannot approve authorize codes.
    from collections.abc import Callable, Sequence

    factory = make_agent_client  # type: ignore[assignment]
    assert isinstance(factory, Callable)
    agent = factory([workspace.personal_scope_id], ["memory:read"])  # type: ignore[operator]
    client_id = _register(app, workspace)
    verifier = secrets.token_urlsafe(32)
    denied = agent.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            "scope_id": str(workspace.personal_scope_id),
            "response_mode": "json",
        },
    )
    assert denied.status_code == 403
    _ = Sequence  # silence unused import in some editors
