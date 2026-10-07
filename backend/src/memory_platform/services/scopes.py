"""Visible scope listing and owner-admin scope creation."""

from sqlalchemy import Connection, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from memory_platform.api.schemas import ScopeCreateRequest, ScopeListResponse, ScopeOut
from memory_platform.auth.principal import Principal
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.idempotency import record_activity
from memory_platform.tables import actors, credential_grants, credentials, scopes


def list_scopes(conn: Connection, principal: Principal) -> ScopeListResponse:
    """List scopes with a grant, using the caller's capabilities without an owner bypass."""
    visible_ids = [scope_id for scope_id, caps in principal.grants.items() if caps]
    rows = conn.execute(
        select(scopes)
        .where(
            scopes.c.workspace_id == principal.workspace_id,
            scopes.c.id.in_(visible_ids),
        )
        .order_by(scopes.c.name, scopes.c.id)
    ).mappings()
    return ScopeListResponse(
        items=[
            ScopeOut(
                id=row["id"],
                kind=row["kind"],
                name=row["name"],
                revision=row["revision"],
                capabilities=[cap for cap in Capability if cap in principal.grants[row["id"]]],
            )
            for row in rows
        ]
    )


def create_scope(
    conn: Connection, principal: Principal, req: ScopeCreateRequest, *, request_id: str
) -> ScopeOut:
    """Create a scope, granting all capabilities to every non-revoked owner admin."""
    if not principal.is_admin:
        raise AppError(ErrorCode.forbidden, "Admin credential required.")
    row = (
        conn.execute(
            pg_insert(scopes)
            .values(
                workspace_id=principal.workspace_id,
                kind=req.kind.value,
                name=req.name,
            )
            .on_conflict_do_nothing(constraint="uq_scopes_workspace_name")
            .returning(scopes)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(ErrorCode.bad_request, "Scope name already exists.")
    admin_ids = (
        conn.execute(
            select(credentials.c.id)
            .join(
                actors,
                credentials.c.actor_id == actors.c.id,
            )
            .where(
                credentials.c.workspace_id == principal.workspace_id,
                credentials.c.is_admin.is_(True),
                credentials.c.revoked_at.is_(None),
                actors.c.kind == ActorKind.owner.value,
            )
        )
        .scalars()
        .all()
    )
    capabilities = [cap.value for cap in Capability]
    if admin_ids:
        conn.execute(
            insert(credential_grants),
            [
                {
                    "workspace_id": principal.workspace_id,
                    "credential_id": credential_id,
                    "scope_id": row["id"],
                    "capabilities": capabilities,
                }
                for credential_id in admin_ids
            ],
        )
        conn.execute(
            update(scopes)
            .where(scopes.c.id == row["id"])
            .values(
                grant_revision=scopes.c.grant_revision + 1,
            )
        )
    record_activity(
        conn,
        workspace_id=principal.workspace_id,
        actor_id=principal.actor_id,
        action="scope.create",
        target_ids=(row["id"],),
        scope_id=row["id"],
        request_id=request_id,
        result="ok",
    )
    return ScopeOut(
        id=row["id"],
        kind=row["kind"],
        name=row["name"],
        revision=row["revision"],
        capabilities=list(Capability) if principal.credential_id in admin_ids else [],
    )
