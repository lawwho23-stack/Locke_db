"""FastAPI dependencies shared by routes; writes use a separate route transaction."""

import re
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import Depends, Header, Request
from sqlalchemy import Engine

from memory_platform.api.schemas import IDEMPOTENCY_KEY_HEADER, IDEMPOTENCY_KEY_PATTERN
from memory_platform.auth.principal import Principal, resolve_principal
from memory_platform.config import Settings
from memory_platform.errors import AppError, ErrorCode

# Bounded in-process fallback for per-credential rate limits when Redis is
# unavailable or unconfigured. Keys are credential IDs, values are recent
# request timestamps. Evicted aggressively; never grows without bound.
_LOCAL_BUCKETS: dict[str, deque[float]] = {}
_LOCAL_LOCK = threading.Lock()
_LOCAL_MAX_KEYS = 5000


def get_settings_dep(request: Request) -> Settings:
    """Return settings supplied to the application factory."""
    return cast(Settings, request.app.state.settings)


def get_engine_dep(request: Request) -> Engine:
    """Return the engine supplied to the application factory."""
    return cast(Engine, request.app.state.engine)


def get_request_id(request: Request) -> str:
    """Return the request id assigned by middleware."""
    return cast(str, request.state.request_id)


def _redis_increment(settings: Settings, key: str) -> int | None:
    """Best-effort Redis counter; returns None when Redis is unusable."""
    if not settings.redis_rest_url or not settings.redis_rest_token:
        return None
    try:
        from memory_platform.cache import RedisCache

        cache = RedisCache(settings.redis_rest_url, settings.redis_rest_token.get_secret_value())
        count = cache._command(["INCR", key])
        if not isinstance(count, int):
            return None
        if count == 1:
            cache._command(["EXPIRE", key, 70])
        return count
    except Exception:
        return None


def enforce_rate_limit(request: Request, principal: Principal) -> None:
    """Per-credential per-minute limit with a bounded local fallback.

    A zero limit disables rate limiting. Redis is the primary counter when
    configured; the in-process fallback keeps single-process local V1 bounded
    when Redis fails. Exceeding the limit returns 429, never a silent drop.
    """
    settings = get_settings_dep(request)
    # Unit tests may inject a stub settings object; treat a missing limit as disabled.
    limit = getattr(settings, "rate_limit_per_minute", 0)
    if limit is None or limit <= 0:
        return
    minute = datetime.now(UTC).strftime("%Y%m%d%H%M")
    key = f"memory:ratelimit:{principal.credential_id}:{minute}"
    count = _redis_increment(settings, key)
    if count is not None:
        if count > limit:
            raise AppError(ErrorCode.rate_limited, "Rate limit exceeded.", retry_after=60)
        return
    now = time.monotonic()
    window = 60.0
    with _LOCAL_LOCK:
        credential_key = str(principal.credential_id)
        if len(_LOCAL_BUCKETS) >= _LOCAL_MAX_KEYS and credential_key not in _LOCAL_BUCKETS:
            _LOCAL_BUCKETS.pop(next(iter(_LOCAL_BUCKETS)))
        bucket = _LOCAL_BUCKETS.setdefault(credential_key, deque(maxlen=limit + 1))
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        bucket.append(now)
        if len(bucket) > limit:
            retry_after = max(1, int(bucket[0] + window - now))
            raise AppError(ErrorCode.rate_limited, "Rate limit exceeded.", retry_after=retry_after)


def get_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Authenticate in a short read transaction and expose the caller to error logging."""
    match = re.fullmatch(r"(?i:Bearer) (mem_[A-Za-z0-9_-]+)", authorization or "")
    if match is None:
        raise AppError(ErrorCode.unauthenticated, "Invalid credentials.")
    with get_engine_dep(request).begin() as conn:
        principal = resolve_principal(conn, match.group(1))
    request.state.principal = principal
    enforce_rate_limit(request, principal)
    return principal


def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_KEY_HEADER)] = None,
) -> str | None:
    """Validate the optional key without echoing potentially sensitive header values."""
    if (
        idempotency_key is not None
        and re.fullmatch(IDEMPOTENCY_KEY_PATTERN, idempotency_key) is None
    ):
        raise AppError(
            ErrorCode.validation_error,
            "Invalid Idempotency-Key header.",
            details={
                "errors": [
                    {
                        "loc": ["header", IDEMPOTENCY_KEY_HEADER],
                        "msg": "Invalid Idempotency-Key header.",
                        "type": "string_pattern_mismatch",
                    }
                ]
            },
        )
    return idempotency_key


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
EngineDep = Annotated[Engine, Depends(get_engine_dep)]
RequestIdDep = Annotated[str, Depends(get_request_id)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
IdempotencyKeyDep = Annotated[str | None, Depends(get_idempotency_key)]
