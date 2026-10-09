"""Protocol and retry contracts for the hosted agent setup."""

import asyncio

import pytest

from memory_platform import mcp_server


def test_api_errors_keep_safe_code_and_retry_after(monkeypatch):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, *args, **kwargs):
            raise mcp_server.ClientError(429, "rate_limited", 12)

    monkeypatch.setattr(mcp_server, "APIClient", Client)
    monkeypatch.setenv("MEMORY_API_TOKEN", "mem_test_not_a_real_credential")
    with pytest.raises(mcp_server.ToolError, match=r"rate_limited.*12"):
        asyncio.run(mcp_server.create_server().call_tool("memory_get", {"memory_id": "test"}))


def test_memory_writes_forward_retry_keys(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, path, payload, **kwargs):
            calls.append((method, path, kwargs.get("idempotency_key")))
            return {"ok": True}

    monkeypatch.setattr(mcp_server, "APIClient", Client)
    monkeypatch.setenv("MEMORY_API_TOKEN", "mem_test_not_a_real_credential")
    server = mcp_server.create_server()

    async def write():
        for name, arguments in [
            ("memory_remember", {"scope_id": "scope", "content": "fact"}),
            ("memory_update", {"memory_id": "memory", "changes": {"expected_version": 1}}),
            ("memory_forget", {"memory_id": "memory", "expected_version": 1}),
        ]:
            await server.call_tool(name, {**arguments, "idempotency_key": "stable-retry-key"})
        await server.call_tool("memory_remember", {"scope_id": "scope", "content": "other"})

    asyncio.run(write())
    assert [call[2] for call in calls[:3]] == ["stable-retry-key"] * 3
    assert len(calls) == 4 and calls[3][2]
