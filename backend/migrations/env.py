"""Alembic environment: how migrations connect to the database.

Which database URL is used (first match wins):
  1. `config.attributes["url"]`   - set by code, for example the test fixtures
  2. `-x url=...`                 - command line: `alembic -x url=... upgrade head`
  3. env var `MIGRATION_DATABASE_URL`
  4. Settings (`.env`): `migration_database_url`, else `database_url`

Safety: every run prints the target as host:port/database (never the password).
Check that line before you trust a migration run.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from memory_platform.config import get_settings
from memory_platform.db import describe_url, normalize_url
from memory_platform.schema import metadata

config = context.config

# The test fixtures run Alembic inside pytest. Re-configuring logging there would
# switch off pytest's log capture, so only do it for normal command-line runs.
if config.config_file_name is not None and "url" not in config.attributes:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Autogenerate (`alembic revision --autogenerate`) and `alembic check` compare this
# metadata with the real database.
target_metadata = metadata


def _get_url() -> str:
    from_code = config.attributes.get("url")
    if from_code:
        return str(from_code)
    from_cli = context.get_x_argument(as_dictionary=True).get("url")
    if from_cli:
        return from_cli
    from_env = os.environ.get("MIGRATION_DATABASE_URL")
    if from_env:
        return from_env
    settings = get_settings()
    return settings.migration_database_url or settings.database_url


def _context_options() -> dict[str, object]:
    return {
        "target_metadata": target_metadata,
        # Detect column type and server default changes too (not only added/removed columns).
        "compare_type": True,
        "compare_server_default": True,
    }


def run_migrations_offline() -> None:
    """Write SQL text instead of running it (`alembic upgrade head --sql`)."""
    url = normalize_url(_get_url())
    print(f"[alembic] offline target: {describe_url(url)}", file=sys.stderr)
    context.configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_context_options(),  # type: ignore[arg-type]
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the database and run the migrations."""
    url = normalize_url(_get_url())
    print(f"[alembic] target: {describe_url(url)}", file=sys.stderr)
    # NullPool: a migration is a short one-off job, so do not keep connections around.
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, **_context_options())  # type: ignore[arg-type]
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
