"""Version-checked changes and explicit owner promotion."""

from uuid import UUID

from sqlalchemy import Connection, func, select, update
from sqlalchemy.exc import IntegrityError

from memory_platform.api.schemas import UpdateRequest
from memory_platform.auth.principal import Principal
from memory_platform.core.normalize import content_hash, normalize_text
from memory_platform.core.policy import can_transition
from memory_platform.enums import (
    ActorKind,
    Capability,
    MemoryState,
    MemoryType,
    RelationOrigin,
    RelationType,
    Trust,
)
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.access import fetch_memory_checked
from memory_platform.services.evidence import add_assertion_evidence
from memory_platform.services.locking import lock_scope
from memory_platform.services.remember import check_suppression, write_body
from memory_platform.services.supersede import (
    find_active_fact_for_update,
    is_fact_key_race,
    link,
    mark_superseded,
)
from memory_platform.services.types import WriteOutcome
from memory_platform.services.versions import append_version
from memory_platform.tables import memories


def update_memory(
    conn: Connection,
    principal: Principal,
    memory_id: UUID,
    req: UpdateRequest,
    *,
    request_id: str,
    hmac_key: bytes,
) -> WriteOutcome:
    scope_id = conn.execute(
        select(memories.c.scope_id).where(
            memories.c.id == memory_id,
            memories.c.workspace_id == principal.workspace_id,
        )
    ).scalar_one_or_none()
    if scope_id is None:
        raise AppError(ErrorCode.not_found, "Not found.")
    principal.require(scope_id, Capability.memory_write)
    lock_scope(conn, scope_id)
    row = fetch_memory_checked(conn, principal, memory_id, Capability.memory_write, for_update=True)
    fields = req.provided_fields()
    if row["version"] != req.expected_version:
        raise AppError(
            ErrorCode.version_conflict,
            "Memory version changed.",
            details={"current_version": row["version"]},
        )
    if principal.actor_kind == ActorKind.agent and fields & {"state", "fact_key"}:
        raise AppError(ErrorCode.forbidden, "Only the owner may change state or fact_key.")
    if row["state"] not in (MemoryState.active, MemoryState.draft):
        raise AppError(
            ErrorCode.invalid_transition, "Only active or draft memories can be updated."
        )
    promotion = "state" in fields
    if promotion and not can_transition(
        MemoryState(row["state"]), MemoryState.active, principal.actor_kind
    ):
        raise AppError(ErrorCode.invalid_transition, "This state transition is not allowed.")
    values = req.model_dump(exclude_unset=True, exclude={"expected_version"})
    content = values.get("content", row["content"])
    fact_key = values.get("fact_key", row["fact_key"])
    if "content" in fields:
        values["content_hash"] = content_hash(normalize_text(content))
    if principal.actor_kind == ActorKind.agent and "content" in fields:
        check_suppression(conn, principal, row["scope_id"], content, fact_key, hmac_key)
    until = values.get("valid_until", row["valid_until"])
    if until is not None and row["valid_from"] is not None and until <= row["valid_from"]:
        raise AppError(ErrorCode.validation_error, "valid_until must be later than valid_from.")
    superseded_id = None
    new_state = MemoryState(values.get("state", row["state"]))
    if (
        fact_key is not None
        and new_state == MemoryState.active
        and (promotion or "fact_key" in fields)
    ):
        active = find_active_fact_for_update(
            conn, scope_id=row["scope_id"], mtype=MemoryType(row["type"]), fact_key=fact_key
        )
        if active is not None and active.id != memory_id:
            superseded_id = active.id
            mark_superseded(conn, memory_id=active.id, actor_id=principal.actor_id)
            link(
                conn,
                workspace_id=principal.workspace_id,
                from_id=memory_id,
                to_id=active.id,
                rtype=RelationType.supersedes,
                origin=RelationOrigin.system,
                actor_id=principal.actor_id,
            )
    if promotion:
        values["trust"] = Trust.owner_asserted.value
        add_assertion_evidence(
            conn, memory_id=memory_id, actor_id=principal.actor_id, request_id=request_id
        )
    values.update(version=memories.c.version + 1, updated_at=func.now())
    try:
        changed = (
            conn.execute(
                update(memories)
                .where(memories.c.id == memory_id, memories.c.version == req.expected_version)
                .values(**values)
                .returning(memories)
            )
            .mappings()
            .first()
        )
    except IntegrityError as exc:
        if is_fact_key_race(exc):
            raise AppError(
                ErrorCode.fact_key_race,
                "An active fact changed; read again and retry.",
                retry_after=1,
            ) from None
        raise
    if changed is None:
        raise AppError(
            ErrorCode.version_conflict,
            "Memory version changed.",
            details={"current_version": row["version"]},
        )
    append_version(
        conn,
        memory_id=memory_id,
        version=changed["version"],
        content=changed["content"],
        state=new_state,
        labels=changed["labels"],
        fact_key=changed["fact_key"],
        actor_id=principal.actor_id,
        reason="promoted" if promotion else "updated",
    )
    return WriteOutcome(
        200,
        write_body(changed, superseded_id=superseded_id),
        "memory.update",
        row["scope_id"],
        (memory_id,) + (() if superseded_id is None else (superseded_id,)),
    )
