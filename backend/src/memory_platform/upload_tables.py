"""Expiring direct-upload receipts; no document contents or credentials."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from memory_platform.tables import _id, _text, _timestamp, _uuid, metadata

upload_receipts = sa.Table(
    "upload_receipts",
    metadata,
    _id(),
    _uuid("workspace_id"),
    _uuid("scope_id"),
    _uuid("credential_id"),
    _uuid("source_id", nullable=True),
    sa.Column("expected_version", sa.Integer(), nullable=True),
    _text("title"),
    _text("filename"),
    sa.Column("byte_size", sa.Integer(), nullable=False),
    _timestamp("expires_at"),
    _timestamp("created_at", default_now=True),
    sa.Column("result", JSONB(), nullable=True),
    sa.ForeignKeyConstraint(["scope_id", "workspace_id"], ["scopes.id", "scopes.workspace_id"]),
    sa.ForeignKeyConstraint(
        ["credential_id", "workspace_id"], ["credentials.id", "credentials.workspace_id"]
    ),
    sa.ForeignKeyConstraint(["source_id", "workspace_id"], ["sources.id", "sources.workspace_id"]),
    sa.CheckConstraint("byte_size > 0 AND byte_size <= 20971520", name="upload_size"),
    sa.CheckConstraint(
        "(source_id IS NULL AND expected_version IS NULL) OR "
        "(source_id IS NOT NULL AND expected_version IS NOT NULL AND expected_version >= 1)",
        name="upload_replacement",
    ),
)
sa.Index("ix_upload_receipts_expiry", upload_receipts.c.expires_at)
