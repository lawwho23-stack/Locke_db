"""MCP OAuth bridge tables: DCR clients, auth codes, access and refresh tokens.

All secrets are stored as SHA-256 hashes, never plain text. Each code and token
carries its workspace, credential, scope and capabilities so the existing scope
checks in `services/access.py` keep working without duplication.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from memory_platform.tables import _id, _text, _timestamp, _uuid, metadata

oauth_clients = sa.Table(
    "oauth_clients",
    metadata,
    _id(),
    _text("client_name"),
    sa.Column("redirect_uris", postgresql.ARRAY(sa.Text()), nullable=False),
    _timestamp("created_at", default_now=True),
)

oauth_auth_codes = sa.Table(
    "oauth_auth_codes",
    metadata,
    sa.Column("code_hash", sa.Text(), primary_key=True),
    _uuid("client_id"),
    _uuid("workspace_id"),
    _uuid("credential_id"),
    _uuid("scope_id"),
    sa.Column("capabilities", postgresql.ARRAY(sa.Text()), nullable=False),
    _text("code_challenge"),
    _text("redirect_uri"),
    _timestamp("expires_at"),
    _timestamp("used_at", nullable=True),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(["client_id"], ["oauth_clients.id"]),
    sa.ForeignKeyConstraint(
        ["credential_id", "workspace_id"],
        ["credentials.id", "credentials.workspace_id"],
    ),
    sa.ForeignKeyConstraint(
        ["scope_id", "workspace_id"],
        ["scopes.id", "scopes.workspace_id"],
    ),
    sa.CheckConstraint("cardinality(capabilities) > 0", name="capabilities_non_empty"),
)
sa.Index("ix_oauth_auth_codes_expiry", oauth_auth_codes.c.expires_at)

oauth_access_tokens = sa.Table(
    "oauth_access_tokens",
    metadata,
    sa.Column("token_hash", sa.Text(), primary_key=True),
    _uuid("client_id"),
    _uuid("workspace_id"),
    _uuid("credential_id"),
    _uuid("scope_id"),
    sa.Column("capabilities", postgresql.ARRAY(sa.Text()), nullable=False),
    _timestamp("expires_at"),
    _timestamp("revoked_at", nullable=True),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(["client_id"], ["oauth_clients.id"]),
    sa.ForeignKeyConstraint(
        ["credential_id", "workspace_id"],
        ["credentials.id", "credentials.workspace_id"],
    ),
    sa.ForeignKeyConstraint(
        ["scope_id", "workspace_id"],
        ["scopes.id", "scopes.workspace_id"],
    ),
    sa.CheckConstraint("cardinality(capabilities) > 0", name="capabilities_non_empty"),
)
sa.Index("ix_oauth_access_tokens_expiry", oauth_access_tokens.c.expires_at)
sa.Index(
    "ix_oauth_access_tokens_credential",
    oauth_access_tokens.c.credential_id,
    oauth_access_tokens.c.workspace_id,
)

oauth_refresh_tokens = sa.Table(
    "oauth_refresh_tokens",
    metadata,
    sa.Column("token_hash", sa.Text(), primary_key=True),
    _uuid("client_id"),
    _uuid("workspace_id"),
    _uuid("credential_id"),
    _uuid("scope_id"),
    sa.Column("capabilities", postgresql.ARRAY(sa.Text()), nullable=False),
    _timestamp("expires_at"),
    _timestamp("revoked_at", nullable=True),
    _text("rotated_to", nullable=True),
    _timestamp("created_at", default_now=True),
    sa.ForeignKeyConstraint(["client_id"], ["oauth_clients.id"]),
    sa.ForeignKeyConstraint(
        ["credential_id", "workspace_id"],
        ["credentials.id", "credentials.workspace_id"],
    ),
    sa.ForeignKeyConstraint(
        ["scope_id", "workspace_id"],
        ["scopes.id", "scopes.workspace_id"],
    ),
    sa.CheckConstraint("cardinality(capabilities) > 0", name="capabilities_non_empty"),
)
sa.Index("ix_oauth_refresh_tokens_expiry", oauth_refresh_tokens.c.expires_at)

# One-time CSRF states for social sign-in (GitHub). No secrets, no user data.
oauth_login_states = sa.Table(
    "oauth_login_states",
    metadata,
    sa.Column("state_hash", sa.Text(), primary_key=True),
    _text("provider"),
    _text("next_url", nullable=True),
    _timestamp("expires_at"),
    _timestamp("used_at", nullable=True),
    _timestamp("created_at", default_now=True),
    sa.CheckConstraint("provider IN ('github')", name="provider_valid"),
)
sa.Index("ix_oauth_login_states_expiry", oauth_login_states.c.expires_at)
