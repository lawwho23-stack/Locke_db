"""Error codes and the one exception type the services raise (contract section 2).

Services raise `AppError`. The API layer turns it into the JSON error envelope:
{"error": {"code", "message", "request_id", "details", "retry_after"}}.
Messages must be safe to show to clients: never put memory text or secrets in them.
"""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    validation_error = "validation_error"
    bad_request = "bad_request"
    unauthenticated = "unauthenticated"
    forbidden = "forbidden"
    not_found = "not_found"
    version_conflict = "version_conflict"
    idempotency_mismatch = "idempotency_mismatch"
    memory_suppressed = "memory_suppressed"
    fact_key_race = "fact_key_race"
    invalid_transition = "invalid_transition"
    payload_too_large = "payload_too_large"
    quota_exceeded = "quota_exceeded"
    rate_limited = "rate_limited"
    dependency_unavailable = "dependency_unavailable"
    internal_error = "internal_error"


# HTTP status for every code. Keep this complete: a missing code would be a bug.
STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.validation_error: 422,
    ErrorCode.bad_request: 400,
    ErrorCode.unauthenticated: 401,
    ErrorCode.forbidden: 403,
    ErrorCode.not_found: 404,
    ErrorCode.version_conflict: 409,
    ErrorCode.idempotency_mismatch: 409,
    ErrorCode.memory_suppressed: 409,
    ErrorCode.fact_key_race: 409,
    ErrorCode.invalid_transition: 409,
    ErrorCode.payload_too_large: 413,
    ErrorCode.quota_exceeded: 429,
    ErrorCode.rate_limited: 429,
    ErrorCode.dependency_unavailable: 503,
    ErrorCode.internal_error: 500,
}


class AppError(Exception):
    """A business-rule failure with a stable code, a safe message and optional details."""

    code: ErrorCode
    message: str
    details: dict[str, Any]
    retry_after: int | None

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # Always an object (never None) so the envelope shape is stable.
        self.details = details if details is not None else {}
        self.retry_after = retry_after

    @property
    def http_status(self) -> int:
        return STATUS_BY_CODE[self.code]
