"""Streamable HTTP MCP uses per-request scoped credentials, never the owner's."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update

from memory_platform.config import REPO_ROOT
from memory_platform.tables import credentials, memories


def test_hosted_mcp_writes_and_credentials(settings, workspace, make_agent_client, engine):
    """One API process serves REST and MCP, including its SDK lifespan."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    scope = str(workspace.personal_scope_id)
    first = make_agent_client([workspace.personal_scope_id], ["memory:read", "memory:write"])
    second = make_agent_client([workspace.personal_scope_id], ["memory:read", "memory:write"])
    stranger = make_agent_client([], [])
    expired = make_agent_client([workspace.personal_scope_id], ["memory:read"])
    revoked = make_agent_client([workspace.personal_scope_id], ["memory:read"])
    with engine.begin() as conn:
        conn.execute(
            update(credentials)
            .where(credentials.c.id == expired.credential_id)
            .values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )
        conn.execute(
            update(credentials)
            .where(credentials.c.id == revoked.credential_id)
            .values(revoked_at=datetime.now(UTC))
        )
    env = dict(os.environ) | {
        "DATABASE_URL": settings.database_url,
        "MEMORY_HMAC_KEY": settings.memory_hmac_key.get_secret_value(),
        "MEMORY_API_URL": url,
        "PROVIDER_DAILY_BUDGET_USD": "0",
        "PROVIDER_API_KEY": "",
        "REDIS_REST_URL": "",
        "REDIS_REST_TOKEN": "",
    }
    process = _start("memory_platform.api.app:create_default_app", port, env)
    try:
        for token in [None, "mem_invalid", expired.token, revoked.token]:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            response = httpx.post(url + "/mcp", json={}, headers=headers, follow_redirects=False)
            assert response.status_code == 401
        marker = "hosted-agent-" + uuid.uuid4().hex

        async def exercise(token, denied=False):
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            async with (
                httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=20) as http,
                streamable_http_client(url + "/mcp", http_client=http) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"memory_remember", "memory_recall", "memory_update"} <= names
                args = {
                    "scope_id": scope,
                    "content": marker + token[-5:],
                    "type": "fact",
                    "idempotency_key": str(uuid.uuid4()),
                }
                created = await session.call_tool("memory_remember", args)
                if denied:
                    assert created.is_error
                    return
                assert not created.is_error, str(created)
                body = json.loads(created.content[0].text)
                replay = await session.call_tool("memory_remember", args)
                assert not replay.is_error
                replay_body = json.loads(replay.content[0].text)
                assert replay_body["id"] == body["id"] and replay_body["replayed"]
                fetched = await session.call_tool("memory_get", {"memory_id": body["id"]})
                assert not fetched.is_error and args["content"] in str(fetched)
                changed = await session.call_tool(
                    "memory_update",
                    {
                        "memory_id": body["id"],
                        "changes": {"expected_version": 1, "content": args["content"] + " updated"},
                    },
                )
                assert not changed.is_error
                conflict = await session.call_tool(
                    "memory_update",
                    {
                        "memory_id": body["id"],
                        "changes": {"expected_version": 1, "content": "stale"},
                    },
                )
                assert conflict.is_error and "version_conflict" in str(conflict)
                recall = await session.call_tool(
                    "memory_recall", {"query": marker, "scope_ids": [scope]}
                )
                assert not recall.is_error and marker in str(recall)
                deleted = await session.call_tool(
                    "memory_forget", {"memory_id": body["id"], "expected_version": 2}
                )
                assert deleted.is_error
                assert "forbidden" in str(deleted)

        async def concurrent():
            await asyncio.gather(
                exercise(first.token), exercise(second.token), exercise(stranger.token, denied=True)
            )

        asyncio.run(concurrent())
        with engine.connect() as conn:
            authors = set(
                conn.scalars(
                    select(memories.c.created_by).where(memories.c.content.like(marker + "%"))
                )
            )
            assert authors == {first.actor_id, second.actor_id}
    finally:
        process.terminate()
        process.wait(timeout=5)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start(target: str, port: int, env: dict[str, str]) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            target,
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=env,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    is_api = "api.app" in target
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if is_api:
                if httpx.get(url + "/health").status_code == 200:
                    return process
            elif socket.create_connection(("127.0.0.1", port), timeout=1):
                return process
        except (httpx.TransportError, OSError):
            pass
        time.sleep(0.1)
    process.terminate()
    raise AssertionError("Disposable server failed to start.")


def test_http_bridge_enforces_caller_credentials(settings, workspace, make_agent_client):
    api_port = _free_port()
    mcp_port = _free_port()
    api_env = dict(os.environ) | {
        "DATABASE_URL": settings.database_url,
        "MEMORY_HMAC_KEY": settings.memory_hmac_key.get_secret_value(),
        "PROVIDER_DAILY_BUDGET_USD": "0",
        "PROVIDER_API_KEY": "",
        "REDIS_REST_URL": "",
        "REDIS_REST_TOKEN": "",
    }
    api = _start("memory_platform.api.app:create_default_app", api_port, api_env)
    try:
        api_url = f"http://127.0.0.1:{api_port}"
        mcp_env = dict(os.environ) | {"MEMORY_API_URL": api_url}
        mcp = _start("memory_platform.mcp_server:create_http_app", mcp_port, mcp_env)
        try:
            mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
            # No credential at all is rejected before any protocol handling.
            assert httpx.post(mcp_url, json={}).status_code == 401
            assert (
                httpx.post(
                    mcp_url, json={}, headers={"Authorization": "Bearer mem_invalid"}
                ).status_code
                == 401
            )
            marker = "http-bridge-marker-" + uuid.uuid4().hex
            owner_headers = {"Authorization": f"Bearer {workspace.owner_token}"}
            created = httpx.post(
                api_url + "/v1/memories",
                json={
                    "scope_id": str(workspace.personal_scope_id),
                    "type": "fact",
                    "content": marker,
                },
                headers={**owner_headers, "Idempotency-Key": "mcp-http-" + uuid.uuid4().hex},
            )
            assert created.status_code == 201, created.text
            reader = make_agent_client([workspace.personal_scope_id], ["memory:read"])
            stranger = make_agent_client([], [])
            direct = httpx.post(
                api_url + "/v1/recall",
                json={"query": marker, "scope_ids": [str(workspace.personal_scope_id)]},
                headers={"Authorization": f"Bearer {reader.token}"},
            )
            assert direct.status_code == 200, direct.text
            assert marker in direct.text

            async def recall(token: str) -> str:
                import httpx as httpx_async
                from mcp.client.session import ClientSession
                from mcp.client.streamable_http import streamable_http_client

                async with (
                    httpx_async.AsyncClient(
                        headers={"Authorization": f"Bearer {token}"}, timeout=10
                    ) as http,
                    streamable_http_client(mcp_url, http_client=http) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    assert {
                        "memory_recall",
                        "memory_remember",
                        "memory_ingest",
                        "memory_record_event",
                        "memory_link",
                        "memory_forget",
                    } <= names
                    result = await session.call_tool(
                        "memory_recall",
                        {
                            "query": marker,
                            "scope_ids": [str(workspace.personal_scope_id)],
                        },
                    )
                    return str(result)

            async def recall_text(token: str) -> str:
                text = await recall(token)
                assert "Error executing tool" not in text, text
                return text

            async def recall_denied(token: str) -> None:
                text = await recall(token)
                assert "code=not_found" in text, text

            assert marker in asyncio.run(recall_text(reader.token))
            asyncio.run(recall_denied(stranger.token))
        finally:
            mcp.terminate()
            mcp.wait(timeout=5)
    finally:
        api.terminate()
        api.wait(timeout=5)
