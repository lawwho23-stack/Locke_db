"""The ONE scope-filter path for memory reads and writes (contract sections 7 and 8).

There is no "if owner, skip the check" branch anywhere: the owner has explicit grants like
everyone else. Rule used by every memory route:
  - memory missing, deleted, in another workspace, or in a scope where the caller has NO
    grant at all  -> 404 `not_found` (we hide that it exists);
  - caller has some grant on the scope but lacks the needed capability -> 403 `forbidden`.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Connection, select
from sqlalchemy.engine import RowMapping

from memory_platform.auth.principal import Principal
from memory_platform.enums import Capability, MemoryState
from memory_platform.errors import AppError, ErrorCode
from memory_platform.tables import memories


def fetch_memory_checked(
    conn: Connection,
    principal: Principal,
    memory_id: UUID,
    cap: Capability,
    *,
    for_update: bool = False,
) -> RowMapping:
    """Load one memory row and check the caller may use it with `cap`.

    `SELECT ... FROM memories WHERE id = :id AND workspace_id = :ws AND state <> 'deleted'`
    (add `FOR UPDATE` when `for_update` is True). No row -> `not_found`. Then call
    `principal.require(row["scope_id"], cap)` (which raises `not_found` or `forbidden`).
    Returns the full row as a mapping.
    """
    statement = select(memories).where(
        memories.c.id == memory_id,
        memories.c.workspace_id == principal.workspace_id,
        memories.c.state != MemoryState.deleted.value,
    )
    if for_update:
        statement = statement.with_for_update()
    row = conn.execute(statement).mappings().first()
    if row is None:
        raise AppError(ErrorCode.not_found, "Not found.")
    principal.require(row["scope_id"], cap)
    return row


def readable_scope_ids(principal: Principal, requested: Sequence[UUID] | None) -> list[UUID]:
    """Scope ids a list/search may read.

    - `requested` is None: every scope where the principal holds `memory:read`.
    - Otherwise each requested scope is checked with `principal.require(scope, memory_read)`:
      no grant at all -> `not_found`; grant without read -> `forbidden`.
    """
    if requested is None:
        return sorted(principal.scopes_with(Capability.memory_read), key=str)
    for scope_id in requested:
        principal.require(scope_id, Capability.memory_read)
    return list(requested)
