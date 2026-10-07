"""Lock, replace and link the current fact (contract section 8)."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Connection, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from memory_platform.enums import MemoryState, MemoryType, RelationOrigin, RelationType, Trust
from memory_platform.services.versions import append_version
from memory_platform.tables import memories, memory_relations


@dataclass(frozen=True)
class ActiveFact:
    id: UUID
    version: int
    trust: Trust


def find_active_fact_for_update(
    conn: Connection, *, scope_id: UUID, mtype: MemoryType, fact_key: str
) -> ActiveFact | None:
    row = (
        conn.execute(
            select(memories)
            .where(
                memories.c.scope_id == scope_id,
                memories.c.type == mtype.value,
                memories.c.fact_key == fact_key,
                memories.c.state == MemoryState.active.value,
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    return None if row is None else ActiveFact(row["id"], row["version"], Trust(row["trust"]))


def mark_superseded(conn: Connection, *, memory_id: UUID, actor_id: UUID) -> int:
    row = (
        conn.execute(
            update(memories)
            .where(memories.c.id == memory_id)
            .values(
                state=MemoryState.superseded.value,
                version=memories.c.version + 1,
                updated_at=func.now(),
            )
            .returning(memories)
        )
        .mappings()
        .one()
    )
    append_version(
        conn,
        memory_id=memory_id,
        version=row["version"],
        content=row["content"],
        state=MemoryState.superseded,
        labels=row["labels"],
        fact_key=row["fact_key"],
        actor_id=actor_id,
        reason="superseded",
    )
    return int(row["version"])


def link(
    conn: Connection,
    *,
    workspace_id: UUID,
    from_id: UUID,
    to_id: UUID,
    rtype: RelationType,
    origin: RelationOrigin,
    actor_id: UUID,
) -> UUID:
    relation_id = conn.execute(
        insert(memory_relations)
        .values(
            id=uuid4(),
            workspace_id=workspace_id,
            from_id=from_id,
            to_id=to_id,
            type=rtype.value,
            origin=origin.value,
            created_by=actor_id,
        )
        .on_conflict_do_nothing(constraint="uq_memory_relations_tuple")
        .returning(memory_relations.c.id)
    ).scalar_one_or_none()
    if relation_id is None:
        relation_id = conn.execute(
            select(memory_relations.c.id).where(
                memory_relations.c.from_id == from_id,
                memory_relations.c.to_id == to_id,
                memory_relations.c.type == rtype.value,
            )
        ).scalar_one()
    return UUID(str(relation_id))


def is_fact_key_race(exc: IntegrityError) -> bool:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == "uq_memories_active_fact_key"
