"""Who is calling: the `Principal` (contract section 8).

A principal is built from a credential on every request. It carries the grants
({scope_id: capabilities}). All memory access goes through `require`, so the rule
"concealed scope -> 404, visible scope without the capability -> 403" lives in one place.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Connection, or_, select

from memory_platform.auth.tokens import hash_token
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.tables import actors, credential_grants, credentials


@dataclass(frozen=True)
class Principal:
    actor_id: UUID
    actor_kind: ActorKind
    workspace_id: UUID
    credential_id: UUID
    is_admin: bool
    # scope_id -> the capabilities this credential has on that scope.
    grants: Mapping[UUID, frozenset[Capability]]

    def scopes_with(self, cap: Capability) -> frozenset[UUID]:
        """All scope ids where this principal holds `cap`."""
        return frozenset(scope_id for scope_id, caps in self.grants.items() if cap in caps)

    def has_grant(self, scope_id: UUID) -> bool:
        """True if the principal has ANY capability on the scope (i.e. the scope is visible)."""
        return bool(self.grants.get(scope_id))

    def require(self, scope_id: UUID, cap: Capability) -> None:
        """Raise unless the principal holds `cap` on `scope_id`.

        - No grant at all on the scope -> `not_found` (404). We hide that the scope exists.
        - Some grant, but not `cap`    -> `forbidden` (403). The scope is already visible.
        """
        caps = self.grants.get(scope_id)
        if not caps:
            raise AppError(ErrorCode.not_found, "Not found.")
        if cap not in caps:
            raise AppError(
                ErrorCode.forbidden,
                f"Missing capability {cap.value} on this scope.",
                details={"required_capability": cap.value},
            )


def resolve_principal(conn: Connection, token: str) -> Principal:
    """Resolve current credential validity and grants in the caller's read transaction."""
    row = (
        conn.execute(
            select(
                credentials.c.id,
                credentials.c.actor_id,
                credentials.c.workspace_id,
                credentials.c.is_admin,
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
                credentials.c.token_hash == hash_token(token),
                credentials.c.revoked_at.is_(None),
                or_(
                    credentials.c.expires_at.is_(None),
                    credentials.c.expires_at > datetime.now(UTC),
                ),
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        # All invalid-token cases share a message to avoid revealing credential status.
        raise AppError(ErrorCode.unauthenticated, "Invalid credentials.")
    grant_rows = conn.execute(
        select(credential_grants.c.scope_id, credential_grants.c.capabilities).where(
            credential_grants.c.credential_id == row["id"],
            credential_grants.c.workspace_id == row["workspace_id"],
        )
    ).mappings()
    return Principal(
        actor_id=row["actor_id"],
        actor_kind=ActorKind(row["kind"]),
        workspace_id=row["workspace_id"],
        credential_id=row["id"],
        is_admin=row["is_admin"],
        grants={
            grant["scope_id"]: frozenset(Capability(cap) for cap in grant["capabilities"])
            for grant in grant_rows
        },
    )
