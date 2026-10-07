"""Frozen selected capture and provenance schema; revision 0005."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
ALTER TABLE memories ADD CONSTRAINT uq_memories_id_scope_workspace UNIQUE (id, scope_id,
workspace_id)
"""
    )
    op.execute(
        """
ALTER TABLE sources ADD CONSTRAINT uq_sources_id_scope_workspace UNIQUE (id, scope_id,
workspace_id)
"""
    )
    op.execute(
        """
ALTER TABLE source_versions ADD CONSTRAINT uq_source_versions_id_source_workspace UNIQUE
(id, source_id, workspace_id)
"""
    )
    op.execute(
        """
ALTER TABLE source_chunks ADD CONSTRAINT uq_source_chunks_id_version_workspace UNIQUE
(id, source_version_id, workspace_id)
"""
    )
    op.execute("""
CREATE TABLE capture_events (
id UUID DEFAULT gen_random_uuid() NOT NULL,
workspace_id UUID NOT NULL,
scope_id UUID NOT NULL,
session_id UUID NOT NULL,
sequence BIGINT NOT NULL,
kind TEXT NOT NULL,
author TEXT NOT NULL,
content TEXT NOT NULL,
tool_name TEXT,
payload_hash TEXT NOT NULL,
created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
CONSTRAINT pk_capture_events PRIMARY KEY (id),
CONSTRAINT fk_capture_events_session_id_task_sessions FOREIGN KEY(session_id, scope_id,
workspace_id) REFERENCES task_sessions (id, scope_id, workspace_id),
CONSTRAINT uq_capture_events_session_sequence UNIQUE (session_id, sequence),
CONSTRAINT ck_capture_events_sequence_positive CHECK (sequence > 0),
CONSTRAINT ck_capture_events_kind_valid CHECK (kind IN ('message','tool_result')),
CONSTRAINT ck_capture_events_author_valid CHECK (author IN ('user','assistant','tool')),
CONSTRAINT ck_capture_events_content_bounded CHECK (octet_length(content) <= 16384)
)
""")
    op.execute("""
CREATE TABLE session_summaries (
session_id UUID NOT NULL,
workspace_id UUID NOT NULL,
scope_id UUID NOT NULL,
summary TEXT NOT NULL,
version INTEGER DEFAULT '1' NOT NULL,
updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
CONSTRAINT pk_session_summaries PRIMARY KEY (session_id),
CONSTRAINT fk_session_summaries_session_id_task_sessions FOREIGN KEY(session_id,
scope_id, workspace_id) REFERENCES task_sessions (id, scope_id, workspace_id),
CONSTRAINT ck_session_summaries_version_positive CHECK (version > 0),
CONSTRAINT ck_session_summaries_summary_bounded CHECK (octet_length(summary) <= 16384)
)
""")
    op.execute("""
CREATE TABLE source_memory_evidence (
id UUID DEFAULT gen_random_uuid() NOT NULL,
workspace_id UUID NOT NULL,
scope_id UUID NOT NULL,
memory_id UUID NOT NULL,
source_id UUID NOT NULL,
source_version_id UUID NOT NULL,
chunk_id UUID NOT NULL,
actor_id UUID NOT NULL,
request_id TEXT NOT NULL,
ordinal INTEGER NOT NULL,
page INTEGER,
created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
CONSTRAINT pk_source_memory_evidence PRIMARY KEY (id),
CONSTRAINT fk_source_memory_evidence_memory_id_memories FOREIGN KEY(memory_id, scope_id,
workspace_id) REFERENCES memories (id, scope_id, workspace_id) ON DELETE CASCADE,
CONSTRAINT fk_source_memory_evidence_source_id_sources FOREIGN KEY(source_id, scope_id,
workspace_id) REFERENCES sources (id, scope_id, workspace_id),
CONSTRAINT fk_source_memory_evidence_source_version_id_source_versions FOREIGN
KEY(source_version_id, source_id, workspace_id) REFERENCES source_versions (id,
source_id, workspace_id),
CONSTRAINT fk_source_memory_evidence_chunk_id_source_chunks FOREIGN KEY(chunk_id,
source_version_id, workspace_id) REFERENCES source_chunks (id, source_version_id,
workspace_id) ON DELETE CASCADE,
CONSTRAINT fk_source_memory_evidence_actor_id_actors FOREIGN KEY(actor_id, workspace_id)
REFERENCES actors (id, workspace_id),
CONSTRAINT uq_source_memory_evidence_memory_chunk UNIQUE (memory_id, chunk_id),
CONSTRAINT ck_source_memory_evidence_locator_valid CHECK (ordinal >= 0 AND (page IS NULL
OR page > 0))
)
""")


def downgrade() -> None:
    op.execute("DROP TABLE source_memory_evidence")
    op.execute("DROP TABLE session_summaries")
    op.execute("DROP TABLE capture_events")
    op.execute("ALTER TABLE memories DROP CONSTRAINT uq_memories_id_scope_workspace")
    op.execute("ALTER TABLE sources DROP CONSTRAINT uq_sources_id_scope_workspace")
    op.execute("ALTER TABLE source_versions DROP CONSTRAINT uq_source_versions_id_source_workspace")
    op.execute("ALTER TABLE source_chunks DROP CONSTRAINT uq_source_chunks_id_version_workspace")
