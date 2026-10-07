"""Erase all historical text while retaining only keyed suppression hashes."""

from uuid import UUID, uuid4

from sqlalchemy import Connection, delete, func, insert, select, update

from memory_platform.api.schemas import ForgetResponse
from memory_platform.auth.principal import Principal
from memory_platform.core.normalize import normalize_fact_key, normalize_text, suppression_hmac
from memory_platform.enums import Capability, MemoryState
from memory_platform.errors import AppError, ErrorCode
from memory_platform.knowledge_tables import memory_embeddings
from memory_platform.services.access import fetch_memory_checked
from memory_platform.services.locking import lock_scope
from memory_platform.services.types import WriteOutcome
from memory_platform.services.versions import append_version
from memory_platform.tables import forget_suppressions, memories, memory_versions


def forget_memory(
    conn: Connection,
    principal: Principal,
    memory_id: UUID,
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
    principal.require(scope_id, Capability.memory_delete)
    lock_scope(conn, scope_id)
    row = fetch_memory_checked(
        conn, principal, memory_id, Capability.memory_delete, for_update=True
    )
    history = (
        conn.execute(select(memory_versions).where(memory_versions.c.memory_id == memory_id))
        .mappings()
        .all()
    )
    fingerprints: set[tuple[str | None, str | None]] = set()
    for version in [row, *history]:
        ch = (
            None
            if version["content"] is None
            else suppression_hmac(normalize_text(version["content"]), hmac_key)
        )
        fh = (
            None
            if version["fact_key"] is None
            else suppression_hmac(normalize_fact_key(version["fact_key"]), hmac_key)
        )
        if ch is not None or fh is not None:
            fingerprints.add((ch, fh))
    for ch, fh in fingerprints:
        conn.execute(
            insert(forget_suppressions).values(
                id=uuid4(),
                workspace_id=principal.workspace_id,
                scope_id=row["scope_id"],
                content_hmac=ch,
                fact_key_hmac=fh,
            )
        )
    new_version = int(row["version"]) + 1
    conn.execute(
        update(memories)
        .where(memories.c.id == memory_id)
        .values(
            content=None,
            content_hash=None,
            labels=[],
            fact_key=None,
            state=MemoryState.deleted.value,
            version=new_version,
            updated_at=func.now(),
        )
    )
    conn.execute(
        update(memory_versions)
        .where(memory_versions.c.memory_id == memory_id)
        .values(content=None, labels=[], fact_key=None)
    )
    conn.execute(delete(memory_embeddings).where(memory_embeddings.c.memory_id == memory_id))
    append_version(
        conn,
        memory_id=memory_id,
        version=new_version,
        content=None,
        state=MemoryState.deleted,
        labels=[],
        fact_key=None,
        actor_id=principal.actor_id,
        reason="forgotten",
    )
    body = ForgetResponse(id=memory_id, version=new_version).model_dump(mode="json")
    return WriteOutcome(200, body, "memory.forget", row["scope_id"], (memory_id,))
