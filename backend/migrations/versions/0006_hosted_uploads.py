"""Private hosted upload receipts and job-kind compatibility."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE upload_receipts (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id UUID NOT NULL, scope_id UUID NOT NULL, credential_id UUID NOT NULL,
 source_id UUID, expected_version INTEGER, title TEXT NOT NULL, filename TEXT NOT NULL,
 byte_size INTEGER NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), result JSONB,
 CONSTRAINT fk_upload_receipts_scope_id_scopes FOREIGN KEY(scope_id, workspace_id)
 REFERENCES scopes(id, workspace_id),
 CONSTRAINT fk_upload_receipts_credential_id_credentials FOREIGN KEY(credential_id, workspace_id)
 REFERENCES credentials(id, workspace_id),
 CONSTRAINT fk_upload_receipts_source_id_sources FOREIGN KEY(source_id, workspace_id)
 REFERENCES sources(id, workspace_id),
 CONSTRAINT ck_upload_receipts_upload_size CHECK(byte_size > 0 AND byte_size <= 20971520),
 CONSTRAINT ck_upload_receipts_upload_replacement CHECK(
 (source_id IS NULL AND expected_version IS NULL) OR
 (source_id IS NOT NULL AND expected_version IS NOT NULL AND expected_version >= 1))
)
""")
    op.execute("CREATE INDEX ix_upload_receipts_expiry ON upload_receipts(expires_at)")
    # Upgrade old databases as well as fresh installs; never rewrite a frozen migration.
    op.execute("ALTER TABLE knowledge_jobs DROP CONSTRAINT ck_knowledge_jobs_kind_valid")
    op.execute("""ALTER TABLE knowledge_jobs ADD CONSTRAINT ck_knowledge_jobs_kind_valid
CHECK(kind IN ('source','source_enrichment','source_cleanup','memory_embedding'))""")


def downgrade() -> None:
    op.execute("DROP TABLE upload_receipts")
    # Retain the valid job kinds used by the existing application.
