"""Immutable approved skills and expanded capability allowlist (frozen SQL)."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE skills (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	workspace_id UUID NOT NULL,
	scope_id UUID NOT NULL,
	command TEXT NOT NULL,
	revision BIGINT DEFAULT 1 NOT NULL,
	active_version INTEGER,
	revoked BOOLEAN DEFAULT false NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_skills PRIMARY KEY (id),
	CONSTRAINT fk_skills_scope_id_scopes FOREIGN KEY(scope_id, workspace_id)
        REFERENCES scopes (id, workspace_id),
	CONSTRAINT uq_skills_id_workspace UNIQUE (id, workspace_id),
	CONSTRAINT uq_skills_scope_command UNIQUE (scope_id, command),
	CONSTRAINT ck_skills_command_format CHECK (command ~ '^/[a-z][a-z0-9_]{0,63}$'),
	CONSTRAINT ck_skills_revision_positive CHECK (revision >= 1)
)

;

CREATE TABLE skill_versions (
	skill_id UUID NOT NULL,
	version INTEGER NOT NULL,
	workspace_id UUID NOT NULL,
	package_hash TEXT NOT NULL,
	approved_by UUID NOT NULL,
	approved_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_skill_versions PRIMARY KEY (skill_id, version),
	CONSTRAINT fk_skill_versions_skill_id_skills FOREIGN KEY(skill_id, workspace_id)
        REFERENCES skills (id, workspace_id),
	CONSTRAINT fk_skill_versions_approved_by_actors FOREIGN KEY(approved_by, workspace_id)
        REFERENCES actors (id, workspace_id),
	CONSTRAINT uq_skill_versions_identity UNIQUE (skill_id, version, workspace_id),
	CONSTRAINT ck_skill_versions_version_positive CHECK (version >= 1),
	CONSTRAINT ck_skill_versions_hash_format CHECK (package_hash ~ '^[0-9a-f]{64}$')
)

;

CREATE TABLE skill_files (
	skill_id UUID NOT NULL,
	version INTEGER NOT NULL,
	path TEXT NOT NULL,
	workspace_id UUID NOT NULL,
	content BYTEA NOT NULL,
	sha256 TEXT NOT NULL,
	executable BOOLEAN NOT NULL,
	CONSTRAINT pk_skill_files PRIMARY KEY (skill_id, version, path),
	CONSTRAINT fk_skill_files_skill_id_skill_versions FOREIGN KEY(skill_id, version, workspace_id)
        REFERENCES skill_versions (skill_id, version, workspace_id),
	CONSTRAINT ck_skill_files_file_size CHECK (octet_length(content) <= 1048576),
	CONSTRAINT ck_skill_files_hash_format CHECK (sha256 ~ '^[0-9a-f]{64}$')
)

;
ALTER TABLE skills ADD CONSTRAINT fk_skills_active_version
        FOREIGN KEY(id, active_version, workspace_id)
        REFERENCES skill_versions (skill_id, version, workspace_id);"""


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_credential_grants_capabilities_valid"), "credential_grants", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_credential_grants_capabilities_valid"),
        "credential_grants",
        "capabilities <@ ARRAY['memory:read','memory:write','memory:delete',"
        "'source:ingest','task:read','task:write','skill:read','skill:write']::text[] "
        "AND cardinality(capabilities) > 0",
    )
    op.execute(SCHEMA)
    op.execute(
        "CREATE FUNCTION reject_skill_version_mutation() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'Approved skill versions are immutable'; END $$"
    )
    for table in ("skill_versions", "skill_files"):
        op.execute(
            f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_skill_version_mutation()"
        )


def downgrade() -> None:
    op.drop_constraint("fk_skills_active_version", "skills", type_="foreignkey")
    op.drop_table("skill_files")
    op.drop_table("skill_versions")
    op.drop_table("skills")
    op.execute("DROP FUNCTION reject_skill_version_mutation()")
    op.drop_constraint(
        op.f("ck_credential_grants_capabilities_valid"), "credential_grants", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_credential_grants_capabilities_valid"),
        "credential_grants",
        "capabilities <@ ARRAY['memory:read','memory:write','memory:delete',"
        "'source:ingest']::text[] AND cardinality(capabilities) > 0",
    )
