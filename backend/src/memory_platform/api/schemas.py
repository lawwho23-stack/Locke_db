"""All request and response models for the HTTP API (contract section 6).

JSON field names are snake_case and exactly as written here.
Request models use `extra="forbid"`: an unknown field is a 422 error. This is how we make
sure a client can NEVER send `trust`, `state` (on create), `owner_id` or `actor_id`.
Response models never contain secrets, and write responses never contain memory text.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from memory_platform.enums import (
    Capability,
    EvidenceKind,
    EvidenceRole,
    MemoryState,
    MemoryType,
    ScopeKind,
    Trust,
)
from memory_platform.errors import ErrorCode

# --- HTTP conventions (contract section 3) -------------------------------------------------
# Header names and formats are pinned here so every module uses the same ones.
REQUEST_ID_HEADER = "X-Request-ID"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,100}$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,200}$"

# --- Field formats (the same regexes are used by database check constraints) ---------------
FACT_KEY_PATTERN = r"^[a-z0-9][a-z0-9_.:-]{0,199}$"
SCOPE_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"

# Whitespace is stripped first, then the length is checked: "   " is rejected, and the
# stored text has no leading or trailing spaces.
Content = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
FactKey = Annotated[str, StringConstraints(pattern=FACT_KEY_PATTERN)]
ScopeName = Annotated[str, StringConstraints(pattern=SCOPE_NAME_PATTERN)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Importance = Annotated[float, Field(ge=0, le=1)]


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """Treat a time without a timezone as UTC (the platform stores UTC only)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _dedupe(labels: list[str]) -> list[str]:
    """Remove repeated labels but keep the first-seen order."""
    return list(dict.fromkeys(labels))


class _Request(BaseModel):
    """Base for every request body: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


# --- Requests --------------------------------------------------------------------------------


class RememberRequest(_Request):
    """Body of POST /v1/memories. Trust and state are decided by the server, not the client."""

    scope_id: UUID
    type: MemoryType
    content: Content
    labels: list[Label] = Field(default_factory=list, max_length=20)
    fact_key: FactKey | None = None
    importance: Importance = 0.5
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @field_validator("labels")
    @classmethod
    def _dedupe_labels(cls, value: list[str]) -> list[str]:
        return _dedupe(value)

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _as_aware_utc(value)

    @model_validator(mode="after")
    def _check_validity_window(self) -> Self:
        # Mirrors the database check `valid_until > valid_from`, but gives a clean 422.
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        return self


class UpdateRequest(_Request):
    """Body of PATCH /v1/memories/{id}.

    Only fields the client actually SENT are changed. To know which were sent, use
    `provided_fields()` (it is based on pydantic's `model_fields_set`), NOT "value is not None":
    - `fact_key: null` and `valid_until: null` mean "clear this field" and are allowed.
    - `content`, `labels`, `importance` and `state` cannot be null (422).
    - `state` can only be "active" (promoting a draft; the service checks who may do it).
    At least one field besides `expected_version` is required (else 422).
    """

    expected_version: int = Field(ge=1)
    content: Content | None = None
    labels: list[Label] | None = Field(default=None, max_length=20)
    fact_key: FactKey | None = None
    importance: Importance | None = None
    valid_until: datetime | None = None
    state: Literal["active"] | None = None

    def provided_fields(self) -> frozenset[str]:
        """Names of the fields the client sent, not counting `expected_version`."""
        return frozenset(self.model_fields_set - {"expected_version"})

    @field_validator("labels")
    @classmethod
    def _dedupe_labels(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _dedupe(value)

    @field_validator("valid_until")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _as_aware_utc(value)

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        provided = self.provided_fields()
        if not provided:
            raise ValueError("send at least one field to change besides expected_version")
        for name in ("content", "labels", "importance", "state"):
            if name in provided and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class ScopeCreateRequest(_Request):
    kind: ScopeKind
    name: ScopeName


class GrantIn(_Request):
    scope_id: UUID
    capabilities: list[Capability] = Field(min_length=1, max_length=len(Capability))

    @field_validator("capabilities")
    @classmethod
    def _unique(cls, value: list[Capability]) -> list[Capability]:
        if len(set(value)) != len(value):
            raise ValueError("capabilities must not repeat")
        return value


class CredentialCreateRequest(_Request):
    display_name: DisplayName
    grants: list[GrantIn] = Field(min_length=1, max_length=50)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _as_aware_utc(value)

    @field_validator("grants")
    @classmethod
    def _unique_scopes(cls, value: list[GrantIn]) -> list[GrantIn]:
        # The database has one grant row per (credential, scope), so repeats must be refused here.
        scope_ids = [grant.scope_id for grant in value]
        if len(set(scope_ids)) != len(scope_ids):
            raise ValueError("each scope may appear only once in grants")
        return value


# --- Responses -------------------------------------------------------------------------------


class MemoryWriteResponse(BaseModel):
    """Result of remember / update. No memory text, on purpose (it is also stored for replays)."""

    id: UUID
    scope_id: UUID
    state: MemoryState
    version: int
    trust: Trust
    indexing_status: Literal["not_applicable"] = "not_applicable"
    deduplicated: bool = False
    superseded_id: UUID | None = None
    conflict_with_id: UUID | None = None
    replayed: bool = False


class ForgetResponse(BaseModel):
    id: UUID
    state: Literal["deleted"] = "deleted"
    version: int
    replayed: bool = False


class EvidenceOut(BaseModel):
    id: UUID
    kind: EvidenceKind
    role: EvidenceRole
    actor_id: UUID
    request_id: str
    created_at: datetime
    source_id: UUID | None = None
    source_version_id: UUID | None = None
    chunk_id: UUID | None = None
    ordinal: int | None = None
    page: int | None = None


class VersionOut(BaseModel):
    version: int
    content: str | None
    state: MemoryState
    labels: list[str]
    fact_key: str | None
    actor_id: UUID
    reason: str
    created_at: datetime


class MemoryOut(BaseModel):
    id: UUID
    scope_id: UUID
    type: MemoryType
    content: str
    labels: list[str]
    fact_key: str | None
    trust: Trust
    importance: float
    # EFFECTIVE state: "expired" when the stored state is active and valid_until has passed.
    state: MemoryState
    valid_from: datetime | None
    valid_until: datetime | None
    version: int
    created_by: UUID
    created_at: datetime
    updated_at: datetime


class MemoryDetail(MemoryOut):
    evidence: list[EvidenceOut]
    # Ascending by version.
    versions: list[VersionOut]


class MemoryListResponse(BaseModel):
    items: list[MemoryOut]
    next_cursor: str | None = None


class ScopeOut(BaseModel):
    id: UUID
    kind: ScopeKind
    name: str
    revision: int
    # The CALLER'S grant on this scope.
    capabilities: list[Capability]


class ScopeListResponse(BaseModel):
    items: list[ScopeOut]


class GrantOut(BaseModel):
    scope_id: UUID
    capabilities: list[Capability]


class CredentialCreateResponse(BaseModel):
    id: UUID
    actor_id: UUID
    # The plain token. It is shown ONCE, here, and never stored.
    token: str
    token_prefix: str
    grants: list[GrantOut]
    expires_at: datetime | None


class CredentialRevokeResponse(BaseModel):
    id: UUID
    revoked_at: datetime


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    retry_after: int | None = None


class ErrorEnvelope(BaseModel):
    """Every non-2xx response has this shape."""

    error: ErrorBody


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
