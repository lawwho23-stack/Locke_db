"""Frozen document, vector, worker and usage schema; revision 0004, parent 0003."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE EXTENSION IF NOT EXISTS vector
        """
    )
    op.execute(
        """
        CREATE TABLE sources (
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        workspace_id UUID NOT NULL,
        scope_id UUID NOT NULL,
        title TEXT NOT NULL,
        created_by UUID NOT NULL,
        current_version INTEGER DEFAULT '1' NOT NULL,
        deleted_at TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_sources PRIMARY KEY (id),
        CONSTRAINT fk_sources_scope_id_scopes FOREIGN KEY(scope_id, workspace_id) REFERENCES
        scopes (id, workspace_id),
        CONSTRAINT fk_sources_created_by_actors FOREIGN KEY(created_by, workspace_id)
        REFERENCES actors (id, workspace_id),
        CONSTRAINT uq_sources_id_workspace UNIQUE (id, workspace_id),
        CONSTRAINT ck_sources_version_positive CHECK (current_version >= 1)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE source_versions (
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        source_id UUID NOT NULL,
        workspace_id UUID NOT NULL,
        version INTEGER NOT NULL,
        filename TEXT NOT NULL,
        format TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        storage_key TEXT NOT NULL,
        byte_size INTEGER NOT NULL,
        status TEXT DEFAULT 'queued' NOT NULL,
        summary TEXT,
        error_code TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_source_versions PRIMARY KEY (id),
        CONSTRAINT fk_source_versions_source_id_sources FOREIGN KEY(source_id, workspace_id)
        REFERENCES sources (id, workspace_id),
        CONSTRAINT uq_source_versions_source_version UNIQUE (source_id, version),
        CONSTRAINT uq_source_versions_id_workspace UNIQUE (id, workspace_id),
        CONSTRAINT ck_source_versions_size_version CHECK (version >= 1 AND byte_size >= 0
        AND byte_size <= 20971520),
        CONSTRAINT ck_source_versions_format_valid CHECK (format IN
        ('pdf','md','txt','docx')),
        CONSTRAINT ck_source_versions_status_valid CHECK (status IN
        ('queued','processing','ready','failed','cancelled'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE source_chunks (
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        source_version_id UUID NOT NULL,
        workspace_id UUID NOT NULL,
        ordinal INTEGER NOT NULL,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        page INTEGER,
        embedding VECTOR,
        embedding_model TEXT,
        embedding_dimensions INTEGER,
        CONSTRAINT pk_source_chunks PRIMARY KEY (id),
        CONSTRAINT fk_source_chunks_source_version_id_source_versions FOREIGN
        KEY(source_version_id, workspace_id) REFERENCES source_versions (id, workspace_id),
        CONSTRAINT uq_source_chunks_version_ordinal UNIQUE (source_version_id, ordinal)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE memory_embeddings (
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        memory_id UUID NOT NULL,
        workspace_id UUID NOT NULL,
        memory_version INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        model TEXT NOT NULL,
        dimensions INTEGER NOT NULL,
        embedding VECTOR NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_memory_embeddings PRIMARY KEY (id),
        CONSTRAINT fk_memory_embeddings_memory_id_memories FOREIGN KEY(memory_id,
        workspace_id) REFERENCES memories (id, workspace_id) ON DELETE CASCADE,
        CONSTRAINT uq_memory_embeddings_version_model UNIQUE (memory_id, memory_version,
        model, dimensions)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE knowledge_jobs (
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        workspace_id UUID NOT NULL,
        dedup_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        target_id UUID NOT NULL,
        target_version INTEGER NOT NULL,
        status TEXT DEFAULT 'queued' NOT NULL,
        attempts INTEGER DEFAULT '0' NOT NULL,
        lease_token UUID,
        lease_until TIMESTAMP WITH TIME ZONE,
        available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        error_code TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        finished_at TIMESTAMP WITH TIME ZONE,
        CONSTRAINT pk_knowledge_jobs PRIMARY KEY (id),
        CONSTRAINT fk_knowledge_jobs_workspace_id_workspaces FOREIGN KEY(workspace_id)
        REFERENCES workspaces (id),
        CONSTRAINT uq_knowledge_jobs_workspace_dedup UNIQUE (workspace_id, dedup_key),
        CONSTRAINT ck_knowledge_jobs_kind_valid CHECK (kind IN
        ('source','source_enrichment','source_cleanup','memory_embedding')),
        CONSTRAINT ck_knowledge_jobs_status_valid CHECK (status IN
        ('queued','leased','completed','failed','cancelled')),
        CONSTRAINT ck_knowledge_jobs_attempts_range CHECK (attempts >= 0 AND attempts <= 5)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_jobs_status_available ON knowledge_jobs (status,
        available_at)
        """
    )
    op.execute(
        """
        CREATE TABLE provider_usage (
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        workspace_id UUID NOT NULL,
        operation TEXT NOT NULL,
        model TEXT NOT NULL,
        day DATE NOT NULL,
        reserved_usd NUMERIC(16, 8) NOT NULL,
        charged_usd NUMERIC(16, 8),
        tokens INTEGER,
        metadata JSONB DEFAULT '{}'::jsonb NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        completed_at TIMESTAMP WITH TIME ZONE,
        CONSTRAINT pk_provider_usage PRIMARY KEY (id),
        CONSTRAINT fk_provider_usage_workspace_id_workspaces FOREIGN KEY(workspace_id)
        REFERENCES workspaces (id),
        CONSTRAINT ck_provider_usage_cost_nonnegative CHECK (reserved_usd >= 0 AND
        (charged_usd IS NULL OR charged_usd >= 0))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_provider_usage_workspace_day ON provider_usage (workspace_id, day)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE provider_usage")
    op.execute("DROP TABLE knowledge_jobs")
    op.execute("DROP TABLE memory_embeddings")
    op.execute("DROP TABLE source_chunks")
    op.execute("DROP TABLE source_versions")
    op.execute("DROP TABLE sources")
