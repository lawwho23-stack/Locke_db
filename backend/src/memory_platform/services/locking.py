"""Transaction locks for a scope's write/forget barrier; no schema changes needed."""

import hashlib
from uuid import UUID

from sqlalchemy import Connection, text


def lock_scope(conn: Connection, scope_id: UUID, *, shared: bool = False) -> None:
    # Shared remembers can race on the fact-key index. Updates/forgetting are exclusive:
    # this prevents suppression-check gaps and opposite-order memory-row locks.
    digest = hashlib.sha256(b"memory-scope:" + scope_id.bytes).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    conn.execute(text(f"SELECT {function}(:key)"), {"key": key})
