"""Shared task metadata, sessions and indefinitely deduplicated events."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from memory_platform.tables import metadata


def build_task_tables(meta: sa.MetaData) -> tuple[sa.Table, sa.Table, sa.Table]:
    sessions = sa.Table(
        "task_sessions",
        meta,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("client", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["scope_id", "workspace_id"], ["scopes.id", "scopes.workspace_id"]),
        sa.ForeignKeyConstraint(["actor_id", "workspace_id"], ["actors.id", "actors.workspace_id"]),
        sa.ForeignKeyConstraint(
            ["credential_id", "workspace_id"], ["credentials.id", "credentials.workspace_id"]
        ),
        sa.UniqueConstraint(
            "id", "scope_id", "workspace_id", name="uq_task_sessions_id_scope_workspace"
        ),
        sa.UniqueConstraint(
            "credential_id", "scope_id", "client", "external_id", name="uq_task_sessions_identity"
        ),
        sa.CheckConstraint("last_sequence >= 0", name="sequence_non_negative"),
    )
    tasks = sa.Table(
        "tasks",
        meta,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="planned"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("current_step", sa.Text(), nullable=False, server_default=""),
        sa.Column("next_step", sa.Text(), nullable=False, server_default=""),
        sa.Column("blockers", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("artifact_refs", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("results", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("owner_session_id", sa.Uuid(), nullable=False),
        sa.Column("handoff_session_id", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["scope_id", "workspace_id"], ["scopes.id", "scopes.workspace_id"]),
        sa.ForeignKeyConstraint(
            ["owner_session_id", "scope_id", "workspace_id"],
            ["task_sessions.id", "task_sessions.scope_id", "task_sessions.workspace_id"],
        ),
        sa.ForeignKeyConstraint(
            ["handoff_session_id", "scope_id", "workspace_id"],
            ["task_sessions.id", "task_sessions.scope_id", "task_sessions.workspace_id"],
        ),
        sa.UniqueConstraint("id", "scope_id", "workspace_id", name="uq_tasks_id_scope_workspace"),
        sa.CheckConstraint(
            "status IN ('planned','running','blocked','paused','completed','cancelled')",
            name="status_valid",
        ),
        sa.CheckConstraint("version > 0 AND generation > 0", name="version_positive"),
    )
    events = sa.Table(
        "task_events",
        meta,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("checkpoint", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "scope_id", "workspace_id"],
            ["tasks.id", "tasks.scope_id", "tasks.workspace_id"],
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "scope_id", "workspace_id"],
            ["task_sessions.id", "task_sessions.scope_id", "task_sessions.workspace_id"],
        ),
        sa.UniqueConstraint("session_id", "sequence", name="uq_task_events_session_sequence"),
        sa.CheckConstraint("sequence > 0", name="sequence_positive"),
    )
    return sessions, tasks, events


task_sessions, tasks, task_events = build_task_tables(metadata)
