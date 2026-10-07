"""Credential issuance, revocation and a serialized first-install bootstrap."""

from uuid import UUID, uuid4

from sqlalchemy import Connection, func, insert, select, text, update

from memory_platform.api.schemas import (
    CredentialCreateRequest,
    CredentialCreateResponse,
    CredentialRevokeResponse,
    GrantOut,
)
from memory_platform.auth.principal import Principal
from memory_platform.auth.tokens import display_prefix, generate_token, hash_token
from memory_platform.enums import ActorKind, Capability, ScopeKind
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.idempotency import record_activity
from memory_platform.tables import actors, credential_grants, credentials, scopes, workspaces

# All bootstraps hold this transaction lock, including when no workspace row exists yet.
_BOOTSTRAP_LOCK = 0x4D454D424F4F54


def create_credential(
    conn: Connection, principal: Principal, req: CredentialCreateRequest, *, request_id: str
) -> CredentialCreateResponse:
    """Create an agent and scoped credential, returning the plain token once."""
    if not principal.is_admin:
        raise AppError(ErrorCode.forbidden, "Admin credential required.")
    scope_ids = [grant.scope_id for grant in req.grants]
    allowed_ids = set(
        conn.execute(
            select(scopes.c.id).where(
                scopes.c.workspace_id == principal.workspace_id,
                scopes.c.id.in_(scope_ids),
            )
        ).scalars()
    )
    if allowed_ids != set(scope_ids):
        raise AppError(ErrorCode.not_found, "Not found.")
    for grant in req.grants:
        held = principal.grants.get(grant.scope_id, frozenset())
        if not set(grant.capabilities) <= set(held):
            raise AppError(ErrorCode.forbidden, "Cannot grant capabilities beyond held grant.")
    actor_id = uuid4()
    credential_id = uuid4()
    token = generate_token()
    prefix = display_prefix(token)
    conn.execute(
        insert(actors).values(
            id=actor_id,
            workspace_id=principal.workspace_id,
            kind=ActorKind.agent.value,
            display_name=req.display_name,
        )
    )
    conn.execute(
        insert(credentials).values(
            id=credential_id,
            actor_id=actor_id,
            workspace_id=principal.workspace_id,
            token_hash=hash_token(token),
            token_prefix=prefix,
            is_admin=False,
            expires_at=req.expires_at,
        )
    )
    conn.execute(
        insert(credential_grants),
        [
            {
                "credential_id": credential_id,
                "workspace_id": principal.workspace_id,
                "scope_id": grant.scope_id,
                "capabilities": [cap.value for cap in grant.capabilities],
            }
            for grant in req.grants
        ],
    )
    conn.execute(
        update(scopes)
        .where(scopes.c.id.in_(scope_ids))
        .values(
            grant_revision=scopes.c.grant_revision + 1,
        )
    )
    record_activity(
        conn,
        workspace_id=principal.workspace_id,
        actor_id=principal.actor_id,
        action="credential.create",
        target_ids=(credential_id, actor_id),
        scope_id=None,
        request_id=request_id,
        result="ok",
    )
    return CredentialCreateResponse(
        id=credential_id,
        actor_id=actor_id,
        token=token,
        token_prefix=prefix,
        grants=[
            GrantOut(scope_id=grant.scope_id, capabilities=grant.capabilities)
            for grant in req.grants
        ],
        expires_at=req.expires_at,
    )


def revoke_credential(
    conn: Connection, principal: Principal, credential_id: UUID, *, request_id: str
) -> CredentialRevokeResponse:
    """Revoke once; concurrent and later repeats return the original timestamp."""
    if not principal.is_admin:
        raise AppError(ErrorCode.forbidden, "Admin credential required.")
    if credential_id == principal.credential_id:
        raise AppError(ErrorCode.bad_request, "Cannot revoke the calling credential.")
    row = (
        conn.execute(
            select(credentials)
            .where(
                credentials.c.id == credential_id,
                credentials.c.workspace_id == principal.workspace_id,
            )
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(ErrorCode.not_found, "Not found.")
    if row["revoked_at"] is not None:
        return CredentialRevokeResponse(id=credential_id, revoked_at=row["revoked_at"])
    revoked_at = conn.execute(
        update(credentials)
        .where(credentials.c.id == credential_id)
        .values(
            revoked_at=func.now(),
        )
        .returning(credentials.c.revoked_at)
    ).scalar_one()
    granted_scopes = select(credential_grants.c.scope_id).where(
        credential_grants.c.credential_id == credential_id,
    )
    conn.execute(
        update(scopes)
        .where(scopes.c.id.in_(granted_scopes))
        .values(
            grant_revision=scopes.c.grant_revision + 1,
        )
    )
    record_activity(
        conn,
        workspace_id=principal.workspace_id,
        actor_id=principal.actor_id,
        action="credential.revoke",
        target_ids=(credential_id,),
        scope_id=None,
        request_id=request_id,
        result="ok",
    )
    return CredentialRevokeResponse(id=credential_id, revoked_at=revoked_at)


def bootstrap_workspace(
    conn: Connection, *, owner_name: str, workspace_name: str = "default"
) -> tuple[UUID, str]:
    """Create one workspace, owner, personal scope and admin token on an empty install."""
    conn.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _BOOTSTRAP_LOCK})
    if conn.scalar(select(workspaces.c.id).limit(1)) is not None:
        raise AppError(ErrorCode.bad_request, "A workspace already exists.")
    workspace_id, owner_id, scope_id, credential_id = (uuid4() for _ in range(4))
    token = generate_token()
    conn.execute(insert(workspaces).values(id=workspace_id, name=workspace_name))
    conn.execute(
        insert(actors).values(
            id=owner_id,
            workspace_id=workspace_id,
            kind=ActorKind.owner.value,
            display_name=owner_name,
        )
    )
    conn.execute(
        insert(scopes).values(
            id=scope_id,
            workspace_id=workspace_id,
            kind=ScopeKind.personal.value,
            name="personal",
            grant_revision=1,
        )
    )
    conn.execute(
        insert(credentials).values(
            id=credential_id,
            actor_id=owner_id,
            workspace_id=workspace_id,
            token_hash=hash_token(token),
            token_prefix=display_prefix(token),
            is_admin=True,
        )
    )
    conn.execute(
        insert(credential_grants).values(
            credential_id=credential_id,
            scope_id=scope_id,
            workspace_id=workspace_id,
            capabilities=[cap.value for cap in Capability],
        )
    )
    record_activity(
        conn,
        workspace_id=workspace_id,
        actor_id=owner_id,
        action="workspace.bootstrap",
        target_ids=(workspace_id, credential_id, scope_id),
        scope_id=scope_id,
        request_id=f"bootstrap:{uuid4()}",
        result="ok",
    )
    return workspace_id, token
