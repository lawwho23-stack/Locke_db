"""API token helpers (contract section 4).

A token looks like `mem_<random url-safe text>`. The plain token is shown to its owner ONCE.
The database stores only its SHA-256 hash. A fast hash is safe here because the token is
long and random (about 256 bits); a slow password hash is only needed for human passwords.
"""

import hashlib
import secrets

TOKEN_PREFIX = "mem_"


def generate_token() -> str:
    """Create a new random token: "mem_" + 43 url-safe characters."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex digest of the FULL token string (including the `mem_` prefix)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def display_prefix(token: str) -> str:
    """First 12 characters. Safe to show in lists so a person can tell tokens apart."""
    return token[:12]
