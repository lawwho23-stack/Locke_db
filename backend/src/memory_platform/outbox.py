"""Private durable task-event queue. It never accepts skill packages or credentials."""

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from memory_platform.client import APIClient, ClientError

_ALLOWED = re.compile(
    r"^/v1/tasks(?:/[A-Za-z0-9_-]+(?:/(?:checkpoint|handoff|accept-handoff|recover))?)?$"
)


class Outbox:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("Outbox cannot be a symlink.")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
            method TEXT NOT NULL, path TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
            due REAL NOT NULL DEFAULT 0, error TEXT
        )""")
        self.db.commit()

    def __enter__(self) -> "Outbox":
        return self

    def __exit__(self, *args: Any) -> None:
        self.db.close()

    def enqueue(self, method: str, path: str, payload: dict[str, Any]) -> None:
        if method not in {"POST", "PATCH", "DELETE"} or not _ALLOWED.fullmatch(path):
            raise ValueError("Outbox only stores task events.")
        event = payload.get("event_id")
        if not isinstance(event, str) or not event:
            raise ValueError("A stable event_id is required.")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 65536:
            raise ValueError("Task event is too large.")
        forbidden = {
            "authorization",
            "token",
            "api_key",
            "password",
            "files",
            "transcript",
            "reasoning",
        }
        if forbidden & {key.lower() for key in payload}:
            raise ValueError("Task event contains prohibited fields.")
        existing = self.db.execute("SELECT * FROM events WHERE event_id=?", (event,)).fetchone()
        if existing and (existing["payload"], existing["method"], existing["path"]) != (
            encoded,
            method,
            path,
        ):
            raise ValueError("Event identifier was reused with different data.")
        self.db.execute(
            "INSERT OR IGNORE INTO events(event_id,method,path,payload) VALUES(?,?,?,?)",
            (event, method, path, encoded),
        )
        self.db.commit()

    def status(self) -> list[dict[str, Any]]:
        # Status displays metadata only, never the queued checkpoint contents.
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id,event_id,state,attempts,due,error FROM events ORDER BY id"
            )
        ]

    def _recover(self, event_id: str, state: str) -> None:
        # The operator may retry identical bytes or deliberately discard a conflict;
        # changing an existing event's payload would violate server idempotency.
        with self.db:
            changed = self.db.execute(
                "UPDATE events SET state=?,due=0,error=NULL WHERE event_id=? AND state='blocked'",
                (state, event_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Only a blocked event can be recovered.")

    def retry(self, event_id: str) -> None:
        self._recover(event_id, "queued")

    def discard(self, event_id: str) -> None:
        self._recover(event_id, "discarded")

    def flush(self, client: APIClient, *, now: float | None = None) -> dict[str, int]:
        instant = time.time() if now is None else now
        # Serialize flushers with a SQLite transaction so events cannot overtake
        # each other. A lost HTTP response retries the same immutable event_id.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute(
                "SELECT * FROM events WHERE state NOT IN ('synced','discarded') ORDER BY id"
            ).fetchall()
            for row in rows:
                if row["state"] == "blocked" or row["due"] > instant:
                    break
                try:
                    client.request(row["method"], row["path"], json.loads(row["payload"]))
                except (httpx.TransportError, ClientError) as exc:
                    attempts = row["attempts"] + 1
                    retryable = isinstance(exc, httpx.TransportError) or exc.status in {
                        429,
                        500,
                        502,
                        503,
                        504,
                    }
                    error = "transport_error" if isinstance(exc, httpx.TransportError) else exc.code
                    retry_after = exc.retry_after if isinstance(exc, ClientError) else None
                    self.db.execute(
                        "UPDATE events SET state=?,attempts=?,due=?,error=? WHERE id=?",
                        (
                            "queued" if retryable else "blocked",
                            attempts,
                            instant + max(retry_after or 0, min(300, 2 ** min(attempts, 8))),
                            error,
                            row["id"],
                        ),
                    )
                    break
                else:
                    self.db.execute(
                        "UPDATE events SET state='synced',error=NULL WHERE id=?", (row["id"],)
                    )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        counts = {
            row["state"]: row["count"]
            for row in self.db.execute("SELECT state,count(*) AS count FROM events GROUP BY state")
        }
        return {key: counts.get(key, 0) for key in ("synced", "queued", "blocked")}
