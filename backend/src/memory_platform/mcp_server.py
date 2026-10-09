"""Official SDK stdio and local Streamable HTTP bridges to the same authorized API."""

import os
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from memory_platform.client import APIClient, ClientError

_request_token: ContextVar[str | None] = ContextVar("memory_request_token", default=None)


def create_server(*, http: bool = False, api_url: str | None = None) -> MCPServer:
    server = MCPServer(
        "Memory Platform",
        instructions=(
            "Use scoped project context. Skill lookup returns owner-approved packages; "
            "retrieving a skill does not execute it."
        ),
    )

    def call(
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        token = _request_token.get() if http else os.environ.get("MEMORY_API_TOKEN")
        if not token:
            raise ValueError("A scoped Memory API credential is required.")
        with APIClient(
            api_url or os.environ.get("MEMORY_API_URL", "http://127.0.0.1:8000"), token
        ) as client:
            try:
                return client.request(
                    method, path, payload, params=params, idempotency_key=idempotency_key
                )
            except ClientError as exc:
                # ToolError is intentionally visible to agents; arbitrary exceptions are hidden.
                delay = f"; retry_after={exc.retry_after}" if exc.retry_after else ""
                raise ToolError(
                    f"Memory API error: status={exc.status}; code={exc.code}{delay}"
                ) from None

    @server.tool()
    def memory_recall(
        query: str,
        scope_ids: list[str],
        limit: int = 8,
        token_budget: int = 3000,
        session_id: str | None = None,
        semantic: bool = True,
    ) -> dict[str, Any]:
        """Retrieve current authorized evidence and bounded context."""
        body: dict[str, Any] = {
            "query": query,
            "scope_ids": scope_ids,
            "limit": limit,
            "token_budget": token_budget,
            "semantic": semantic,
        }
        if session_id is not None:
            body["session_id"] = session_id
        return call("POST", "/v1/recall", body)

    @server.tool()
    def memory_remember(
        scope_id: str, content: str, type: str = "experience", idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Record explicit memory using existing trust and suppression rules."""
        return call(
            "POST",
            "/v1/memories",
            {"scope_id": scope_id, "content": content, "type": type},
            idempotency_key=idempotency_key or str(uuid4()),
        )

    @server.tool()
    def memory_get(memory_id: str) -> dict[str, Any]:
        """Read one authorized memory including its history/evidence."""
        return call("GET", f"/v1/memories/{memory_id}")

    @server.tool()
    def memory_update(
        memory_id: str, changes: dict[str, Any], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Update with the expected version in changes; conflicts are returned."""
        return call(
            "PATCH",
            f"/v1/memories/{memory_id}",
            changes,
            idempotency_key=idempotency_key or str(uuid4()),
        )

    @server.tool()
    def memory_forget(
        memory_id: str, expected_version: int, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Explicitly forget a memory under the caller's delete grant."""
        return call(
            "DELETE",
            f"/v1/memories/{memory_id}",
            {"expected_version": expected_version},
            idempotency_key=idempotency_key or str(uuid4()),
        )

    @server.tool()
    def memory_ingest(
        scope_id: str,
        title: str,
        filename: str,
        content_base64: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Ingest inline document text as a versioned source under the caller's ingest grant."""
        return call(
            "POST",
            "/v1/sources",
            {
                "scope_id": scope_id,
                "title": title,
                "filename": filename,
                "content_base64": content_base64,
            },
            idempotency_key=idempotency_key or str(uuid4()),
        )

    @server.tool()
    def memory_record_event(session_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Append one explicitly selected message or tool result to the caller's session."""
        return call("POST", f"/v1/sessions/{session_id}/events", event)

    @server.tool()
    def memory_link(from_id: str, to_id: str) -> dict[str, Any]:
        """Add an owner asserted typed relationship between two authorized memories."""
        return call("POST", "/v1/relations", {"from_id": from_id, "to_id": to_id})

    @server.tool()
    def skill_resolve(scope_id: str, command: str) -> dict[str, Any]:
        """Fetch an approved skill by exact command, for example /github_review."""
        return call("GET", "/v1/skills/resolve", params={"scope_id": scope_id, "command": command})

    @server.tool()
    def task_list(scope_id: str) -> dict[str, Any]:
        """Read shared task checkpoints for an authorized project."""
        return call("GET", "/v1/tasks", params={"scope_id": scope_id})

    @server.tool()
    def session_start(scope_id: str, client: str, external_id: str) -> dict[str, Any]:
        """Register the current client session; actor identity comes from its credential."""
        return call(
            "POST",
            "/v1/sessions",
            {"scope_id": scope_id, "client": client, "external_id": external_id},
        )

    @server.tool()
    def session_heartbeat(session_id: str) -> dict[str, Any]:
        """Update session liveness without completing any task."""
        return call("POST", f"/v1/sessions/{session_id}/heartbeat", {})

    @server.tool()
    def task_create(task: dict[str, Any]) -> dict[str, Any]:
        """Create a task with scope/session/stable event ID/sequence/title/goal."""
        return call("POST", "/v1/tasks", task)

    @server.tool()
    def task_checkpoint(task_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
        """Publish concise progress with expected_version, generation, event_id and sequence."""
        return call("PATCH", f"/v1/tasks/{task_id}/checkpoint", checkpoint)

    @server.tool()
    def task_handoff(task_id: str, handoff: dict[str, Any]) -> dict[str, Any]:
        """Offer an explicit handoff to target_session_id with the current version/generation."""
        return call("POST", f"/v1/tasks/{task_id}/handoff", handoff)

    @server.tool()
    def task_accept_handoff(task_id: str, acceptance: dict[str, Any]) -> dict[str, Any]:
        """Accept an offered handoff; previous ownership is fenced out."""
        return call("POST", f"/v1/tasks/{task_id}/accept-handoff", acceptance)

    return server


class AuthenticatedMCP:
    """Local bridge: each HTTP request forwards its own scoped credential, never the owner's."""

    def __init__(self, app: Any, api_url: str) -> None:
        self.app = app
        self.api_url = api_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authorization = Headers(scope=scope).get("authorization", "")
        token = authorization.removeprefix("Bearer ")
        if token == authorization or not (token.startswith("mem_") or token.startswith("mcp_at_")):
            await JSONResponse({"error": "Scoped bearer credential required."}, status_code=401)(
                scope, receive, send
            )
            return
        # Verify credentials even for initialize/list requests. Do not expose tools
        # through an unauthenticated protocol handshake.
        import asyncio

        try:

            def verify() -> None:
                with APIClient(self.api_url, token) as client:
                    client.request("GET", "/v1/scopes")

            await asyncio.to_thread(verify)
        except ClientError as exc:
            await JSONResponse(
                {"error": "Credential rejected."},
                status_code=exc.status,
                headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None,
            )(scope, receive, send)
            return
        except Exception:
            await JSONResponse({"error": "Memory API unavailable."}, status_code=503)(
                scope, receive, send
            )
            return
        marker = _request_token.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_token.reset(marker)


def create_http_app(*, api_url: str | None = None) -> AuthenticatedMCP:
    api_url = api_url or os.environ.get("MEMORY_API_URL", "http://127.0.0.1:8000")
    # Validate the forwarding origin before accepting requests. Never derive it from Host.
    with APIClient(api_url, "mem_validation_only"):
        pass
    origin = urlsplit(api_url)
    server = create_server(http=True, api_url=api_url)
    return AuthenticatedMCP(
        server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            max_request_body_size=65536,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[origin.netloc]
                + (
                    ["127.0.0.1:*", "localhost:*", "[::1]:*"]
                    if origin.hostname in {"127.0.0.1", "localhost", "::1"}
                    else []
                ),
                allowed_origins=[f"{origin.scheme}://{origin.netloc}"],
            ),
        ),
        api_url,
    )


def main() -> None:
    create_server().run("stdio")


if __name__ == "__main__":
    main()
