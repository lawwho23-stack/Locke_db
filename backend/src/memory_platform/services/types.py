"""Small shared types for services (contract section 8)."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class WriteOutcome:
    """What a write action returns to the idempotency wrapper (`services/idempotency.py`).

    - `body` is stored for replays, so it must be JSON-safe (UUIDs as strings) and
      content-free: ids, version, state and flags only. Never memory text.
    - `scope_id` is the scope whose `revision` should go up. Use None when nothing
      searchable changed (for example an exact duplicate that only added evidence).
    """

    status_code: int
    body: dict[str, Any]
    action: str  # for example "memory.remember"
    scope_id: UUID | None
    target_ids: tuple[UUID, ...]
    replayed: bool = False
