"""Private, process-serialized local receipts and immutable pending operations."""

import fcntl
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


class HookState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = path.with_suffix(".lock")
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        self.lock = os.fdopen(os.open(lock_path, flags, 0o600), "w")
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.lock.close()
                    raise TimeoutError("Hook state is busy; try again later.") from None
                time.sleep(0.02)
        fd = os.open(path, flags, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, namespace TEXT NOT NULL, turn TEXT NOT NULL,
                payload TEXT NOT NULL, operations TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)

    def __enter__(self) -> "HookState":
        return self

    def __exit__(self, *args: Any) -> None:
        self.db.close()
        self.lock.close()

    def get(self, key: str) -> dict[str, Any]:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else {}

    def put(self, key: str, value: dict[str, Any]) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def jobs(self, namespace: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM jobs WHERE namespace=? "
                "AND status NOT IN ('saved','discarded') ORDER BY rowid",
                (namespace,),
            )
        ]

    def enqueue(self, job_id: str, namespace: str, turn: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO jobs (id,namespace,turn,payload,operations) VALUES (?,?,?,?,?)",
            (job_id, namespace, turn, json.dumps(payload), "[]"),
        )

    def job(self, job_id: str) -> dict[str, Any]:
        return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def update_job(self, job_id: str, **changes: Any) -> None:
        if not changes.keys() <= {"operations", "position", "status", "error"}:
            raise ValueError("Invalid journal field.")
        columns = ",".join(f"{key}=?" for key in changes)
        self.db.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*changes.values(), job_id))

    def origins(self, group: str) -> list[dict[str, Any]]:
        candidates = self.db.execute("SELECT key,value FROM state WHERE key LIKE 'origin:%'")
        return [
            json.loads(row["value"]) | {"key": row["key"][7:]}
            for row in candidates
            if json.loads(row["value"]).get("group") == group
        ]

    def receipt(self, namespace: str, turn: str) -> dict[str, Any]:
        # A turn-level receipt cannot hide other unfinished publications in that turn.
        unfinished = [job for job in self.jobs(namespace) if job["turn"] == turn]
        if unfinished:
            job = next((job for job in unfinished if job["status"] == "blocked"), unfinished[0])
            return {"status": job["status"], "job_id": job["id"], "error": job["error"]}
        saved = self.db.execute(
            "SELECT id FROM jobs WHERE namespace=? AND turn=? AND status='saved' "
            "ORDER BY rowid DESC LIMIT 1",
            (namespace, turn),
        ).fetchone()
        if saved:
            return self.get("job-result:" + saved["id"]) | {"status": "saved"}
        return self.get(f"receipt:{namespace}:{turn}")

    def saved_receipts(self, namespace: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id,payload FROM jobs WHERE namespace=? AND status='saved' "
            "ORDER BY rowid DESC LIMIT 100",
            (namespace,),
        )
        receipts = []
        seen = set()
        for row in rows:
            receipt = self.get("job-result:" + row["id"])
            task_id = receipt.get("task_id")
            if task_id and task_id not in seen:
                receipts.append(receipt | {"task_status": json.loads(row["payload"])["status"]})
                seen.add(task_id)
                if len(receipts) >= 20:
                    break
        return receipts
