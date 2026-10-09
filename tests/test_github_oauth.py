"""GitHub owner sign-in: login redirect, allowlisted callback, guards."""

from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine

from memory_platform.api.app import create_app
from memory_platform.config import Settings
from tests.conftest import TEST_HMAC_KEY_HEX, WorkspaceInfo

DASHBOARD_PAGE = "http://127.0.0.1:3000/oauth/authorize"


@pytest.fixture
def github_settings(test_db_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=test_db_url,
        migration_database_url=None,
        test_database_url=None,
        memory_hmac_key=SecretStr(TEST_HMAC_KEY_HEX),
        github_client_id="test-github-id",
        github_client_secret=SecretStr("test-github-secret"),
        owner_email="Owner@Example.com",
        oauth_authorize_url=DASHBOARD_PAGE,
    )


@pytest.fixture
def github_app(github_settings: Settings, engine: Engine) -> FastAPI:
    return create_app(settings=github_settings, engine=engine)


class _FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


class _FakeHttp:
    """Stands in for httpx.Client. Class attributes set the canned replies."""

    token_body: ClassVar[Any] = {"access_token": "gho_test"}
    emails_body: ClassVar[Any] = []
    emails_status: ClassVar[int] = 200

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_FakeHttp":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def post(
        self, url: str, headers: dict[str, str] | None = None, data: Any = None
    ) -> _FakeResponse:
        assert "github.com/login/oauth/access_token" in url
        return _FakeResponse(200, dict(_FakeHttp.token_body))

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        assert "api.github.com/user/emails" in url
        assert headers is not None and headers.get("Authorization") == "Bearer gho_test"
        return _FakeResponse(_FakeHttp.emails_status, list(_FakeHttp.emails_body))


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeHttp)
    _FakeHttp.token_body = {"access_token": "gho_test"}
    _FakeHttp.emails_body = []
    _FakeHttp.emails_status = 200


def _login_state(github_app: FastAPI, next_url: str = DASHBOARD_PAGE) -> str:
    anon = TestClient(github_app, follow_redirects=False)
    response = anon.get("/oauth/github/login", params={"next": next_url})
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize")
    query = parse_qs(urlsplit(location).query)
    assert query["client_id"] == ["test-github-id"]
    return query["state"][0]


def test_config_reports_login_options(github_app: FastAPI, app: FastAPI) -> None:
    configured = TestClient(github_app).get("/oauth/config")
    assert configured.status_code == 200
    assert configured.json() == {"github": True, "google": False, "owner_email_set": True}
    plain = TestClient(app).get("/oauth/config")
    assert plain.status_code == 200
    assert plain.json()["github"] is False


def test_login_guards_next_url(github_app: FastAPI, app: FastAPI) -> None:
    evil = TestClient(github_app, follow_redirects=False).get(
        "/oauth/github/login", params={"next": "https://evil.test/steal"}
    )
    assert evil.status_code == 400
    missing = TestClient(app, follow_redirects=False).get("/oauth/github/login")
    assert missing.status_code == 400


def test_callback_signs_in_owner(
    github_app: FastAPI, workspace: WorkspaceInfo, fake_github: None
) -> None:
    _FakeHttp.emails_body = [
        {"email": "other@example.com", "primary": False, "verified": True},
        {"email": "owner@example.com", "primary": True, "verified": True},
    ]
    state = _login_state(github_app)
    anon = TestClient(github_app, follow_redirects=False)
    callback = anon.get("/oauth/github/callback", params={"code": "gh-code", "state": state})
    assert callback.status_code == 302, callback.text
    parts = urlsplit(callback.headers["location"])
    assert parts.netloc == "127.0.0.1:3000"
    assert parts.path == "/oauth/github/finish"
    token = parse_qs(parts.fragment)["token"][0]
    assert token.startswith("mem_")

    owner = TestClient(github_app, headers={"Authorization": f"Bearer {token}"})
    summary = owner.get("/v1/dashboard/summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["is_admin"] is True
    scopes = owner.get("/v1/scopes")
    assert scopes.status_code == 200
    # Production holds one workspace, so the minted credential covers it. The
    # test database holds many workspaces side by side, so read the minted
    # credential's own scope back instead of assuming the fixture workspace.
    items = scopes.json()["items"]
    assert items
    scope_id = items[0]["id"]
    remember = owner.post(
        "/v1/memories",
        json={
            "scope_id": scope_id,
            "type": "fact",
            "content": "GitHub sign-in works",
        },
    )
    assert remember.status_code == 201, remember.text

    # The login state is single-use.
    replay = anon.get("/oauth/github/callback", params={"code": "gh-code", "state": state})
    assert replay.status_code == 400


def test_callback_rejects_foreign_email(
    github_app: FastAPI, workspace: WorkspaceInfo, fake_github: None
) -> None:
    _ = workspace
    _FakeHttp.emails_body = [{"email": "stranger@example.com", "primary": True, "verified": True}]
    state = _login_state(github_app)
    anon = TestClient(github_app, follow_redirects=False)
    denied = anon.get("/oauth/github/callback", params={"code": "gh-code", "state": state})
    assert denied.status_code == 403


def test_callback_rejects_unverified_email(
    github_app: FastAPI, workspace: WorkspaceInfo, fake_github: None
) -> None:
    _ = workspace
    _FakeHttp.emails_body = [{"email": "owner@example.com", "primary": True, "verified": False}]
    state = _login_state(github_app)
    anon = TestClient(github_app, follow_redirects=False)
    denied = anon.get("/oauth/github/callback", params={"code": "gh-code", "state": state})
    assert denied.status_code == 403
