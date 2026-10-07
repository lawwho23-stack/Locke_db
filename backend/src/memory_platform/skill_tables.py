"""Approved skill registry with scope-safe immutable package versions."""

import sqlalchemy as sa

from memory_platform.tables import metadata

skills = sa.Table(
    "skills",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("workspace_id", sa.Uuid(), nullable=False),
    sa.Column("scope_id", sa.Uuid(), nullable=False),
    sa.Column("command", sa.Text(), nullable=False),
    sa.Column("revision", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
    sa.Column("active_version", sa.Integer(), nullable=True),
    sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    ),
    sa.ForeignKeyConstraint(["scope_id", "workspace_id"], ["scopes.id", "scopes.workspace_id"]),
    sa.UniqueConstraint("id", "workspace_id", name="uq_skills_id_workspace"),
    sa.UniqueConstraint("scope_id", "command", name="uq_skills_scope_command"),
    sa.CheckConstraint("command ~ '^/[a-z][a-z0-9_]{0,63}$'", name="command_format"),
    sa.CheckConstraint("revision >= 1", name="revision_positive"),
)

skill_versions = sa.Table(
    "skill_versions",
    metadata,
    sa.Column("skill_id", sa.Uuid(), primary_key=True),
    sa.Column("version", sa.Integer(), primary_key=True),
    sa.Column("workspace_id", sa.Uuid(), nullable=False),
    sa.Column("package_hash", sa.Text(), nullable=False),
    sa.Column("approved_by", sa.Uuid(), nullable=False),
    sa.Column(
        "approved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    ),
    sa.ForeignKeyConstraint(["skill_id", "workspace_id"], ["skills.id", "skills.workspace_id"]),
    sa.ForeignKeyConstraint(["approved_by", "workspace_id"], ["actors.id", "actors.workspace_id"]),
    sa.UniqueConstraint("skill_id", "version", "workspace_id", name="uq_skill_versions_identity"),
    sa.CheckConstraint("version >= 1", name="version_positive"),
    sa.CheckConstraint("package_hash ~ '^[0-9a-f]{64}$'", name="hash_format"),
)

skills.append_constraint(
    sa.ForeignKeyConstraint(
        ["id", "active_version", "workspace_id"],
        ["skill_versions.skill_id", "skill_versions.version", "skill_versions.workspace_id"],
        name="fk_skills_active_version",
        use_alter=True,
    )
)

skill_files = sa.Table(
    "skill_files",
    metadata,
    sa.Column("skill_id", sa.Uuid(), primary_key=True),
    sa.Column("version", sa.Integer(), primary_key=True),
    sa.Column("path", sa.Text(), primary_key=True),
    sa.Column("workspace_id", sa.Uuid(), nullable=False),
    sa.Column("content", sa.LargeBinary(), nullable=False),
    sa.Column("sha256", sa.Text(), nullable=False),
    sa.Column("executable", sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(
        ["skill_id", "version", "workspace_id"],
        ["skill_versions.skill_id", "skill_versions.version", "skill_versions.workspace_id"],
    ),
    sa.CheckConstraint("octet_length(content) <= 1048576", name="file_size"),
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="hash_format"),
)
