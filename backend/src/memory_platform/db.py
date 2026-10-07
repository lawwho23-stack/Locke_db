"""Database engine factory (contract section 8).

We use SQLAlchemy Core with the sync psycopg 3 driver. No ORM.
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url


def normalize_url(url: str) -> str:
    """Make sure the URL names the psycopg 3 driver: `postgresql+psycopg://...`.

    Providers often hand out plain `postgresql://` or `postgres://` URLs.
    The password is kept (this returns the real, usable URL; never print it).
    """
    parsed = make_url(url)
    if parsed.drivername in ("postgres", "postgresql"):
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


def describe_url(url: str) -> str:
    """Return `host:port/database` only. Safe to print or log (no user, no password)."""
    parsed = make_url(url)
    return f"{parsed.host}:{parsed.port or 5432}/{parsed.database}"


def make_engine(
    url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    prepare_threshold: int | None = 5,
) -> Engine:
    """Create a connection-pooled engine.

    - `pool_pre_ping=True` checks a connection is alive before use, so a database
      that restarted (or a Neon compute that woke up) does not cause errors.
    - `prepare_threshold` is a psycopg option: None turns prepared statements off.
    """
    return create_engine(
        normalize_url(url),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        connect_args={"prepare_threshold": prepare_threshold},
    )
