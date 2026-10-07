"""Database tables as SQLAlchemy Core objects (contract section 5).

This file and `migrations/versions/0001_core.py` must always describe the same schema.
`alembic check` (and tests/test_schema.py) fail if they drift apart.

Rules of thumb used here:
- Every id is a UUID. The database can generate one (`gen_random_uuid()`),
  but services may also pass their own `uuid.uuid4()`.
- Every time is `timestamptz` (UTC).
- "Composite foreign keys" such as (scope_id, workspace_id) -> scopes(id, workspace_id)
  make the DATABASE refuse rows that point into another workspace.
- Names marked PINNED are caught by code (for example `uq_memories_active_fact_key`),
  so never rename them.
"""

from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from memory_platform.enums import (
    ActorKind,
    Capability,
    EvidenceKind,
    EvidenceRole,
    MemoryState,
    MemoryType,
    RelationOrigin,
    RelationStatus,
    RelationType,
    ScopeKind,
    Trust,
)

# Naming convention: SQLAlchemy builds constraint and index names from these templates.
# Note the "ck" template uses the constraint's own name, so every CheckConstraint below
# gets a SHORT name (for example "no_self_link"), and the final database name becomes
# "ck_<table>_<short name>". Never pass the full name, or the prefix would be doubled.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

# Same regex as the API schemas (api/schemas.py). Kept as plain text in the database too.
SCOPE_NAME_REGEX = "^[a-z0-9][a-z0-9-]{0,62}$"
FACT_KEY_REGEX = "^[a-z0-9][a-z0-9_.:-]{0,199}$"


def _in_values(column: str, enum_cls: type[StrEnum]) -> str:
    """Build `column IN ('a', 'b', ...)` from an enum, so checks match the enums exactly."""
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({values})"


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _uuid(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.Uuid(), nullable=nullable)


def _text(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.Text(), nullable=nullable)


def _timestamp(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if default_now else None,
    )


workspaces = sa.Table(
    "workspaces",
    metadata,
    _id(),
    _text("name"),
    _timestamp("created_at", default_now=True),
)

scopes = sa.Table(
    "scopes",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _text("kind"),
    _text("name"),
    # `revision` goes up on every change that affects search results (used by caches later).
    sa.Column("revision", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    # `grant_revision` goes up when who-can-see-this changes.
    sa.Column("grant_revision", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(
        ["workspace_id"], ["workspaces.id"], name="fk_scopes_workspace_id_workspaces"
    ),
    sa.CheckConstraint(_in_values("kind", ScopeKind), name="kind_valid"),
    sa.CheckConstraint(f"name ~ '{SCOPE_NAME_REGEX}'", name="name_format"),
    sa.CheckConstraint("revision >= 0", name="revision_non_negative"),
    # PINNED: the target of composite foreign keys from other tables.
    sa.UniqueConstraint("id", "workspace_id", name="uq_scopes_id_workspace"),
    sa.UniqueConstraint("workspace_id", "name", name="uq_scopes_workspace_name"),
)

actors = sa.Table(
    "actors",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _text("kind"),
    _text("display_name"),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(
        ["workspace_id"], ["workspaces.id"], name="fk_actors_workspace_id_workspaces"
    ),
    sa.CheckConstraint(_in_values("kind", ActorKind), name="kind_valid"),
    # PINNED.
    sa.UniqueConstraint("id", "workspace_id", name="uq_actors_id_workspace"),
)
# PINNED: at most one owner actor per workspace (a partial unique index).
sa.Index(
    "uq_actors_one_owner",
    actors.c.workspace_id,
    unique=True,
    postgresql_where=sa.text("kind = 'owner'"),
)

credentials = sa.Table(
    "credentials",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _uuid("actor_id"),
    # Only the SHA-256 hash of the token is stored. The plain token is shown once.
    _text("token_hash"),
    _text("token_prefix"),
    sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    _timestamp("created_at", default_now=True),
    _timestamp("expires_at", nullable=True),
    _timestamp("revoked_at", nullable=True),
    sa.ForeignKeyConstraint(
        ["actor_id", "workspace_id"],
        ["actors.id", "actors.workspace_id"],
        name="fk_credentials_actor_id_actors",
    ),
    sa.UniqueConstraint("token_hash", name="uq_credentials_token_hash"),
    # PINNED.
    sa.UniqueConstraint("id", "workspace_id", name="uq_credentials_id_workspace"),
)

credential_grants = sa.Table(
    "credential_grants",
    metadata,
    _uuid("credential_id"),
    _uuid("scope_id"),
    _uuid("workspace_id"),
    sa.Column("capabilities", postgresql.ARRAY(sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint("credential_id", "scope_id", name="pk_credential_grants"),
    sa.ForeignKeyConstraint(
        ["credential_id", "workspace_id"],
        ["credentials.id", "credentials.workspace_id"],
        name="fk_credential_grants_credential_id_credentials",
    ),
    sa.ForeignKeyConstraint(
        ["scope_id", "workspace_id"],
        ["scopes.id", "scopes.workspace_id"],
        name="fk_credential_grants_scope_id_scopes",
    ),
    sa.CheckConstraint(
        "capabilities <@ ARRAY["
        + ", ".join(f"'{member.value}'" for member in Capability)
        + "]::text[] AND cardinality(capabilities) > 0",
        name="capabilities_valid",
    ),
)

memories = sa.Table(
    "memories",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _uuid("scope_id"),
    _text("type"),
    # Null only when the memory is deleted (a "tombstone": the row stays, the text is erased).
    _text("content", nullable=True),
    _text("content_hash", nullable=True),
    _text("fact_key", nullable=True),
    sa.Column(
        "labels",
        postgresql.ARRAY(sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::text[]"),
    ),
    _text("trust"),
    sa.Column("importance", sa.REAL(), nullable=False, server_default=sa.text("0.5")),
    _text("state"),
    _timestamp("valid_from", nullable=True),
    _timestamp("valid_until", nullable=True),
    # Goes up by 1 on every change. Clients send it back as `expected_version`.
    sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    _uuid("created_by"),
    _timestamp("created_at", default_now=True),
    _timestamp("updated_at", default_now=True),
    sa.ForeignKeyConstraint(
        ["scope_id", "workspace_id"],
        ["scopes.id", "scopes.workspace_id"],
        name="fk_memories_scope_id_scopes",
    ),
    sa.ForeignKeyConstraint(
        ["created_by", "workspace_id"],
        ["actors.id", "actors.workspace_id"],
        name="fk_memories_created_by_actors",
    ),
    sa.CheckConstraint(_in_values("type", MemoryType), name="type_valid"),
    sa.CheckConstraint(f"fact_key ~ '{FACT_KEY_REGEX}'", name="fact_key_format"),
    sa.CheckConstraint("cardinality(labels) <= 20", name="labels_max_20"),
    sa.CheckConstraint(_in_values("trust", Trust), name="trust_valid"),
    sa.CheckConstraint("importance >= 0 AND importance <= 1", name="importance_range"),
    sa.CheckConstraint(_in_values("state", MemoryState), name="state_valid"),
    sa.CheckConstraint(
        "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
        name="valid_range",
    ),
    sa.CheckConstraint("version >= 1", name="version_positive"),
    # Only deleted memories may have no text.
    sa.CheckConstraint(
        "(state = 'deleted') OR (content IS NOT NULL AND content_hash IS NOT NULL)",
        name="content_present_unless_deleted",
    ),
    # PINNED.
    sa.UniqueConstraint("id", "workspace_id", name="uq_memories_id_workspace"),
)
sa.Index(
    "ix_memories_scope_state_updated", memories.c.scope_id, memories.c.state, memories.c.updated_at
)
sa.Index("ix_memories_labels", memories.c.labels, postgresql_using="gin")
sa.Index(
    "ix_memories_scope_type_content_hash",
    memories.c.scope_id,
    memories.c.type,
    memories.c.content_hash,
)
# PINNED: only ONE active memory per (scope, type, fact_key). A second writer gets an
# IntegrityError with this name, which the services turn into `fact_key_race`.
sa.Index(
    "uq_memories_active_fact_key",
    memories.c.scope_id,
    memories.c.type,
    memories.c.fact_key,
    unique=True,
    postgresql_where=sa.text("state = 'active' AND fact_key IS NOT NULL"),
)

memory_versions = sa.Table(
    "memory_versions",
    metadata,
    _id(),
    _uuid("memory_id"),
    sa.Column("version", sa.Integer(), nullable=False),
    _text("content", nullable=True),
    _text("state"),
    sa.Column("labels", postgresql.ARRAY(sa.Text()), nullable=False),
    _text("fact_key", nullable=True),
    _uuid("actor_id"),
    # Why this version was written: created, updated, promoted, superseded, forgotten.
    _text("reason"),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(
        ["memory_id"],
        ["memories.id"],
        name="fk_memory_versions_memory_id_memories",
        ondelete="CASCADE",
    ),
    # PINNED.
    sa.UniqueConstraint("memory_id", "version", name="uq_memory_versions_memory_version"),
)

memory_evidence = sa.Table(
    "memory_evidence",
    metadata,
    _id(),
    _uuid("memory_id"),
    _text("kind"),
    _text("role"),
    _uuid("actor_id"),
    # Evidence stores WHO asserted it and in which request, never a copy of the text.
    _text("request_id"),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(
        ["memory_id"],
        ["memories.id"],
        name="fk_memory_evidence_memory_id_memories",
        ondelete="CASCADE",
    ),
    sa.CheckConstraint(_in_values("kind", EvidenceKind), name="kind_valid"),
    sa.CheckConstraint(_in_values("role", EvidenceRole), name="role_valid"),
)

memory_relations = sa.Table(
    "memory_relations",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _uuid("from_id"),
    _uuid("to_id"),
    _text("type"),
    _text("origin"),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
    _uuid("created_by"),
    _timestamp("created_at", default_now=True),
    # Both ends must live in the same workspace as the relation row.
    sa.ForeignKeyConstraint(
        ["from_id", "workspace_id"],
        ["memories.id", "memories.workspace_id"],
        name="fk_memory_relations_from_id_memories",
    ),
    sa.ForeignKeyConstraint(
        ["to_id", "workspace_id"],
        ["memories.id", "memories.workspace_id"],
        name="fk_memory_relations_to_id_memories",
    ),
    sa.CheckConstraint(_in_values("type", RelationType), name="type_valid"),
    sa.CheckConstraint(_in_values("origin", RelationOrigin), name="origin_valid"),
    sa.CheckConstraint(_in_values("status", RelationStatus), name="status_valid"),
    # PINNED (ck_memory_relations_no_self_link): a memory cannot relate to itself.
    sa.CheckConstraint("from_id <> to_id", name="no_self_link"),
    # PINNED. Direction: supersedes = new -> old; conflicts_with = new draft -> existing active.
    sa.UniqueConstraint("from_id", "to_id", "type", name="uq_memory_relations_tuple"),
)

# Only keyed hashes (HMAC) are stored here, never the forgotten text itself.
forget_suppressions = sa.Table(
    "forget_suppressions",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _uuid("scope_id"),
    _text("content_hmac", nullable=True),
    _text("fact_key_hmac", nullable=True),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(
        ["scope_id", "workspace_id"],
        ["scopes.id", "scopes.workspace_id"],
        name="fk_forget_suppressions_scope_id_scopes",
    ),
    sa.CheckConstraint(
        "content_hmac IS NOT NULL OR fact_key_hmac IS NOT NULL",
        name="at_least_one_hmac",
    ),
)
sa.Index(
    "ix_forget_suppressions_scope_content_hmac",
    forget_suppressions.c.scope_id,
    forget_suppressions.c.content_hmac,
)
sa.Index(
    "ix_forget_suppressions_scope_fact_key_hmac",
    forget_suppressions.c.scope_id,
    forget_suppressions.c.fact_key_hmac,
)

# Idempotency records. `response_body` must be content-free: ids, version, state, flags only.
operations = sa.Table(
    "operations",
    metadata,
    _id(),
    _uuid("actor_id"),
    _uuid("workspace_id"),
    _text("operation"),
    _text("idempotency_key"),
    _text("request_hash"),
    sa.Column("response_status", sa.Integer(), nullable=True),
    sa.Column("response_body", postgresql.JSONB(), nullable=True),
    _timestamp("created_at", default_now=True),
    _timestamp("expires_at"),
    # PINNED: the same actor cannot reuse a key for the same operation.
    sa.UniqueConstraint(
        "actor_id", "operation", "idempotency_key", name="uq_operations_actor_op_key"
    ),
)

# Audit log. It must never hold memory text.
activity = sa.Table(
    "activity",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _uuid("actor_id", nullable=True),
    _text("action"),
    sa.Column(
        "target_ids",
        postgresql.ARRAY(sa.Uuid()),
        nullable=False,
        server_default=sa.text("'{}'::uuid[]"),
    ),
    _uuid("scope_id", nullable=True),
    _text("request_id"),
    # "ok" or an ErrorCode value.
    _text("result"),
    sa.Column("latency_ms", sa.Integer(), nullable=True),
    _timestamp("created_at", default_now=True),
)
