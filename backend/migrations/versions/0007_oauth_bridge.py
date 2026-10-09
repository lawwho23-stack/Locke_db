"""MCP OAuth bridge: DCR clients, auth codes, access and refresh tokens (frozen SQL)."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE oauth_clients (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    client_name TEXT NOT NULL,
    redirect_uris TEXT[] NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_oauth_clients PRIMARY KEY (id)
);

CREATE TABLE oauth_auth_codes (
    code_hash TEXT NOT NULL,
    client_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    credential_id UUID NOT NULL,
    scope_id UUID NOT NULL,
    capabilities TEXT[] NOT NULL,
    code_challenge TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    used_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_oauth_auth_codes PRIMARY KEY (code_hash),
    CONSTRAINT fk_oauth_auth_codes_client_id_oauth_clients
        FOREIGN KEY(client_id) REFERENCES oauth_clients (id),
    CONSTRAINT fk_oauth_auth_codes_credential_id_credentials
        FOREIGN KEY(credential_id, workspace_id)
        REFERENCES credentials (id, workspace_id),
    CONSTRAINT fk_oauth_auth_codes_scope_id_scopes
        FOREIGN KEY(scope_id, workspace_id)
        REFERENCES scopes (id, workspace_id),
    CONSTRAINT ck_oauth_auth_codes_capabilities_non_empty
        CHECK (cardinality(capabilities) > 0)
);
CREATE INDEX ix_oauth_auth_codes_expiry ON oauth_auth_codes (expires_at);

CREATE TABLE oauth_access_tokens (
    token_hash TEXT NOT NULL,
    client_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    credential_id UUID NOT NULL,
    scope_id UUID NOT NULL,
    capabilities TEXT[] NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_oauth_access_tokens PRIMARY KEY (token_hash),
    CONSTRAINT fk_oauth_access_tokens_client_id_oauth_clients
        FOREIGN KEY(client_id) REFERENCES oauth_clients (id),
    CONSTRAINT fk_oauth_access_tokens_credential_id_credentials
        FOREIGN KEY(credential_id, workspace_id)
        REFERENCES credentials (id, workspace_id),
    CONSTRAINT fk_oauth_access_tokens_scope_id_scopes
        FOREIGN KEY(scope_id, workspace_id)
        REFERENCES scopes (id, workspace_id),
    CONSTRAINT ck_oauth_access_tokens_capabilities_non_empty
        CHECK (cardinality(capabilities) > 0)
);
CREATE INDEX ix_oauth_access_tokens_expiry ON oauth_access_tokens (expires_at);
CREATE INDEX ix_oauth_access_tokens_credential
    ON oauth_access_tokens (credential_id, workspace_id);

CREATE TABLE oauth_refresh_tokens (
    token_hash TEXT NOT NULL,
    client_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    credential_id UUID NOT NULL,
    scope_id UUID NOT NULL,
    capabilities TEXT[] NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    rotated_to TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_oauth_refresh_tokens PRIMARY KEY (token_hash),
    CONSTRAINT fk_oauth_refresh_tokens_client_id_oauth_clients
        FOREIGN KEY(client_id) REFERENCES oauth_clients (id),
    CONSTRAINT fk_oauth_refresh_tokens_credential_id_credentials
        FOREIGN KEY(credential_id, workspace_id)
        REFERENCES credentials (id, workspace_id),
    CONSTRAINT fk_oauth_refresh_tokens_scope_id_scopes
        FOREIGN KEY(scope_id, workspace_id)
        REFERENCES scopes (id, workspace_id),
    CONSTRAINT ck_oauth_refresh_tokens_capabilities_non_empty
        CHECK (cardinality(capabilities) > 0)
);
CREATE INDEX ix_oauth_refresh_tokens_expiry ON oauth_refresh_tokens (expires_at);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute("DROP TABLE oauth_refresh_tokens")
    op.execute("DROP TABLE oauth_access_tokens")
    op.execute("DROP TABLE oauth_auth_codes")
    op.execute("DROP TABLE oauth_clients")
