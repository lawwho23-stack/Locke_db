"""One-time CSRF states for GitHub owner sign-in (frozen SQL)."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE oauth_login_states (
    state_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    next_url TEXT,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    used_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_oauth_login_states PRIMARY KEY (state_hash),
    CONSTRAINT ck_oauth_login_states_provider_valid
        CHECK (provider IN ('github'))
);
CREATE INDEX ix_oauth_login_states_expiry ON oauth_login_states (expires_at);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute("DROP TABLE oauth_login_states")
