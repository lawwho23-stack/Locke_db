"""MCP OAuth bridge: DCR clients, owner-approved codes, short-lived tokens.

Owner-only, no public signup. Approvals create a per-device agent credential
through the existing credential service, so scope checks, grant revisions and
revocation keep working without duplicated rules.
"""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import Connection, func, insert, select, update

from memory_platform.api.schemas import CredentialCreateRequest, GrantIn
from memory_platform.auth.principal import Principal
from memory_platform.auth.tokens import (
    generate_oauth_access_token,
    generate_oauth_code,
    generate_oauth_refresh_token,
    hash_token,
)
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.oauth_tables import (
    oauth_access_tokens,
    oauth_auth_codes,
    oauth_clients,
    oauth_refresh_tokens,
)
from memory_platform.services.credentials import create_credential
from memory_platform.services.idempotency import record_activity
from memory_platform.tables import actors, credential_grants, credentials

# skill:write stays owner-CLI-only; OAuth devices may not mint it.
_OAUTH_FORBIDDEN_CAPS = {Capability.skill_write}

_MAX_REDIRECT_URIS = 10
_MAX_REDIRECT_LEN = 2000
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _validate_redirect_uri(value: str) -> str:
    """Accept https URLs or loopback http URLs; reject fragments and empties."""
    text = value.strip()
    if not text or len(text) > _MAX_REDIRECT_LEN or "#" in text:
        raise AppError(ErrorCode.bad_request, "Invalid redirect URI.")
    parts = urlsplit(text)
    if not parts.scheme or not parts.hostname:
        raise AppError(ErrorCode.bad_request, "Invalid redirect URI.")
    if parts.scheme == "https":
        return text
    if parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS:
        return text
    raise AppError(ErrorCode.bad_request, "Redirect URI must be https or loopback http.")


def _validate_challenge(value: str) -> str:
    """PKCE S256 challenges are 43-128 base64url characters."""
    text = value.strip()
    if len(text) < 43 or len(text) > 128:
        raise AppError(ErrorCode.bad_request, "Invalid code challenge.")
    allowed = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    if any(ch not in allowed for ch in text):
        raise AppError(ErrorCode.bad_request, "Invalid code challenge.")
    return text


def _verify_pkce(verifier: str, challenge: str) -> bool:
    """Return True when S256(verifier) matches the stored challenge."""
    text = verifier.strip()
    if len(text) < 43 or len(text) > 128:
        return False
    digest = hashlib.sha256(text.encode("ascii", errors="strict")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets_compare(computed, challenge)


def secrets_compare(left: str, right: str) -> bool:
    """Constant-time string compare without importing hmac at call sites."""
    import hmac as _hmac

    return _hmac.compare_digest(left, right)


def parse_capabilities(values: list[str]) -> list[Capability]:
    """Parse capability strings, rejecting unknowns, repeats and skill:write."""
    try:
        caps = [Capability(v) for v in values]
    except ValueError:
        raise AppError(ErrorCode.bad_request, "Unknown capability.") from None
    if len(set(caps)) != len(caps) or not caps:
        raise AppError(ErrorCode.bad_request, "Capabilities must be unique and non-empty.")
    if _OAUTH_FORBIDDEN_CAPS & set(caps):
        raise AppError(ErrorCode.forbidden, "Owner approval required.")
    return caps


def register_client(conn: Connection, *, client_name: str, redirect_uris: list[str]) -> UUID:
    """Store a dynamic-client-registration record and return its id."""
    name = client_name.strip()
    if not name or len(name) > 100:
        raise AppError(ErrorCode.bad_request, "Client name must be 1-100 characters.")
    if not redirect_uris or len(redirect_uris) > _MAX_REDIRECT_URIS:
        raise AppError(ErrorCode.bad_request, "Provide 1-10 redirect URIs.")
    cleaned = [_validate_redirect_uri(uri) for uri in redirect_uris]
    if len(set(cleaned)) != len(cleaned):
        raise AppError(ErrorCode.bad_request, "Redirect URIs must be unique.")
    client_id = uuid4()
    conn.execute(
        insert(oauth_clients).values(id=client_id, client_name=name, redirect_uris=cleaned)
    )
    return client_id


def approve_authorize(
    conn: Connection,
    principal: Principal,
    *,
    client_id: UUID,
    redirect_uri: str,
    scope_id: UUID,
    capabilities: list[Capability],
    code_challenge: str,
    code_ttl_seconds: int,
    request_id: str,
) -> str:
    """Owner approval: mint a per-device credential and bind a one-time code to it."""
    if not principal.is_admin or principal.actor_kind != ActorKind.owner:
        raise AppError(ErrorCode.forbidden, "Admin credential required.")
    if _OAUTH_FORBIDDEN_CAPS & set(capabilities):
        raise AppError(ErrorCode.forbidden, "Owner approval required.")
    client = (
        conn.execute(select(oauth_clients).where(oauth_clients.c.id == client_id))
        .mappings()
        .one_or_none()
    )
    if client is None:
        raise AppError(ErrorCode.not_found, "Not found.")
    if redirect_uri not in list(client["redirect_uris"]):
        raise AppError(ErrorCode.bad_request, "Redirect URI is not registered.")
    challenge = _validate_challenge(code_challenge)
    # Reuse credential issuance so grant checks, revision bumps and audit stay identical.
    created = create_credential(
        conn,
        principal,
        CredentialCreateRequest(
            display_name=f"oauth:{client['client_name']}"[:100],
            grants=[GrantIn(scope_id=scope_id, capabilities=capabilities)],
        ),
        request_id=request_id,
    )
    code = generate_oauth_code()
    conn.execute(
        insert(oauth_auth_codes).values(
            code_hash=hash_token(code),
            client_id=client_id,
            workspace_id=principal.workspace_id,
            credential_id=created.id,
            scope_id=scope_id,
            capabilities=[cap.value for cap in capabilities],
            code_challenge=challenge,
            redirect_uri=redirect_uri,
            expires_at=datetime.now(UTC) + timedelta(seconds=code_ttl_seconds),
        )
    )
    record_activity(
        conn,
        workspace_id=principal.workspace_id,
        actor_id=principal.actor_id,
        action="oauth.authorize",
        target_ids=(created.id, client_id),
        scope_id=scope_id,
        request_id=request_id,
        result="ok",
    )
    return code


def exchange_code(
    conn: Connection,
    *,
    client_id: UUID,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    access_ttl_seconds: int,
    refresh_ttl_days: int,
    request_id: str,
) -> tuple[str, str, int, UUID, list[str]]:
    """Swap a one-time code for an access/refresh pair. Returns tokens + scope info."""
    row = (
        conn.execute(
            select(oauth_auth_codes)
            .where(oauth_auth_codes.c.code_hash == hash_token(code))
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(ErrorCode.bad_request, "Invalid grant.")
    if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
        raise AppError(ErrorCode.bad_request, "Invalid grant.")
    if row["used_at"] is not None:
        raise AppError(ErrorCode.bad_request, "Invalid grant.")
    expires = row["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= datetime.now(UTC):
        raise AppError(ErrorCode.bad_request, "Invalid grant.")
    if not _verify_pkce(code_verifier, row["code_challenge"]):
        raise AppError(ErrorCode.bad_request, "Invalid grant.")
    _require_credential_usable(conn, row["workspace_id"], row["credential_id"])
    conn.execute(
        update(oauth_auth_codes)
        .where(oauth_auth_codes.c.code_hash == hash_token(code))
        .values(used_at=func.now())
    )
    access = generate_oauth_access_token()
    refresh = generate_oauth_refresh_token()
    now = datetime.now(UTC)
    conn.execute(
        insert(oauth_access_tokens).values(
            token_hash=hash_token(access),
            client_id=client_id,
            workspace_id=row["workspace_id"],
            credential_id=row["credential_id"],
            scope_id=row["scope_id"],
            capabilities=row["capabilities"],
            expires_at=now + timedelta(seconds=access_ttl_seconds),
        )
    )
    conn.execute(
        insert(oauth_refresh_tokens).values(
            token_hash=hash_token(refresh),
            client_id=client_id,
            workspace_id=row["workspace_id"],
            credential_id=row["credential_id"],
            scope_id=row["scope_id"],
            capabilities=row["capabilities"],
            expires_at=now + timedelta(days=refresh_ttl_days),
        )
    )
    record_activity(
        conn,
        workspace_id=row["workspace_id"],
        actor_id=None,
        action="oauth.token",
        target_ids=(row["credential_id"], client_id),
        scope_id=row["scope_id"],
        request_id=request_id,
        result="ok",
    )
    return access, refresh, access_ttl_seconds, row["scope_id"], list(row["capabilities"])


def refresh_grant(
    conn: Connection,
    *,
    client_id: UUID,
    refresh_token: str,
    access_ttl_seconds: int,
    refresh_ttl_days: int,
    request_id: str,
) -> tuple[str, str, int, UUID, list[str]]:
    """Rotate a refresh token and return a fresh access/refresh pair."""
    digest = hash_token(refresh_token)
    row = (
        conn.execute(
            select(oauth_refresh_tokens)
            .where(oauth_refresh_tokens.c.token_hash == digest)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(ErrorCode.bad_request, "Invalid grant.")
    if row["client_id"] != client_id:
        raise AppError(ErrorCode.bad_request, "Invalid grant.")
    if row["revoked_at"] is not None or row["rotated_to"] is not None:
        raise AppError(ErrorCode.bad_request, "Invalid grant.")
    expires = row["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= datetime.now(UTC):
        raise AppError(ErrorCode.bad_request, "Invalid grant.")
    _require_credential_usable(conn, row["workspace_id"], row["credential_id"])
    access = generate_oauth_access_token()
    rotated = generate_oauth_refresh_token()
    now = datetime.now(UTC)
    conn.execute(
        update(oauth_refresh_tokens)
        .where(oauth_refresh_tokens.c.token_hash == digest)
        .values(revoked_at=func.now(), rotated_to=hash_token(rotated))
    )
    conn.execute(
        insert(oauth_access_tokens).values(
            token_hash=hash_token(access),
            client_id=client_id,
            workspace_id=row["workspace_id"],
            credential_id=row["credential_id"],
            scope_id=row["scope_id"],
            capabilities=row["capabilities"],
            expires_at=now + timedelta(seconds=access_ttl_seconds),
        )
    )
    conn.execute(
        insert(oauth_refresh_tokens).values(
            token_hash=hash_token(rotated),
            client_id=client_id,
            workspace_id=row["workspace_id"],
            credential_id=row["credential_id"],
            scope_id=row["scope_id"],
            capabilities=row["capabilities"],
            expires_at=now + timedelta(days=refresh_ttl_days),
        )
    )
    record_activity(
        conn,
        workspace_id=row["workspace_id"],
        actor_id=None,
        action="oauth.refresh",
        target_ids=(row["credential_id"], client_id),
        scope_id=row["scope_id"],
        request_id=request_id,
        result="ok",
    )
    return access, rotated, access_ttl_seconds, row["scope_id"], list(row["capabilities"])


def revoke_token(conn: Connection, token: str) -> bool:
    """Revoke an access or refresh token by its plain value. Returns found or not."""
    digest = hash_token(token)
    for table in (oauth_access_tokens, oauth_refresh_tokens):
        row = (
            conn.execute(
                select(table.c.token_hash, table.c.revoked_at).where(table.c.token_hash == digest)
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            if row["revoked_at"] is None:
                conn.execute(
                    update(table).where(table.c.token_hash == digest).values(revoked_at=func.now())
                )
            return True
    return False


def cascade_revoke_for_credential(
    conn: Connection, *, workspace_id: UUID, credential_id: UUID
) -> None:
    """Mark all OAuth tokens for a revoked credential as revoked."""
    for table in (oauth_access_tokens, oauth_refresh_tokens):
        conn.execute(
            update(table)
            .where(
                table.c.workspace_id == workspace_id,
                table.c.credential_id == credential_id,
                table.c.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
        )


def _require_credential_usable(conn: Connection, workspace_id: UUID, credential_id: UUID) -> None:
    """Refuse OAuth use when the underlying credential was revoked or expired."""
    row = (
        conn.execute(
            select(credentials.c.revoked_at, credentials.c.expires_at).where(
                credentials.c.id == credential_id,
                credentials.c.workspace_id == workspace_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(ErrorCode.bad_request, "Invalid grant.")
    if row["revoked_at"] is not None:
        raise AppError(ErrorCode.bad_request, "Invalid grant.")
    expires = row["expires_at"]
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            raise AppError(ErrorCode.bad_request, "Invalid grant.")


def resolve_oauth_principal(conn: Connection, token: str) -> Principal:
    """Resolve an OAuth access token to a narrowed principal (same shape as mem_)."""
    row = (
        conn.execute(
            select(oauth_access_tokens).where(oauth_access_tokens.c.token_hash == hash_token(token))
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(ErrorCode.unauthenticated, "Invalid credentials.")
    if row["revoked_at"] is not None:
        raise AppError(ErrorCode.unauthenticated, "Invalid credentials.")
    expires = row["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= datetime.now(UTC):
        raise AppError(ErrorCode.unauthenticated, "Invalid credentials.")
    cred = (
        conn.execute(
            select(
                credentials.c.id,
                credentials.c.actor_id,
                credentials.c.workspace_id,
                credentials.c.is_admin,
                credentials.c.revoked_at,
                credentials.c.expires_at,
                actors.c.kind,
            )
            .select_from(
                credentials.join(
                    actors,
                    (actors.c.id == credentials.c.actor_id)
                    & (actors.c.workspace_id == credentials.c.workspace_id),
                )
            )
            .where(
                credentials.c.id == row["credential_id"],
                credentials.c.workspace_id == row["workspace_id"],
            )
        )
        .mappings()
        .one_or_none()
    )
    if cred is None:
        raise AppError(ErrorCode.unauthenticated, "Invalid credentials.")
    if cred["revoked_at"] is not None:
        raise AppError(ErrorCode.unauthenticated, "Invalid credentials.")
    cred_expires = cred["expires_at"]
    if cred_expires is not None:
        if cred_expires.tzinfo is None:
            cred_expires = cred_expires.replace(tzinfo=UTC)
        if cred_expires <= datetime.now(UTC):
            raise AppError(ErrorCode.unauthenticated, "Invalid credentials.")
    # Narrow to the approved scope/capabilities, intersected with live grants.
    live = conn.execute(
        select(credential_grants.c.scope_id, credential_grants.c.capabilities).where(
            credential_grants.c.credential_id == cred["id"],
            credential_grants.c.workspace_id == cred["workspace_id"],
        )
    ).mappings()
    approved = frozenset(Capability(c) for c in row["capabilities"])
    grants: dict[UUID, frozenset[Capability]] = {}
    for grant in live:
        if grant["scope_id"] == row["scope_id"]:
            narrowed = frozenset(Capability(c) for c in grant["capabilities"]) & approved
            if narrowed:
                grants[grant["scope_id"]] = narrowed
    if not grants:
        raise AppError(ErrorCode.unauthenticated, "Invalid credentials.")
    return Principal(
        actor_id=cred["actor_id"],
        actor_kind=ActorKind(cred["kind"]),
        workspace_id=cred["workspace_id"],
        credential_id=cred["id"],
        is_admin=cred["is_admin"],
        grants=grants,
    )
