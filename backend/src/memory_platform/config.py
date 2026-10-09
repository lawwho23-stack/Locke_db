"""Settings, loaded from environment variables and the repo-root `.env` file (contract section 8).

Secrets live only in `.env` (never committed). Real environment variables win over `.env`.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/src/memory_platform/config.py -> parents[3] is the repo root.
# We do not use the current directory, because tools run from different folders
# (for example `cd backend` before running Alembic).
BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        extra="ignore",
        # If validation fails, do not echo the bad value (it may be a password or a key).
        hide_input_in_errors=True,
    )

    # App connection (Neon pooled URL, restricted role).
    database_url: str
    # Hosted MCP forwards to this API origin using the caller's scoped credential.
    memory_api_url: str | None = None
    # Owner-role direct URL, used only for migrations and the admin CLI.
    migration_database_url: str | None = None
    # Local Docker server used by tests (the tests create random databases on it).
    test_database_url: str | None = None
    # 64 hex characters = 32 bytes. Used to HMAC forgotten text so it can be matched
    # later without storing it. If it is lost, old suppressions stop matching.
    memory_hmac_key: SecretStr

    idempotency_ttl_hours: int = 24
    max_body_bytes: int = 65536
    storage_dir: Path = REPO_ROOT / ".data" / "sources"
    storage_provider: Literal["local", "vercel_blob"] = "local"
    blob_read_write_token: SecretStr | None = None
    hosted_enrichment_batch_size: int = Field(default=0, ge=0, le=8)
    cron_secret: SecretStr | None = None
    cron_workspace_id: UUID | None = None
    provider_base_url: str | None = None
    provider_api_key: SecretStr | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = Field(default=None, ge=1, le=4096)
    extraction_model: str | None = None
    provider_daily_budget_usd: float = Field(default=0, ge=0, allow_inf_nan=False)
    embedding_usd_per_million_tokens: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    extraction_usd_per_million_tokens: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    provider_referer: str | None = None
    provider_title: str | None = None
    redis_rest_url: str | None = None
    redis_rest_token: SecretStr | None = None
    cache_ttl_seconds: int = Field(default=180, ge=1, le=300)
    quota_memories: int = Field(default=10000, ge=0)
    quota_tasks: int = Field(default=1000, ge=0)
    quota_sources: int = Field(default=1000, ge=0)
    quota_source_bytes: int = Field(default=104857600, ge=0)
    quota_skill_bytes: int = Field(default=10485760, ge=0)
    session_retention_days: int = Field(default=0, ge=0)
    event_retention_days: int = Field(default=0, ge=0)
    rate_limit_per_minute: int = Field(default=120, ge=0)
    # psycopg prepares a statement after this many uses. Set None to turn it off
    # if the Neon pooler rejects prepared statements.
    db_prepare_threshold: int | None = 5
    # MCP OAuth bridge (owner-only, no public signup). Issuer defaults to the
    # public API origin; override only when behind a custom domain.
    oauth_issuer: str | None = None
    # Browser-facing authorize page (dashboard). When set, discovery advertises
    # this URL so coding tools open the dashboard login+consent screen, which
    # then approves via the backend with the owner session.
    oauth_authorize_url: str | None = None
    # Single owner allowlist for social login. When set, the authorize step
    # requires this email; otherwise any owner admin credential may approve.
    owner_email: str | None = None
    # Optional social login (Google/GitHub). Dashboard shows only configured buttons.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    github_client_id: str | None = None
    github_client_secret: SecretStr | None = None
    oauth_access_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    oauth_refresh_ttl_days: int = Field(default=30, ge=1, le=90)
    oauth_code_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    # Lifetime of the owner credential minted by a GitHub sign-in, in hours.
    github_oauth_ttl_hours: int = Field(default=12, ge=1, le=72)

    @field_validator("memory_hmac_key")
    @classmethod
    def _check_hmac_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            raise ValueError("MEMORY_HMAC_KEY must be 64 hex characters") from None
        if len(raw) != 64 or len(key) != 32:
            raise ValueError("MEMORY_HMAC_KEY must be 64 hex characters")
        return value

    @property
    def hmac_key_bytes(self) -> bytes:
        return bytes.fromhex(self.memory_hmac_key.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once and reuse them (the cache avoids re-reading `.env` per request)."""
    return Settings()
