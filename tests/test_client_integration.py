"""Real Uvicorn + Python/CLI/MCP transports on the disposable pytest database."""

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest

from memory_platform.api.skill_schemas import SkillFile
from memory_platform.client import APIClient
from memory_platform.config import REPO_ROOT
from memory_platform.hooks import context
from memory_platform.skill_package import package_hash


@pytest.fixture
def live_api(settings, workspace):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ) | {
        "DATABASE_URL": settings.database_url,
        "MEMORY_HMAC_KEY": settings.memory_hmac_key.get_secret_value(),
        "PROVIDER_DAILY_BUDGET_USD": "0",
        "PROVIDER_API_KEY": "",
        "REDIS_REST_URL": "",
        "REDIS_REST_TOKEN": "",
    }
    process = subprocess.Popen(
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
        env=env,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(url + "/health").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError("Disposable API failed to start.")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_two_client_checkpoints_cli_and_mcp_equivalence(live_api, workspace, make_agent_client):
    scope = str(workspace.personal_scope_id)
    capabilities = ["task:read", "task:write", "skill:read"]
    codex_token = make_agent_client([workspace.personal_scope_id], capabilities).token
    claude_token = make_agent_client([workspace.personal_scope_id], capabilities).token
    opencode_token = make_agent_client([workspace.personal_scope_id], capabilities).token
    marker = "verified-checkpoint-" + uuid4().hex
    with APIClient(live_api, codex_token) as codex:
        session = codex.request(
            "POST",
            "/v1/sessions",
            {"scope_id": scope, "client": "codex", "external_id": uuid4().hex},
        )
        task = codex.request(
            "POST",
            "/v1/tasks",
            {
                "scope_id": scope,
                "session_id": session["id"],
                "event_id": str(uuid4()),
                "sequence": 1,
                "title": "Shared integration check",
                "goal": "Verify second client gets checkpoint",
            },
        )
        codex.request(
            "PATCH",
            f"/v1/tasks/{task['task_id']}/checkpoint",
            {
                "session_id": session["id"],
                "event_id": str(uuid4()),
                "sequence": 2,
                "expected_version": 1,
                "generation": 1,
                "status": "running",
                "summary": marker,
                "next_step": "Read this checkpoint in the other client",
            },
        )
        files = [
            SkillFile(
                path="SKILL.md",
                content_base64=base64.b64encode(b"owner approved test skill\r\n").decode(),
            )
        ]
    with APIClient(live_api, workspace.owner_token) as owner:
        stored = owner.request(
            "POST",
            "/v1/skills",
            {
                "scope_id": scope,
                "command": "/github_review",
                "files": [f.model_dump() for f in files],
                "approved_package_hash": package_hash(files),
            },
        )
    with APIClient(live_api, claude_token) as claude:
        injected = context(claude, scope, external_id=uuid4().hex, client_name="claude")
        assert marker in injected and "Read this checkpoint" in injected
        assert (
            claude.resolve_skill(scope, "/github_review")["package_hash"] == stored["package_hash"]
        )
    with APIClient(live_api, opencode_token) as opencode:
        injected = context(opencode, scope, external_id=uuid4().hex, client_name="opencode")
        assert marker in injected and "Read this checkpoint" in injected
        assert (
            opencode.resolve_skill(scope, "/github_review")["package_hash"]
            == stored["package_hash"]
        )
    env = dict(os.environ) | {
        "MEMORY_API_URL": live_api,
        "MEMORY_API_TOKEN": claude_token,
        "MEMORY_SCOPE_ID": scope,
    }
    cli = subprocess.run(
        [sys.executable, "-m", "memory_platform.cli", "tasks", "--scope", scope],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert cli.returncode == 0 and marker in cli.stdout
    resolved = subprocess.run(
        [
            sys.executable,
            "-m",
            "memory_platform.cli",
            "skill-resolve",
            "/github_review",
            "--scope",
            scope,
        ],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert resolved.returncode == 0
    assert json.loads(resolved.stdout)["package_hash"] == stored["package_hash"]

    private_prompt = "Prompt contents must never enter shared checkpoint context"
    for name, token in (
        ("codex", codex_token),
        ("claude", claude_token),
        ("opencode", opencode_token),
    ):
        external = uuid4().hex
        for event_name in ("SessionStart", "UserPromptSubmit"):
            hook = subprocess.run(
                [sys.executable, "-m", "memory_platform.hooks"],
                env=env | {"MEMORY_CLIENT_NAME": name, "MEMORY_API_TOKEN": token},
                input=json.dumps(
                    {
                        "session_id": external,
                        "hook_event_name": event_name,
                        "prompt": private_prompt,
                    }
                ),
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert hook.returncode == 0
            assert marker in hook.stdout and "Your Memory session ID:" in hook.stdout
            assert private_prompt not in hook.stdout

    async def stdio_check():
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "memory_platform.mcp_server"], env=env
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert any(t.name == "skill_resolve" for t in tools.tools)
            result = await client.call_tool(
                "skill_resolve", {"scope_id": scope, "command": "/github_review"}
            )
            assert not result.is_error
            assert stored["package_hash"] in str(result)
            checkpoints = await client.call_tool("task_list", {"scope_id": scope})
            assert marker in str(checkpoints)

    asyncio.run(stdio_check())
