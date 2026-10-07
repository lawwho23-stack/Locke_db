"""Core schema for Phase 1: workspaces, auth, memories, versions, evidence, relations, audit.

This file is a frozen snapshot of the schema. It does NOT import tables.py or enums.py,
so later changes to those files can never rewrite history. It must match tables.py exactly
(`alembic check` verifies this). Every constraint name is written in full inside `op.f()`,
which tells Alembic "use this name as it is" (no naming-convention rewriting).

Revision ID: 0001
Revises:
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Short helpers so the table definitions below stay readable.
# Each call returns a NEW Column (one Column object cannot belong to two tables).
def _uuid_pk() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    # Checks that pgvector is available on this server. Phase 1 has no vector columns yet.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "workspaces",
        _uuid_pk(),
        sa.Column("name", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )

    op.create_table(
        "scopes",
        _uuid_pk(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("grant_revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scopes")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_scopes_workspace_id_workspaces")
        ),
        sa.CheckConstraint("kind IN ('personal', 'project')", name=op.f("ck_scopes_kind_valid")),
        sa.CheckConstraint(
            "name ~ '^[a-z0-9][a-z0-9-]{0,62}$'", name=op.f("ck_scopes_name_format")
        ),
        sa.CheckConstraint("revision >= 0", name=op.f("ck_scopes_revision_non_negative")),
        sa.UniqueConstraint("id", "workspace_id", name=op.f("uq_scopes_id_workspace")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_scopes_workspace_name")),
    )

    op.create_table(
        "actors",
        _uuid_pk(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_actors")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_actors_workspace_id_workspaces")
        ),
        sa.CheckConstraint("kind IN ('owner', 'agent')", name=op.f("ck_actors_kind_valid")),
        sa.UniqueConstraint("id", "workspace_id", name=op.f("uq_actors_id_workspace")),
    )
    # At most one owner actor per workspace.
    op.create_index(
        op.f("uq_actors_one_owner"),
        "actors",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'owner'"),
    )

    op.create_table(
        "credentials",
        _uuid_pk(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("token_prefix", sa.Text(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credentials")),
        sa.ForeignKeyConstraint(
            ["actor_id", "workspace_id"],
            ["actors.id", "actors.workspace_id"],
            name=op.f("fk_credentials_actor_id_actors"),
        ),
        sa.UniqueConstraint("token_hash", name=op.f("uq_credentials_token_hash")),
        sa.UniqueConstraint("id", "workspace_id", name=op.f("uq_credentials_id_workspace")),
    )

    op.create_table(
        "credential_grants",
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("capabilities", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("credential_id", "scope_id", name=op.f("pk_credential_grants")),
        sa.ForeignKeyConstraint(
            ["credential_id", "workspace_id"],
            ["credentials.id", "credentials.workspace_id"],
            name=op.f("fk_credential_grants_credential_id_credentials"),
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "workspace_id"],
            ["scopes.id", "scopes.workspace_id"],
            name=op.f("fk_credential_grants_scope_id_scopes"),
        ),
        sa.CheckConstraint(
            "capabilities <@ ARRAY['memory:read', 'memory:write', 'memory:delete', "
            "'source:ingest']::text[] AND cardinality(capabilities) > 0",
            name=op.f("ck_credential_grants_capabilities_valid"),
        ),
    )

    op.create_table(
        "memories",
        _uuid_pk(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        # content and content_hash are NULL only for deleted memories (tombstones).
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("fact_key", sa.Text(), nullable=True),
        sa.Column(
            "labels",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("trust", sa.Text(), nullable=False),
        sa.Column("importance", sa.REAL(), server_default=sa.text("0.5"), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memories")),
        sa.ForeignKeyConstraint(
            ["scope_id", "workspace_id"],
            ["scopes.id", "scopes.workspace_id"],
            name=op.f("fk_memories_scope_id_scopes"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "workspace_id"],
            ["actors.id", "actors.workspace_id"],
            name=op.f("fk_memories_created_by_actors"),
        ),
        sa.CheckConstraint(
            "type IN ('fact', 'preference', 'decision', 'experience', 'procedure')",
            name=op.f("ck_memories_type_valid"),
        ),
        sa.CheckConstraint(
            "fact_key ~ '^[a-z0-9][a-z0-9_.:-]{0,199}$'", name=op.f("ck_memories_fact_key_format")
        ),
        sa.CheckConstraint("cardinality(labels) <= 20", name=op.f("ck_memories_labels_max_20")),
        sa.CheckConstraint(
            "trust IN ('owner_asserted', 'source_extracted', 'agent_reported', 'inferred')",
            name=op.f("ck_memories_trust_valid"),
        ),
        sa.CheckConstraint(
            "importance >= 0 AND importance <= 1", name=op.f("ck_memories_importance_range")
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'active', 'superseded', 'expired', 'deleted')",
            name=op.f("ck_memories_state_valid"),
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
            name=op.f("ck_memories_valid_range"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_memories_version_positive")),
        sa.CheckConstraint(
            "(state = 'deleted') OR (content IS NOT NULL AND content_hash IS NOT NULL)",
            name=op.f("ck_memories_content_present_unless_deleted"),
        ),
        sa.UniqueConstraint("id", "workspace_id", name=op.f("uq_memories_id_workspace")),
    )
    op.create_index(
        op.f("ix_memories_scope_state_updated"),
        "memories",
        ["scope_id", "state", "updated_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_memories_labels"), "memories", ["labels"], unique=False, postgresql_using="gin"
    )
    op.create_index(
        op.f("ix_memories_scope_type_content_hash"),
        "memories",
        ["scope_id", "type", "content_hash"],
        unique=False,
    )
    # Only ONE active memory per (scope, type, fact_key). Code catches this index name.
    op.create_index(
        op.f("uq_memories_active_fact_key"),
        "memories",
        ["scope_id", "type", "fact_key"],
        unique=True,
        postgresql_where=sa.text("state = 'active' AND fact_key IS NOT NULL"),
    )

    op.create_table(
        "memory_versions",
        _uuid_pk(),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("labels", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("fact_key", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_versions")),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["memories.id"],
            name=op.f("fk_memory_versions_memory_id_memories"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("memory_id", "version", name=op.f("uq_memory_versions_memory_version")),
    )

    op.create_table(
        "memory_evidence",
        _uuid_pk(),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_evidence")),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["memories.id"],
            name=op.f("fk_memory_evidence_memory_id_memories"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("kind IN ('assertion')", name=op.f("ck_memory_evidence_kind_valid")),
        sa.CheckConstraint(
            "role IN ('supports', 'contradicts')", name=op.f("ck_memory_evidence_role_valid")
        ),
    )

    op.create_table(
        "memory_relations",
        _uuid_pk(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("from_id", sa.Uuid(), nullable=False),
        sa.Column("to_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_relations")),
        sa.ForeignKeyConstraint(
            ["from_id", "workspace_id"],
            ["memories.id", "memories.workspace_id"],
            name=op.f("fk_memory_relations_from_id_memories"),
        ),
        sa.ForeignKeyConstraint(
            ["to_id", "workspace_id"],
            ["memories.id", "memories.workspace_id"],
            name=op.f("fk_memory_relations_to_id_memories"),
        ),
        sa.CheckConstraint(
            "type IN ('supersedes', 'related_to', 'conflicts_with')",
            name=op.f("ck_memory_relations_type_valid"),
        ),
        sa.CheckConstraint(
            "origin IN ('asserted', 'system', 'suggested')",
            name=op.f("ck_memory_relations_origin_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'removed')", name=op.f("ck_memory_relations_status_valid")
        ),
        sa.CheckConstraint("from_id <> to_id", name=op.f("ck_memory_relations_no_self_link")),
        sa.UniqueConstraint("from_id", "to_id", "type", name=op.f("uq_memory_relations_tuple")),
    )

    op.create_table(
        "forget_suppressions",
        _uuid_pk(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("content_hmac", sa.Text(), nullable=True),
        sa.Column("fact_key_hmac", sa.Text(), nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forget_suppressions")),
        sa.ForeignKeyConstraint(
            ["scope_id", "workspace_id"],
            ["scopes.id", "scopes.workspace_id"],
            name=op.f("fk_forget_suppressions_scope_id_scopes"),
        ),
        sa.CheckConstraint(
            "content_hmac IS NOT NULL OR fact_key_hmac IS NOT NULL",
            name=op.f("ck_forget_suppressions_at_least_one_hmac"),
        ),
    )
    op.create_index(
        op.f("ix_forget_suppressions_scope_content_hmac"),
        "forget_suppressions",
        ["scope_id", "content_hmac"],
        unique=False,
    )
    op.create_index(
        op.f("ix_forget_suppressions_scope_fact_key_hmac"),
        "forget_suppressions",
        ["scope_id", "fact_key_hmac"],
        unique=False,
    )

    op.create_table(
        "operations",
        _uuid_pk(),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        # Content-free: ids, version, state and flags only. Never memory text.
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operations")),
        sa.UniqueConstraint(
            "actor_id", "operation", "idempotency_key", name=op.f("uq_operations_actor_op_key")
        ),
    )

    op.create_table(
        "activity",
        _uuid_pk(),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "target_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("scope_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity")),
    )


def downgrade() -> None:
    # Drop children before parents. Dropping a table also drops its indexes.
    # The `vector` extension is NOT dropped: other objects in the database may use it.
    op.drop_table("activity")
    op.drop_table("operations")
    op.drop_table("forget_suppressions")
    op.drop_table("memory_relations")
    op.drop_table("memory_evidence")
    op.drop_table("memory_versions")
    op.drop_table("memories")
    op.drop_table("credential_grants")
    op.drop_table("credentials")
    op.drop_table("actors")
    op.drop_table("scopes")
    op.drop_table("workspaces")
