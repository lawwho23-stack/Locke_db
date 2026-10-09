"""MCP OAuth bridge: discovery metadata, DCR, authorize, token, revoke, GitHub login.

Owner-only, no public signup. The authorize step requires an owner admin
bearer; it mints a per-device agent credential and binds a PKCE code to it.
GitHub sign-in mints a short-lived owner credential for the allowlisted email
so the dashboard approval page can log in with one click. GitHub tokens never
enter the database; only our own token hashes are stored.
"""

from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from memory_platform.api.deps import EngineDep, RequestIdDep, get_principal
from memory_platform.config import Settings
from memory_platform.enums import Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services import github_oauth
from memory_platform.services import oauth as oauth_service

router = APIRouter(tags=["oauth"])


def _issuer(settings: Settings) -> str:
    """Public origin for discovery documents; never derive from Host."""
    base = (settings.oauth_issuer or settings.memory_api_url or "http://127.0.0.1:8000").rstrip("/")
    return base


def _mcp_resource(settings: Settings) -> str:
    return f"{_issuer(settings)}/mcp"


_SCOPES_SUPPORTED = [cap.value for cap in Capability]


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_name: str = Field(min_length=1, max_length=100)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)


@router.get("/.well-known/oauth-protected-resource")
def protected_resource(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    return JSONResponse(
        {
            "resource": _mcp_resource(settings),
            "authorization_servers": [_issuer(settings)],
            "scopes_supported": _SCOPES_SUPPORTED,
            "bearer_methods_supported": ["header"],
            "resource_documentation": f"{_issuer(settings)}/",
        },
        headers={"Cache-Control": "no-store"},
    )


def _authorization_server_document(settings: Settings) -> dict[str, Any]:
    issuer = _issuer(settings)
    authorize_url = (settings.oauth_authorize_url or f"{issuer}/oauth/authorize").rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": authorize_url,
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "revocation_endpoint": f"{issuer}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": _SCOPES_SUPPORTED,
        "token_endpoint_auth_methods_supported": ["none"],
    }


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_meta(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    return JSONResponse(
        _authorization_server_document(settings), headers={"Cache-Control": "no-store"}
    )


@router.get("/oauth/.well-known/oauth-authorization-server")
def authorization_server_meta_alias(request: Request) -> JSONResponse:
    return authorization_server_meta(request)


@router.post("/oauth/register", status_code=201)
def register_client(req: RegisterRequest, engine: EngineDep) -> dict[str, Any]:
    with engine.begin() as conn:
        client_id = oauth_service.register_client(
            conn, client_name=req.client_name, redirect_uris=req.redirect_uris
        )
    return {
        "client_id": str(client_id),
        "client_name": req.client_name,
        "redirect_uris": req.redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


def _parse_requested_scope(
    scope: str | None, capabilities: str | None, scope_id: UUID | None
) -> tuple[UUID | None, list[Capability]]:
    """Combine the standard `scope` string with our explicit params."""
    tokens: list[str] = []
    if scope:
        tokens.extend(scope.split())
    if capabilities:
        tokens.extend(part.strip(",") for part in capabilities.replace(",", " ").split())
    found_scope: UUID | None = scope_id
    caps: list[str] = []
    for token in tokens:
        text = token.strip()
        if not text:
            continue
        if text.startswith("scope:"):
            try:
                found_scope = UUID(text.removeprefix("scope:"))
            except ValueError:
                raise AppError(ErrorCode.bad_request, "Invalid scope.") from None
        else:
            caps.append(text)
    if not caps:
        caps = ["memory:read"]
    return found_scope, oauth_service.parse_capabilities(caps)


@router.get("/oauth/authorize")
def authorize(
    request: Request,
    engine: EngineDep,
    request_id: RequestIdDep,
    response_type: str = Query(...),
    client_id: UUID = Query(...),
    redirect_uri: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(default="S256"),
    scope: str | None = Query(default=None),
    scope_id: UUID | None = Query(default=None),
    capabilities: str | None = Query(default=None),
    state: str | None = Query(default=None, max_length=2000),
    response_mode: str | None = Query(default=None),
) -> Any:
    """Owner approval. Success redirects to the client; `response_mode=json` returns JSON."""
    if response_type != "code":
        raise AppError(ErrorCode.bad_request, "Only response_type=code is supported.")
    if code_challenge_method != "S256":
        raise AppError(ErrorCode.bad_request, "Only code_challenge_method=S256 is supported.")
    if response_mode is not None and response_mode not in ("json", "redirect"):
        raise AppError(ErrorCode.bad_request, "Invalid response_mode.")
    principal = get_principal(request, request.headers.get("authorization"))
    settings: Settings = request.app.state.settings
    parsed_scope_id, caps = _parse_requested_scope(scope, capabilities, scope_id)
    if parsed_scope_id is None:
        raise AppError(ErrorCode.bad_request, "A scope_id is required.")
    with engine.begin() as conn:
        code = oauth_service.approve_authorize(
            conn,
            principal,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope_id=parsed_scope_id,
            capabilities=caps,
            code_challenge=code_challenge,
            code_ttl_seconds=settings.oauth_code_ttl_seconds,
            request_id=request_id,
        )
    if response_mode == "json":
        body: dict[str, Any] = {"code": code, "redirect_uri": redirect_uri}
        if state is not None:
            body["state"] = state
        return body
    location = (
        f"{redirect_uri}?code={code}" if "?" not in redirect_uri else f"{redirect_uri}&code={code}"
    )
    if state is not None:
        from urllib.parse import quote

        location += f"&state={quote(state, safe='')}"
    return RedirectResponse(url=location, status_code=302)


@router.post("/oauth/token")
async def token(request: Request, engine: EngineDep, request_id: RequestIdDep) -> dict[str, Any]:
    """Exchange a code or refresh token. Accepts JSON or OAuth form bodies."""
    settings: Settings = request.app.state.settings
    payload: dict[str, Any] = {}
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        payload = {str(k): str(v) for k, v in form.items()}
    else:
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            payload = {str(k): v for k, v in body.items()}
    grant_type = str(payload.get("grant_type", ""))
    if grant_type == "authorization_code":
        try:
            client_id = UUID(str(payload.get("client_id", "")))
        except ValueError:
            raise AppError(ErrorCode.bad_request, "Invalid grant.") from None
        code = str(payload.get("code", ""))
        verifier = str(payload.get("code_verifier", ""))
        redirect_uri = str(payload.get("redirect_uri", ""))
        if not code or not verifier or not redirect_uri:
            raise AppError(ErrorCode.bad_request, "Invalid grant.")
        with engine.begin() as conn:
            access, refresh, expires_in, scope_id, caps = oauth_service.exchange_code(
                conn,
                client_id=client_id,
                code=code,
                code_verifier=verifier,
                redirect_uri=redirect_uri,
                access_ttl_seconds=settings.oauth_access_ttl_seconds,
                refresh_ttl_days=settings.oauth_refresh_ttl_days,
                request_id=request_id,
            )
    elif grant_type == "refresh_token":
        try:
            client_id = UUID(str(payload.get("client_id", "")))
        except ValueError:
            raise AppError(ErrorCode.bad_request, "Invalid grant.") from None
        refresh_value = str(payload.get("refresh_token", ""))
        if not refresh_value:
            raise AppError(ErrorCode.bad_request, "Invalid grant.")
        with engine.begin() as conn:
            access, refresh, expires_in, scope_id, caps = oauth_service.refresh_grant(
                conn,
                client_id=client_id,
                refresh_token=refresh_value,
                access_ttl_seconds=settings.oauth_access_ttl_seconds,
                refresh_ttl_days=settings.oauth_refresh_ttl_days,
                request_id=request_id,
            )
    else:
        raise AppError(ErrorCode.bad_request, "Unsupported grant_type.")
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "refresh_token": refresh,
        "scope": " ".join([*caps, f"scope:{scope_id}"]),
    }


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=500)
    token_type_hint: str | None = Field(default=None, max_length=50)


@router.post("/oauth/revoke")
def revoke(req: RevokeRequest, engine: EngineDep) -> dict[str, Any]:
    """Public revocation; always 200 so callers cannot probe token existence."""
    with engine.begin() as conn:
        oauth_service.revoke_token(conn, req.token)
    return {}


@router.get("/oauth/config")
def oauth_config(request: Request) -> dict[str, Any]:
    """Public login options. Names only, never secrets."""
    settings: Settings = request.app.state.settings
    return {
        "github": bool(settings.github_client_id and settings.github_client_secret),
        "google": bool(settings.google_client_id and settings.google_client_secret),
        "owner_email_set": bool(settings.owner_email),
    }


def _github_settings(settings: Settings) -> tuple[str, str, str, str]:
    """Client id, secret, dashboard origin and issuer, or a safe error."""
    if not settings.github_client_id or not settings.github_client_secret:
        raise AppError(ErrorCode.bad_request, "GitHub login is not configured.")
    if not settings.oauth_authorize_url:
        raise AppError(ErrorCode.bad_request, "Dashboard approval URL is not configured.")
    origin = github_oauth.dashboard_origin(settings.oauth_authorize_url)
    return (
        settings.github_client_id,
        settings.github_client_secret.get_secret_value(),
        origin,
        _issuer(settings),
    )


@router.get("/oauth/github/login")
def github_login(
    request: Request, engine: EngineDep, next: str | None = Query(default=None)
) -> RedirectResponse:
    """Start owner sign-in. Stores a one-time state, then redirects to github.com."""
    settings: Settings = request.app.state.settings
    client_id, _, origin, issuer = _github_settings(settings)
    with engine.begin() as conn:
        target = github_oauth.start_login(
            conn,
            github_client_id=client_id,
            issuer=issuer,
            origin=origin,
            next_url=next,
        )
    return RedirectResponse(url=target, status_code=302)


@router.get("/oauth/github/callback")
def github_callback(
    request: Request,
    engine: EngineDep,
    request_id: RequestIdDep,
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    """github.com returns here. Verify email, mint a short owner credential, go home."""
    import httpx

    settings: Settings = request.app.state.settings
    _, secret, origin, issuer = _github_settings(settings)
    client_id = settings.github_client_id or ""
    with engine.begin() as conn:
        next_url = github_oauth.consume_state(conn, state)
    if next_url is not None:
        github_oauth.clean_next_url(next_url, origin)
    else:
        next_url = f"{origin}/"
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as http:
            token_resp = http.post(
                github_oauth.GITHUB_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": client_id,
                    "client_secret": secret,
                    "code": code,
                    "redirect_uri": f"{issuer}/oauth/github/callback",
                },
            )
            token_body = token_resp.json()
            access = token_body.get("access_token") if isinstance(token_body, dict) else None
            if token_resp.status_code != 200 or not isinstance(access, str) or not access:
                raise AppError(ErrorCode.bad_request, "GitHub authorization failed.")
            emails_resp = http.get(
                github_oauth.GITHUB_EMAILS_URL,
                headers={
                    "Authorization": f"Bearer {access}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "locke-memory",
                },
            )
            emails_body = emails_resp.json()
            if emails_resp.status_code != 200:
                raise AppError(ErrorCode.bad_request, "GitHub authorization failed.")
    except AppError:
        raise
    except Exception:
        raise AppError(ErrorCode.dependency_unavailable, "GitHub is unreachable.") from None
    email = github_oauth.check_allowlist(
        settings.owner_email, github_oauth.pick_verified_email(emails_body)
    )
    _ = email
    with engine.begin() as conn:
        _, token = github_oauth.mint_session_credential(
            conn, ttl_hours=settings.github_oauth_ttl_hours, request_id=request_id
        )
    finish = f"{origin}/oauth/github/finish?next={quote(next_url, safe='')}"
    return RedirectResponse(url=f"{finish}#token={quote(token, safe='')}", status_code=302)
