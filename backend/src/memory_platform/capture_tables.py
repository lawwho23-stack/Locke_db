"""Explicit selected events and source-grounded memory provenance."""

import sqlalchemy as sa

from memory_platform.knowledge_tables import source_chunks, source_versions, sources
from memory_platform.tables import _id, _text, _timestamp, _uuid, memories, metadata

# Composite keys bind every evidence locator to one workspace and scope.
for table, columns, name in (
    (memories, ["id", "scope_id", "workspace_id"], "uq_memories_id_scope_workspace"),
    (sources, ["id", "scope_id", "workspace_id"], "uq_sources_id_scope_workspace"),
    (
        source_versions,
        ["id", "source_id", "workspace_id"],
        "uq_source_versions_id_source_workspace",
    ),
    (
        source_chunks,
        ["id", "source_version_id", "workspace_id"],
        "uq_source_chunks_id_version_workspace",
    ),
):
    table.append_constraint(sa.UniqueConstraint(*columns, name=name))

capture_events = sa.Table(
    "capture_events",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _uuid("scope_id"),
    _uuid("session_id"),
    sa.Column("sequence", sa.BigInteger(), nullable=False),
    _text("kind"),
    _text("author"),
    _text("content"),
    _text("tool_name", nullable=True),
    _text("payload_hash"),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(
        ["session_id", "scope_id", "workspace_id"],
        ["task_sessions.id", "task_sessions.scope_id", "task_sessions.workspace_id"],
    ),
    sa.UniqueConstraint("session_id", "sequence", name="uq_capture_events_session_sequence"),
    sa.CheckConstraint("sequence > 0", name="sequence_positive"),
    sa.CheckConstraint("kind IN ('message','tool_result')", name="kind_valid"),
    sa.CheckConstraint("author IN ('user','assistant','tool')", name="author_valid"),
    sa.CheckConstraint("octet_length(content) <= 16384", name="content_bounded"),
)
session_summaries = sa.Table(
    "session_summaries",
    metadata,
    _uuid("session_id"),
    _uuid("workspace_id"),
    _uuid("scope_id"),
    _text("summary"),
    sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    _timestamp("updated_at", default_now=True),
    sa.PrimaryKeyConstraint("session_id"),
    sa.ForeignKeyConstraint(
        ["session_id", "scope_id", "workspace_id"],
        ["task_sessions.id", "task_sessions.scope_id", "task_sessions.workspace_id"],
    ),
    sa.CheckConstraint("version > 0", name="version_positive"),
    sa.CheckConstraint("octet_length(summary) <= 16384", name="summary_bounded"),
)
source_memory_evidence = sa.Table(
    "source_memory_evidence",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _uuid("scope_id"),
    _uuid("memory_id"),
    _uuid("source_id"),
    _uuid("source_version_id"),
    _uuid("chunk_id"),
    _uuid("actor_id"),
    _text("request_id"),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("page", sa.Integer(), nullable=True),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(
        ["memory_id", "scope_id", "workspace_id"],
        ["memories.id", "memories.scope_id", "memories.workspace_id"],
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["source_id", "scope_id", "workspace_id"],
        ["sources.id", "sources.scope_id", "sources.workspace_id"],
    ),
    sa.ForeignKeyConstraint(
        ["source_version_id", "source_id", "workspace_id"],
        ["source_versions.id", "source_versions.source_id", "source_versions.workspace_id"],
    ),
    sa.ForeignKeyConstraint(
        ["chunk_id", "source_version_id", "workspace_id"],
        ["source_chunks.id", "source_chunks.source_version_id", "source_chunks.workspace_id"],
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(["actor_id", "workspace_id"], ["actors.id", "actors.workspace_id"]),
    sa.UniqueConstraint("memory_id", "chunk_id", name="uq_source_memory_evidence_memory_chunk"),
    sa.CheckConstraint("ordinal >= 0 AND (page IS NULL OR page > 0)", name="locator_valid"),
)
