"""FastAPI factory with bounded requests, safe errors and content-free audit metadata."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError, TimeoutError
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from memory_platform.api.routes import credentials, memories, scopes
from memory_platform.api.schemas import (
    REQUEST_ID_HEADER,
    REQUEST_ID_PATTERN,
    ErrorBody,
    ErrorEnvelope,
    HealthResponse,
)
from memory_platform.auth.principal import resolve_principal
from memory_platform.config import Settings, get_settings
from memory_platform.db import make_engine
from memory_platform.enums import ActorKind
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.idempotency import record_activity


def _response(request: Request, error: AppError) -> JSONResponse:
    request_id = request.state.request_id
    headers = {REQUEST_ID_HEADER: request_id}
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    body = ErrorEnvelope(
        error=ErrorBody(
            code=error.code,
            message=error.message,
            request_id=request_id,
            details=error.details,
            retry_after=error.retry_after,
        )
    )
    return JSONResponse(
        body.model_dump(mode="json"), status_code=error.http_status, headers=headers
    )


def _audit_error(request: Request, error: AppError) -> None:
    principal = getattr(request.state, "principal", None)
    if principal is None or error.code == ErrorCode.dependency_unavailable:
        return
    route = request.scope.get("route")
    # Use the route template, never client query strings or payloads.
    action = f"http.{request.method.lower()}:{getattr(route, 'path', 'unknown')}"
    try:
        with request.app.state.engine.begin() as conn:
            record_activity(
                conn,
                workspace_id=principal.workspace_id,
                actor_id=principal.actor_id,
                action=action,
                target_ids=(),
                scope_id=None,
                request_id=request.state.request_id,
                result=error.code.value,
                latency_ms=int((monotonic() - request.state.started_at) * 1000),
            )
    except Exception:
        # An audit failure must not change the original safe HTTP result.
        pass


class RequestBoundary:
    """Bound incoming bytes before JSON parsing and attach a request id to every response."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int, engine: Engine) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.engine = engine

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        provided = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = (
            provided if provided and re.fullmatch(REQUEST_ID_PATTERN, provided) else uuid4().hex
        )
        scope.setdefault("state", {}).update(request_id=request_id, started_at=monotonic())
        request = Request(scope)
        started = False

        async def send_with_id(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            limit = self.max_body_bytes
            path = scope.get("path", "")
            large_skill = scope.get("method") == "POST" and path == "/v1/skills"
            large_source = (scope.get("method") == "POST" and path == "/v1/sources") or (
                scope.get("method") == "PUT" and re.fullmatch(r"/v1/sources/[0-9a-fA-F-]{36}", path)
            )
            if large_skill or large_source:
                # Authenticate before reading a potentially large package. No uploaded
                # skill bytes are ever buffered from an agent credential.
                authorization = Headers(scope=scope).get("authorization", "")
                match = re.fullmatch(r"(?i:Bearer) (mem_[A-Za-z0-9_-]+)", authorization)
                if match is None:
                    raise AppError(ErrorCode.unauthenticated, "Invalid credentials.")
                with self.engine.begin() as conn:
                    principal = resolve_principal(conn, match.group(1))
                request.state.principal = principal
                if path == "/v1/skills":
                    if principal.actor_kind != ActorKind.owner or not principal.is_admin:
                        raise AppError(ErrorCode.forbidden, "Owner approval required.")
                    limit = 10 * 1024 * 1024
                else:
                    limit = 30 * 1024 * 1024
            declared = Headers(scope=scope).get("content-length")
            if declared and declared.isdecimal() and int(declared) > limit:
                raise AppError(ErrorCode.payload_too_large, "Request body is too large.")
            chunks: list[bytes] = []
            total = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                total += len(chunk)
                if total > limit:
                    raise AppError(ErrorCode.payload_too_large, "Request body is too large.")
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            delivered = False

            async def receive_body() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, receive_body, send_with_id)
        except Exception as exc:
            if started:
                raise
            if isinstance(exc, AppError):
                error = exc
            elif isinstance(exc, OperationalError | TimeoutError):
                error = AppError(ErrorCode.dependency_unavailable, "Database is unavailable.")
            else:
                error = AppError(ErrorCode.internal_error, "An unexpected error occurred.")
            _audit_error(request, error)
            await _response(request, error)(scope, receive, send_with_id)


def create_app(*, settings: Settings, engine: Engine) -> FastAPI:
    app = FastAPI(
        title="Agent Memory Platform",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.add_middleware(RequestBoundary, max_body_bytes=settings.max_body_bytes, engine=engine)

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        _audit_error(request, exc)
        return _response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's input/context can contain memory text, tokens or decoder internals.
        errors = [
            {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
            for item in exc.errors()
        ]
        error = AppError(ErrorCode.validation_error, "Invalid request.", details={"errors": errors})
        return await app_error(request, error)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        error = (
            AppError(ErrorCode.not_found, "Not found.")
            if exc.status_code == 404
            else AppError(ErrorCode.bad_request, "Request method is not allowed.")
        )
        return await app_error(request, error)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/ready")
    def ready(request: Request) -> dict[str, Any]:
        """Authenticated readiness: migration head + durable queue depth.

        Returns 503 when the database is unreachable or migrations are behind.
        Queue counts are scoped to the caller's workspace; no content is exposed.
        """
        from sqlalchemy import func, select, text

        from memory_platform.api.deps import get_principal
        from memory_platform.knowledge_tables import knowledge_jobs

        principal = get_principal(request, request.headers.get("authorization"))
        engine = request.app.state.engine
        try:
            with engine.begin() as conn:
                revision = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                rows = (
                    conn.execute(
                        select(knowledge_jobs.c.status, func.count())
                        .where(knowledge_jobs.c.workspace_id == principal.workspace_id)
                        .group_by(knowledge_jobs.c.status)
                    )
                    .mappings()
                    .all()
                )
        except Exception:
            raise AppError(ErrorCode.dependency_unavailable, "Database is unavailable.") from None
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from memory_platform.config import REPO_ROOT

        cfg = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
        head = ScriptDirectory.from_config(cfg).get_current_head()
        counts = {row["status"]: row["count"] for row in rows}
        pending = sum(counts.get(status, 0) for status in ("queued", "leased", "retry_wait"))
        degraded = revision != head
        return {
            "status": "degraded" if degraded else "ok",
            "migration_revision": revision,
            "migration_head": head,
            "migration_current": not degraded,
            "pending_jobs": pending,
            "jobs": counts,
        }

    app.include_router(memories.router)
    app.include_router(scopes.router)
    app.include_router(credentials.router)
    from memory_platform.api.routes import (
        capture,
        dashboard,
        graph,
        knowledge,
        operations,
        skills,
        tasks,
    )

    app.include_router(skills.router)
    app.include_router(tasks.router)
    app.include_router(knowledge.router)
    app.include_router(dashboard.router)
    app.include_router(capture.router)
    app.include_router(graph.router)
    app.include_router(operations.router)
    return app


def create_default_app() -> FastAPI:
    """Uvicorn --factory entry point; engine ownership remains explicit in tests."""
    settings = get_settings()
    engine = make_engine(settings.database_url, prepare_threshold=settings.db_prepare_threshold)
    app = create_app(settings=settings, engine=engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.dispose()

    app.router.lifespan_context = lifespan
    return app
