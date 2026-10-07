"""Safe failures under real database unavailability and unexpected service errors."""

import json
import socket
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from memory_platform.api.app import create_app
from memory_platform.api.routes import memories as routes
from memory_platform.config import Settings
from memory_platform.db import make_engine
from memory_platform.tables import activity
from tests.conftest import WorkspaceInfo


def test_unreachable_database_returns_safe_503(
    settings: Settings, workspace: WorkspaceInfo
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        engine = make_engine(
            f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres?connect_timeout=1"
        )
        try:
            app = create_app(settings=settings, engine=engine)
            with TestClient(app) as client:
                response = client.get(
                    "/v1/memories",
                    headers={
                        "Authorization": f"Bearer {workspace.owner_token}",
                        "X-Request-ID": "db-down",
                    },
                )
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "dependency_unavailable"
                assert (
                    response.json()["error"]["request_id"]
                    == response.headers["X-Request-ID"]
                    == "db-down"
                )
                assert (
                    "postgres" not in response.text and workspace.owner_token not in response.text
                )
                assert client.get("/health").status_code == 200
        finally:
            engine.dispose()


def test_unexpected_error_is_generic_and_audited(
    monkeypatch: pytest.MonkeyPatch,
    owner_client: TestClient,
    workspace: WorkspaceInfo,
    engine: Engine,
) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET_PAYLOAD password database-url")

    monkeypatch.setattr(routes, "get_memory", fail)
    response = owner_client.get(
        f"/v1/memories/{uuid4()}", headers={"X-Request-ID": "internal-failure"}
    )
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
    assert "SECRET_PAYLOAD" not in response.text and "password" not in response.text
    assert response.headers["X-Request-ID"] == "internal-failure"
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(activity).where(
                    activity.c.request_id == "internal-failure",
                    activity.c.workspace_id == workspace.id,
                )
            )
            .mappings()
            .one()
        )
        assert row["result"] == "internal_error" and "SECRET_PAYLOAD" not in str(dict(row))


def test_exact_body_limit_and_wrong_method_envelope(
    owner_client: TestClient, workspace: WorkspaceInfo
) -> None:
    body = json.dumps(
        {"scope_id": str(workspace.personal_scope_id), "type": "fact", "content": "at byte limit"}
    ).encode()
    padded = body + b" " * (65536 - len(body))
    response = owner_client.post(
        "/v1/memories", content=padded, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 201
    response = owner_client.put("/health", headers={"X-Request-ID": "wrong-method"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "bad_request"
    assert response.headers["X-Request-ID"] == "wrong-method"


def test_invalid_idempotency_header_and_optional_routes(
    app: FastAPI, owner_client: TestClient, workspace: WorkspaceInfo
) -> None:
    response = owner_client.post(
        "/v1/memories",
        json={"scope_id": str(workspace.personal_scope_id), "type": "fact", "content": "private"},
        headers={"Idempotency-Key": "secret invalid!"},
    )
    assert response.status_code == 422 and "secret invalid!" not in response.text
    for path in ["/docs", "/openapi.json", "/health/"]:
        assert owner_client.get(path, follow_redirects=False).status_code == 404
