"""Version history for memories (contract section 8)."""

from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import Connection, insert

from memory_platform.enums import MemoryState
from memory_platform.tables import memory_versions


def append_version(
    conn: Connection,
    *,
    memory_id: UUID,
    version: int,
    content: str | None,
    state: MemoryState,
    labels: Sequence[str],
    fact_key: str | None,
    actor_id: UUID,
    reason: str,
) -> None:
    """Save a snapshot of a memory at `version`.

    `reason` says why it was written: created, updated, promoted, superseded, forgotten.
    The pair (memory_id, version) is unique, so writing the same version twice fails.
    The caller must pass `version` equal to the memory's NEW version number.
    """
    conn.execute(
        insert(memory_versions).values(
            id=uuid4(),
            memory_id=memory_id,
            version=version,
            content=content,
            state=state.value,
            labels=list(labels),
            fact_key=fact_key,
            actor_id=actor_id,
            reason=reason,
        )
    )
