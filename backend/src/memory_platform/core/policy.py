"""Pure domain rules for trust, initial state, promotion and expiry."""

from datetime import datetime

from memory_platform.enums import ActorKind, MemoryState, MemoryType, Trust


def trust_for_actor(kind: ActorKind) -> Trust:
    """Trust comes from the authenticated actor, never the request body."""
    return Trust.owner_asserted if kind == ActorKind.owner else Trust.agent_reported


def initial_state(trust: Trust, mtype: MemoryType) -> MemoryState:
    """Owner assertions and agent facts/decisions start active; other inputs start draft."""
    if trust == Trust.owner_asserted or (
        trust == Trust.agent_reported and mtype in {MemoryType.fact, MemoryType.decision}
    ):
        return MemoryState.active
    return MemoryState.draft


def can_transition(old: MemoryState, new: MemoryState, actor: ActorKind) -> bool:
    """PATCH only allows the owner to promote draft to active."""
    return old == MemoryState.draft and new == MemoryState.active and actor == ActorKind.owner


def effective_state(state: MemoryState, valid_until: datetime | None, now: datetime) -> MemoryState:
    """Show expired active rows without changing their stored state."""
    if state == MemoryState.active and valid_until is not None and valid_until <= now:
        return MemoryState.expired
    return state
