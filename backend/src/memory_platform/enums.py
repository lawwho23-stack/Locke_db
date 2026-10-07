"""Shared enums (contract section 1).

They are `StrEnum`, so each member IS a string: `MemoryState.active == "active"`.
The database check constraints use exactly these string values.
When you write to the database, pass `member.value` to keep the parameter a plain `str`.
"""

from enum import StrEnum


class MemoryType(StrEnum):
    fact = "fact"
    preference = "preference"
    decision = "decision"
    experience = "experience"
    procedure = "procedure"


class MemoryState(StrEnum):
    draft = "draft"
    active = "active"
    superseded = "superseded"
    expired = "expired"
    deleted = "deleted"


class Trust(StrEnum):
    owner_asserted = "owner_asserted"
    source_extracted = "source_extracted"
    agent_reported = "agent_reported"
    inferred = "inferred"


class ActorKind(StrEnum):
    owner = "owner"
    agent = "agent"


class ScopeKind(StrEnum):
    personal = "personal"
    project = "project"


class Capability(StrEnum):
    """What a credential may do on one scope.

    Python names use `_` (a name cannot contain ":"); the VALUES are what the
    database and the API use: Capability.memory_write.value == "memory:write".
    """

    memory_read = "memory:read"
    memory_write = "memory:write"
    memory_delete = "memory:delete"
    source_ingest = "source:ingest"
    task_read = "task:read"
    task_write = "task:write"
    skill_read = "skill:read"
    skill_write = "skill:write"


class RelationType(StrEnum):
    supersedes = "supersedes"
    related_to = "related_to"
    conflicts_with = "conflicts_with"


class RelationOrigin(StrEnum):
    asserted = "asserted"
    system = "system"
    suggested = "suggested"


class RelationStatus(StrEnum):
    active = "active"
    removed = "removed"


class EvidenceKind(StrEnum):
    assertion = "assertion"
    source = "source"


class EvidenceRole(StrEnum):
    supports = "supports"
    contradicts = "contradicts"
