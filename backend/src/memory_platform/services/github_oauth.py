"""GitHub sign-in for the single owner (no public signup).

Flow: dashboard approval page -> backend login URL -> github.com ->
backend callback -> dashboard finish page. The callback mints a short-lived
owner admin credential and hands its plain token to the dashboard once, in the
URL fragment (never logged server-side). The dashboard stores it in the
encrypted owner session, exactly like a pasted owner credential.
"""

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit
from uuid import UUID
from uuid import uuid4 as new_uuid

from sqlalchemy import Connection, func, insert, select, update

from memory_platform.auth.tokens import display_prefix, generate_token, hash_token
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.oauth_tables import oauth_login_states
from memory_platform.services.idempotency import record_activity
from memory_platform.tables import actors, credential_grants, credentials, scopes, workspaces

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"

STATE_TTL_SECONDS = 600
MAX_NEXT_LEN = 2000


def dashboard_origin(authorize_url: str) -> str:
    """Origin (scheme + host) of the dashboard approval page URL."""
    parts = urlsplit(authorize_url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AppError(ErrorCode.bad_request, "Dashboard approval URL is not configured.")
    if parts.scheme == "http" and parts.hostname not in {
        "localhost",
        "127.0.0.1",
        "[::1]",
        "::1",
    }:
        raise AppError(ErrorCode.bad_request, "Dashboard approval URL is not configured.")
    return f"{parts.scheme}://{parts.netloc}"


def clean_next_url(value: str | None, origin: str) -> str:
    """The dashboard page to return to. Must stay on the dashboard origin."""
    fallback = f"{origin}/"
    if not value or len(value) > MAX_NEXT_LEN or "#" in value:
        return fallback
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https"):
        return fallback
    if f"{parts.scheme}://{parts.netloc}" != origin:
        raise AppError(ErrorCode.bad_request, "Return address is not allowed.")
    return value.strip()


def start_login(
    conn: Connection, *, github_client_id: str, issuer: str, origin: str, next_url: str | None
) -> str:
    """Store a one-time state and return the github.com redirect target."""
    target = clean_next_url(next_url, origin)
    state = secrets.token_urlsafe(32)
    conn.execute(
        insert(oauth_login_states).values(
            state_hash=hash_token(state),
            provider="github",
            next_url=target,
            expires_at=datetime.now(UTC) + timedelta(seconds=STATE_TTL_SECONDS),
        )
    )
    query = urlencode(
        {
            "client_id": github_client_id,
            "redirect_uri": f"{issuer}/oauth/github/callback",
            "scope": "read:user user:email",
            "state": state,
        }
    )
    return f"{GITHUB_AUTHORIZE_URL}?{query}"


def consume_state(conn: Connection, state: str) -> str | None:
    """Single-use a login state. Returns the stored return address."""
    row = (
        conn.execute(
            select(oauth_login_states)
            .where(oauth_login_states.c.state_hash == hash_token(state))
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["provider"] != "github":
        raise AppError(ErrorCode.bad_request, "Invalid sign-in state.")
    if row["used_at"] is not None:
        raise AppError(ErrorCode.bad_request, "Invalid sign-in state.")
    expires = row["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= datetime.now(UTC):
        raise AppError(ErrorCode.bad_request, "Sign-in took too long. Start again.")
    conn.execute(
        update(oauth_login_states)
        .where(oauth_login_states.c.state_hash == hash_token(state))
        .values(used_at=func.now())
    )
    stored = row["next_url"]
    return str(stored) if stored is not None else None


def pick_verified_email(emails: object) -> str | None:
    """Best verified address: primary first, then any verified one."""
    if not isinstance(emails, list):
        return None
    fallback: str | None = None
    for entry in emails:
        if not isinstance(entry, dict):
            continue
        address = entry.get("email")
        if not isinstance(address, str) or "@" not in address:
            continue
        if entry.get("verified") is not True:
            continue
        if entry.get("primary") is True:
            return address
        fallback = fallback or address
    return fallback


def check_allowlist(configured: str | None, actual: str | None) -> str:
    """Only the allowlisted owner email may sign in with GitHub."""
    if not configured:
        raise AppError(ErrorCode.bad_request, "Sign-in is unavailable.")
    if not actual or actual.casefold() != configured.strip().casefold():
        raise AppError(ErrorCode.forbidden, "This GitHub account is not authorized.")
    return actual


def mint_session_credential(
    conn: Connection, *, ttl_hours: int, request_id: str
) -> tuple[UUID, str]:
    """Create a short-lived owner admin credential covering every scope.

    Production holds a single workspace (bootstrap refuses a second one), so the
    oldest workspace is the owner's. Tests keep many workspaces side by side;
    they must read the minted credential's workspace back instead of assuming.
    """
    workspace_id = conn.scalar(
        select(workspaces.c.id).order_by(workspaces.c.created_at).limit(1)
    )
    if workspace_id is None:
        raise AppError(ErrorCode.bad_request, "No workspace yet. Bootstrap first.")
    owner_id = conn.scalar(
        select(actors.c.id).where(
            actors.c.workspace_id == workspace_id, actors.c.kind == ActorKind.owner.value
        )
    )
    if owner_id is None:
        raise AppError(ErrorCode.bad_request, "No owner yet. Bootstrap first.")
    scope_ids = list(
        conn.execute(select(scopes.c.id).where(scopes.c.workspace_id == workspace_id)).scalars()
    )
    if not scope_ids:
        raise AppError(ErrorCode.bad_request, "No scope yet. Bootstrap first.")
    credential_id = new_uuid()
    token = generate_token()
    conn.execute(
        insert(credentials).values(
            id=credential_id,
            actor_id=owner_id,
            workspace_id=workspace_id,
            token_hash=hash_token(token),
            token_prefix=display_prefix(token),
            is_admin=True,
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
        )
    )
    conn.execute(
        insert(credential_grants),
        [
            {
                "credential_id": credential_id,
                "workspace_id": workspace_id,
                "scope_id": scope_id,
                "capabilities": [cap.value for cap in Capability],
            }
            for scope_id in scope_ids
        ],
    )
    conn.execute(
        update(scopes)
        .where(scopes.c.id.in_(scope_ids))
        .values(grant_revision=scopes.c.grant_revision + 1)
    )
    record_activity(
        conn,
        workspace_id=workspace_id,
        actor_id=owner_id,
        action="oauth.github_login",
        target_ids=(credential_id,),
        scope_id=None,
        request_id=request_id,
        result="ok",
    )
    return credential_id, token
