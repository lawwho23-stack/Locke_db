"""Remember explicit content, preserving provenance and forgotten-content barriers."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, delete, insert, or_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError

from memory_platform.api.schemas import MemoryWriteResponse, RememberRequest
from memory_platform.auth.principal import Principal
from memory_platform.config import Settings
from memory_platform.core.normalize import (
    content_hash,
    normalize_fact_key,
    normalize_text,
    suppression_hmac,
)
from memory_platform.core.policy import initial_state, trust_for_actor
from memory_platform.enums import ActorKind, Capability, MemoryState, RelationOrigin, RelationType
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.evidence import add_assertion_evidence
from memory_platform.services.locking import lock_scope
from memory_platform.services.supersede import (
    find_active_fact_for_update,
    is_fact_key_race,
    link,
    mark_superseded,
)
from memory_platform.services.types import WriteOutcome
from memory_platform.services.versions import append_version
from memory_platform.tables import forget_suppressions, memories, scopes


def check_suppression(
    conn: Connection,
    principal: Principal,
    scope_id: UUID,
    content: str,
    fact_key: str | None,
    hmac_key: bytes,
    *,
    clear_for_owner: bool = True,
) -> None:
    matches = [
        forget_suppressions.c.content_hmac == suppression_hmac(normalize_text(content), hmac_key)
    ]
    if fact_key is not None:
        matches.append(
            forget_suppressions.c.fact_key_hmac
            == suppression_hmac(normalize_fact_key(fact_key), hmac_key)
        )
    condition = (
        (forget_suppressions.c.workspace_id == principal.workspace_id)
        & (forget_suppressions.c.scope_id == scope_id)
        & or_(*matches)
    )
    if conn.execute(select(forget_suppressions.c.id).where(condition)).first() is None:
        return
    if principal.actor_kind == ActorKind.agent:
        raise AppError(ErrorCode.memory_suppressed, "This memory has been forgotten in this scope.")
    if clear_for_owner:
        conn.execute(delete(forget_suppressions).where(condition))


def write_body(row: RowMapping, **flags: Any) -> dict[str, Any]:
    return MemoryWriteResponse(
        id=row["id"],
        scope_id=row["scope_id"],
        state=row["state"],
        version=row["version"],
        trust=row["trust"],
        **flags,
    ).model_dump(mode="json")


def remember(
    conn: Connection,
    principal: Principal,
    req: RememberRequest,
    *,
    request_id: str,
    hmac_key: bytes,
    settings: Settings | None = None,
) -> WriteOutcome:
    principal.require(req.scope_id, Capability.memory_write)
    # Reads stay concurrent under the shared scope lock; the workspace quota lock
    # is taken just before the insert so concurrent remembers are serialized only
    # at the write, preserving the retryable fact-key race contract.
    lock_scope(conn, req.scope_id, shared=True)
    if (
        conn.execute(
            select(scopes.c.id).where(
                scopes.c.id == req.scope_id, scopes.c.workspace_id == principal.workspace_id
            )
        ).first()
        is None
    ):
        raise AppError(ErrorCode.not_found, "Not found.")
    normalized = normalize_text(req.content)
    fact_key = None if req.fact_key is None else normalize_fact_key(req.fact_key)
    check_suppression(conn, principal, req.scope_id, req.content, fact_key, hmac_key)
    digest = content_hash(normalized)
    duplicate = (
        conn.execute(
            select(memories)
            .where(
                memories.c.workspace_id == principal.workspace_id,
                memories.c.scope_id == req.scope_id,
                memories.c.type == req.type.value,
                memories.c.content_hash == digest,
                memories.c.state == MemoryState.active.value,
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if duplicate is not None:
        add_assertion_evidence(
            conn, memory_id=duplicate["id"], actor_id=principal.actor_id, request_id=request_id
        )
        return WriteOutcome(
            200,
            write_body(duplicate, deduplicated=True),
            "memory.remember",
            None,
            (duplicate["id"],),
        )

    trust = trust_for_actor(principal.actor_kind)
    state = initial_state(trust, req.type)
    active = (
        None
        if fact_key is None
        else find_active_fact_for_update(
            conn, scope_id=req.scope_id, mtype=req.type, fact_key=fact_key
        )
    )
    superseded_id = conflict_id = None
    if settings is not None:
        from memory_platform.services.quotas import enforce_quota

        enforce_quota(conn, principal.workspace_id, settings, "memories")
    if active is not None:
        if principal.actor_kind == ActorKind.owner:
            mark_superseded(conn, memory_id=active.id, actor_id=principal.actor_id)
            superseded_id, state = active.id, MemoryState.active
        else:
            conflict_id, state = active.id, MemoryState.draft
    try:
        row = (
            conn.execute(
                insert(memories)
                .values(
                    id=uuid4(),
                    workspace_id=principal.workspace_id,
                    scope_id=req.scope_id,
                    type=req.type.value,
                    content=req.content,
                    content_hash=digest,
                    fact_key=fact_key,
                    labels=req.labels,
                    trust=trust.value,
                    importance=req.importance,
                    state=state.value,
                    valid_from=req.valid_from,
                    valid_until=req.valid_until,
                    version=1,
                    created_by=principal.actor_id,
                )
                .returning(memories)
            )
            .mappings()
            .one()
        )
    except IntegrityError as exc:
        if is_fact_key_race(exc):
            raise AppError(
                ErrorCode.fact_key_race,
                "An active fact changed; read again and retry.",
                retry_after=1,
            ) from None
        raise
    append_version(
        conn,
        memory_id=row["id"],
        version=1,
        content=row["content"],
        state=state,
        labels=req.labels,
        fact_key=fact_key,
        actor_id=principal.actor_id,
        reason="created",
    )
    add_assertion_evidence(
        conn, memory_id=row["id"], actor_id=principal.actor_id, request_id=request_id
    )
    target_id = superseded_id or conflict_id
    if target_id is not None:
        link(
            conn,
            workspace_id=principal.workspace_id,
            from_id=row["id"],
            to_id=target_id,
            rtype=RelationType.supersedes if superseded_id else RelationType.conflicts_with,
            origin=RelationOrigin.system,
            actor_id=principal.actor_id,
        )
    return WriteOutcome(
        201,
        write_body(row, superseded_id=superseded_id, conflict_with_id=conflict_id),
        "memory.remember",
        req.scope_id,
        (row["id"],) + (() if superseded_id is None else (superseded_id,)),
    )
