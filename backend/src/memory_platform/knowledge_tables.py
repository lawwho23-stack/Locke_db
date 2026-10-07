"""Versioned document knowledge, fenced worker jobs, vectors and provider accounting."""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.selectable import ScalarSelect

from memory_platform.tables import _id, _text, _timestamp, _uuid, metadata

sources = sa.Table(
    "sources",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _uuid("scope_id"),
    _text("title"),
    _uuid("created_by"),
    sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
    _timestamp("deleted_at", nullable=True),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(["scope_id", "workspace_id"], ["scopes.id", "scopes.workspace_id"]),
    sa.ForeignKeyConstraint(["created_by", "workspace_id"], ["actors.id", "actors.workspace_id"]),
    sa.UniqueConstraint("id", "workspace_id", name="uq_sources_id_workspace"),
    sa.CheckConstraint("current_version >= 1", name="version_positive"),
)
source_versions = sa.Table(
    "source_versions",
    metadata,
    _id(),
    _uuid("source_id"),
    _uuid("workspace_id"),
    sa.Column("version", sa.Integer(), nullable=False),
    _text("filename"),
    _text("format"),
    _text("content_hash"),
    _text("storage_key"),
    sa.Column("byte_size", sa.Integer(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
    _text("summary", nullable=True),
    _text("error_code", nullable=True),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(["source_id", "workspace_id"], ["sources.id", "sources.workspace_id"]),
    sa.UniqueConstraint("source_id", "version", name="uq_source_versions_source_version"),
    sa.UniqueConstraint("id", "workspace_id", name="uq_source_versions_id_workspace"),
    sa.CheckConstraint(
        "version >= 1 AND byte_size >= 0 AND byte_size <= 20971520", name="size_version"
    ),
    sa.CheckConstraint("format IN ('pdf','md','txt','docx')", name="format_valid"),
    sa.CheckConstraint(
        "status IN ('queued','processing','ready','failed','cancelled')", name="status_valid"
    ),
)
source_chunks = sa.Table(
    "source_chunks",
    metadata,
    _id(),
    _uuid("source_version_id"),
    _uuid("workspace_id"),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    _text("content"),
    _text("content_hash"),
    sa.Column("page", sa.Integer(), nullable=True),
    sa.Column("embedding", Vector(), nullable=True),
    _text("embedding_model", nullable=True),
    sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(
        ["source_version_id", "workspace_id"],
        ["source_versions.id", "source_versions.workspace_id"],
    ),
    sa.UniqueConstraint("source_version_id", "ordinal", name="uq_source_chunks_version_ordinal"),
)
memory_embeddings = sa.Table(
    "memory_embeddings",
    metadata,
    _id(),
    _uuid("memory_id"),
    _uuid("workspace_id"),
    sa.Column("memory_version", sa.Integer(), nullable=False),
    _text("content_hash"),
    _text("model"),
    sa.Column("dimensions", sa.Integer(), nullable=False),
    sa.Column("embedding", Vector(), nullable=False),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(
        ["memory_id", "workspace_id"], ["memories.id", "memories.workspace_id"], ondelete="CASCADE"
    ),
    sa.UniqueConstraint(
        "memory_id",
        "memory_version",
        "model",
        "dimensions",
        name="uq_memory_embeddings_version_model",
    ),
)
knowledge_jobs = sa.Table(
    "knowledge_jobs",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _text("dedup_key"),
    _text("kind"),
    _uuid("target_id"),
    sa.Column("target_version", sa.Integer(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    _uuid("lease_token", nullable=True),
    _timestamp("lease_until", nullable=True),
    _timestamp("available_at", default_now=True),
    _text("error_code", nullable=True),
    _timestamp("created_at", default_now=True),
    _timestamp("finished_at", nullable=True),
    sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
    sa.UniqueConstraint("workspace_id", "dedup_key", name="uq_knowledge_jobs_workspace_dedup"),
    sa.CheckConstraint(
        "kind IN ('source','source_enrichment','source_cleanup','memory_embedding')",
        name="kind_valid",
    ),
    sa.CheckConstraint(
        "status IN ('queued','leased','completed','failed','cancelled')", name="status_valid"
    ),
    sa.CheckConstraint("attempts >= 0 AND attempts <= 5", name="attempts_range"),
)
sa.Index(
    "ix_knowledge_jobs_status_available", knowledge_jobs.c.status, knowledge_jobs.c.available_at
)
provider_usage = sa.Table(
    "provider_usage",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _text("operation"),
    _text("model"),
    sa.Column("day", sa.Date(), nullable=False),
    sa.Column("reserved_usd", sa.Numeric(16, 8), nullable=False),
    sa.Column("charged_usd", sa.Numeric(16, 8), nullable=True),
    sa.Column("tokens", sa.Integer(), nullable=True),
    sa.Column("metadata", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    _timestamp("created_at", default_now=True),
    _timestamp("completed_at", nullable=True),
    sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
    sa.CheckConstraint(
        "reserved_usd >= 0 AND (charged_usd IS NULL OR charged_usd >= 0)", name="cost_nonnegative"
    ),
)
sa.Index("ix_provider_usage_workspace_day", provider_usage.c.workspace_id, provider_usage.c.day)


def published_source_version() -> ScalarSelect[int]:
    """Latest ready evidence, retaining the previous publication during replacement."""
    published = source_versions.alias("published_source_versions")
    return (
        sa.select(sa.func.max(published.c.version))
        .where(
            published.c.source_id == sources.c.id,
            published.c.workspace_id == sources.c.workspace_id,
            published.c.version <= sources.c.current_version,
            published.c.status == "ready",
        )
        .correlate(sources)
        .scalar_subquery()
    )
