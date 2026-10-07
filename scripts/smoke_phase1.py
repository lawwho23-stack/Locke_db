"""Real CLI + Uvicorn + HTTP acceptance on an owned disposable Docker database.

Run: uv run python scripts/smoke_phase1.py
Never loads deployment URLs or prints generated tokens. Only localhost:5433 is allowed.
"""

import argparse
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from memory_platform.db import normalize_url
from memory_platform.tables import activity, memories, memory_versions, operations

ROOT = Path(__file__).resolve().parents[1]


def _cli(arguments: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "memory_platform.admin", *arguments],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError("Local admin CLI failed.")
    return result.stdout


def run(server_url: str) -> None:
    parsed = make_url(normalize_url(server_url))
    if parsed.host not in {"localhost", "127.0.0.1", "::1"} or parsed.port != 5433:
        raise ValueError("Smoke tests require the local Docker server on port 5433.")
    db_name = f"memsmoke_{uuid4().hex[:12]}"
    admin_engine = create_engine(parsed, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    database_created = False
    server: subprocess.Popen[bytes] | None = None
    test_engine = None
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        database_created = True
        url = parsed.set(database=db_name).render_as_string(hide_password=False)
        config = Config(str(ROOT / "backend" / "alembic.ini"))
        config.attributes["url"] = url
        command.upgrade(config, "head")
        command.check(config)
        env = dict(os.environ) | {
            "DATABASE_URL": url,
            "MIGRATION_DATABASE_URL": url,
            "TEST_DATABASE_URL": url,
            "MEMORY_HMAC_KEY": secrets.token_hex(32),
        }
        output = _cli(["bootstrap", "--owner-name", "Smoke Owner"], env)
        owner_token = next(word for word in output.split() if word.startswith("mem_"))
        _cli(["create-scope", "--kind", "project", "--name", "smoke-project"], env)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "memory_platform.api.app:create_default_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base_url, timeout=5) as client:
            deadline = time.monotonic() + 15
            while True:
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if server.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Local API failed to start.")
                time.sleep(0.1)
            owner = {"Authorization": f"Bearer {owner_token}"}
            available = client.get("/v1/scopes", headers=owner).json()["items"]
            project = next(s for s in available if s["name"] == "smoke-project")
            personal = next(s for s in available if s["name"] == "personal")
            issued = client.post(
                "/v1/credentials",
                headers=owner,
                json={
                    "display_name": "Smoke Agent",
                    "grants": [
                        {
                            "scope_id": project["id"],
                            "capabilities": ["memory:read", "memory:write", "memory:delete"],
                        }
                    ],
                },
            )
            assert issued.status_code == 201, "Issue credential"
            agent = {"Authorization": f"Bearer {issued.json()['token']}"}
            assert (
                client.get(
                    "/v1/memories", params={"scope_id": personal["id"]}, headers=agent
                ).status_code
                == 404
            ), "Scope concealment"
            req = {
                "scope_id": project["id"],
                "type": "fact",
                "content": "မြန်မာ English memory",
                "fact_key": "mixed-language",
            }
            retry_headers = agent | {"Idempotency-Key": "smoke-create"}
            created = client.post("/v1/memories", headers=retry_headers, json=req)
            assert created.status_code == 201, "Remember"
            mid = created.json()["id"]
            replay = client.post("/v1/memories", headers=retry_headers, json=req)
            assert replay.status_code == 201 and replay.json()["replayed"], "Retry replay"
            detail = client.get(f"/v1/memories/{mid}", headers=agent)
            assert detail.json()["content"] == req["content"], "Read"
            update = client.patch(
                f"/v1/memories/{mid}",
                headers=agent,
                json={"expected_version": 1, "content": "updated mixed language"},
            )
            assert update.status_code == 200 and update.json()["version"] == 2, "Update"
            stale = client.patch(
                f"/v1/memories/{mid}", headers=agent, json={"expected_version": 1, "labels": []}
            )
            assert stale.status_code == 409, "Version conflict"
            forgotten = client.delete(
                f"/v1/memories/{mid}", headers=agent | {"Idempotency-Key": "smoke-delete"}
            )
            assert forgotten.status_code == 200 and forgotten.json()["version"] == 3, "Forget"
            assert client.get(f"/v1/memories/{mid}", headers=agent).status_code == 404, (
                "Deleted concealment"
            )
            suppressed = client.post("/v1/memories", headers=agent, json=req)
            assert (
                suppressed.status_code == 409
                and suppressed.json()["error"]["code"] == "memory_suppressed"
            ), "Suppression"
            test_engine = create_engine(url, poolclass=NullPool)
            with test_engine.connect() as conn:
                tombstone = (
                    conn.execute(select(memories).where(memories.c.id == UUID(mid)))
                    .mappings()
                    .one()
                )
                assert (
                    tombstone["content"] is None
                    and tombstone["fact_key"] is None
                    and tombstone["labels"] == []
                ), "Current-text scrub"
                history = (
                    conn.execute(
                        select(memory_versions).where(memory_versions.c.memory_id == UUID(mid))
                    )
                    .mappings()
                    .all()
                )
                assert all(
                    v["content"] is None and v["fact_key"] is None and v["labels"] == []
                    for v in history
                ), "History scrub"
                assert all(
                    "content" not in body
                    for body in conn.execute(select(operations.c.response_body)).scalars()
                ), "Replay privacy"
                assert "content" not in activity.c, "Audit privacy"
            assert client.post("/v1/memories", headers=owner, json=req).status_code == 201, (
                "Owner reassertion"
            )
            assert (
                client.delete(f"/v1/credentials/{issued.json()['id']}", headers=owner).status_code
                == 200
            ), "Revocation"
            assert client.get("/v1/scopes", headers=agent).status_code == 401, (
                "Immediate revocation"
            )
        print(
            "Phase 1 smoke passed: real CLI, HTTP lifecycle, retry, conflict, "
            "scope access, forget and revocation."
        )
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        if test_engine is not None:
            test_engine.dispose()
        if database_created:
            with admin_engine.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin_engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-url", default="postgresql+psycopg://postgres:postgres@localhost:5433/postgres"
    )
    args = parser.parse_args()
    try:
        run(args.server_url)
    except Exception as exc:
        # Do not print driver errors or subprocess output: they may contain test tokens.
        print(
            f"Phase 1 smoke failed ({type(exc).__name__}); inspect the failed stage locally.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
