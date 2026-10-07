"""Add scoped sessions, tasks and durable checkpoint events (frozen SQL)."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE task_sessions (
	id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	scope_id UUID NOT NULL,
	actor_id UUID NOT NULL,
	credential_id UUID NOT NULL,
	client TEXT NOT NULL,
	external_id TEXT NOT NULL,
	last_sequence BIGINT DEFAULT '0' NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_task_sessions PRIMARY KEY (id),
        CONSTRAINT fk_task_sessions_scope_id_scopes FOREIGN KEY(scope_id, workspace_id)
    REFERENCES scopes (id, workspace_id),
        CONSTRAINT fk_task_sessions_actor_id_actors FOREIGN KEY(actor_id, workspace_id)
    REFERENCES actors (id, workspace_id),
        CONSTRAINT fk_task_sessions_credential_id_credentials FOREIGN KEY(credential_id,
    workspace_id) REFERENCES credentials (id, workspace_id),
	CONSTRAINT uq_task_sessions_id_scope_workspace UNIQUE (id, scope_id, workspace_id),
	CONSTRAINT uq_task_sessions_identity UNIQUE (credential_id, scope_id, client, external_id),
	CONSTRAINT ck_task_sessions_sequence_non_negative CHECK (last_sequence >= 0)
)


;

CREATE TABLE tasks (
	id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	scope_id UUID NOT NULL,
	title TEXT NOT NULL,
	goal TEXT NOT NULL,
	status TEXT DEFAULT 'planned' NOT NULL,
	summary TEXT DEFAULT '' NOT NULL,
	current_step TEXT DEFAULT '' NOT NULL,
	next_step TEXT DEFAULT '' NOT NULL,
	blockers JSONB DEFAULT '[]'::jsonb NOT NULL,
	artifact_refs JSONB DEFAULT '[]'::jsonb NOT NULL,
	results JSONB DEFAULT '[]'::jsonb NOT NULL,
	version BIGINT DEFAULT '1' NOT NULL,
	generation BIGINT DEFAULT '1' NOT NULL,
	owner_session_id UUID NOT NULL,
	handoff_session_id UUID,
	deleted_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_tasks PRIMARY KEY (id),
        CONSTRAINT fk_tasks_scope_id_scopes FOREIGN KEY(scope_id, workspace_id) REFERENCES
    scopes (id, workspace_id),
        CONSTRAINT fk_tasks_owner_session_id_task_sessions FOREIGN KEY(owner_session_id,
    scope_id, workspace_id) REFERENCES task_sessions (id, scope_id, workspace_id),
        CONSTRAINT fk_tasks_handoff_session_id_task_sessions FOREIGN KEY(handoff_session_id,
    scope_id, workspace_id) REFERENCES task_sessions (id, scope_id, workspace_id),
	CONSTRAINT uq_tasks_id_scope_workspace UNIQUE (id, scope_id, workspace_id),
        CONSTRAINT ck_tasks_status_valid CHECK (status IN
    ('planned','running','blocked','paused','completed','cancelled')),
	CONSTRAINT ck_tasks_version_positive CHECK (version > 0 AND generation > 0)
)


;

CREATE TABLE task_events (
	id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	scope_id UUID NOT NULL,
	task_id UUID NOT NULL,
	session_id UUID NOT NULL,
	sequence BIGINT NOT NULL,
	kind TEXT NOT NULL,
	payload_hash TEXT NOT NULL,
	result JSONB NOT NULL,
	checkpoint JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_task_events PRIMARY KEY (id),
        CONSTRAINT fk_task_events_task_id_tasks FOREIGN KEY(task_id, scope_id, workspace_id)
    REFERENCES tasks (id, scope_id, workspace_id),
        CONSTRAINT fk_task_events_session_id_task_sessions FOREIGN KEY(session_id, scope_id,
    workspace_id) REFERENCES task_sessions (id, scope_id, workspace_id),
	CONSTRAINT uq_task_events_session_sequence UNIQUE (session_id, sequence),
	CONSTRAINT ck_task_events_sequence_positive CHECK (sequence > 0)
)


;"""


def upgrade() -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("task_events")
    op.drop_table("tasks")
    op.drop_table("task_sessions")
