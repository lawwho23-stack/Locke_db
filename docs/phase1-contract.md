# Phase 1 Contract (source of truth for all builders)

This file pins every shared name, signature, and behavior for Phase 1 (durable core).
Blueprint: `agent-memory-platform-v1-architecture.md`. Plan: Phase 0 + Phase 1.
If code and this file disagree, this file wins; report the mismatch instead of improvising.

## 0. Environment facts

- Python 3.14 (fallback 3.13), uv workspace. Package `memory-platform` in `backend/`, import name `memory_platform`, src layout `backend/src/memory_platform/`.
- DB access: **SQLAlchemy 2 Core** (`Table` objects, `select()/insert()/update()`), **sync**, driver **psycopg 3** (`postgresql+psycopg://`). No ORM classes.
- Neon dev: Postgres **18.6**, pgvector **0.8.6**. Local test DB: Docker `pgvector/pgvector:pg18` on **localhost:5433**, user/password `postgres/postgres`.
- `.env` (gitignored, already written — never print or edit it) holds `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `TEST_DATABASE_URL`, `MEMORY_HMAC_KEY` (64 hex chars).
- All IDs UUID (`uuid.uuid4()` generated in Python or `gen_random_uuid()` server default). All times `timestamptz`, UTC.

## 1. Enums — `memory_platform/enums.py` (StrEnum)

| Enum | Values |
|---|---|
| `MemoryType` | `fact`, `preference`, `decision`, `experience`, `procedure` |
| `MemoryState` | `draft`, `active`, `superseded`, `expired`, `deleted` |
| `Trust` | `owner_asserted`, `source_extracted`, `agent_reported`, `inferred` |
| `ActorKind` | `owner`, `agent` |
| `ScopeKind` | `personal`, `project` |
| `Capability` | `memory:read`, `memory:write`, `memory:delete`, `source:ingest` |
| `RelationType` | `supersedes`, `related_to`, `conflicts_with` |
| `RelationOrigin` | `asserted`, `system`, `suggested` |
| `RelationStatus` | `active`, `removed` |
| `EvidenceKind` | `assertion` |
| `EvidenceRole` | `supports`, `contradicts` |

Check constraints in the DB use exactly these strings.

## 2. Errors — `memory_platform/errors.py`

`class ErrorCode(StrEnum)` and HTTP status:

| Code | HTTP | When |
|---|---|---|
| `validation_error` | 422 | Bad body/params (also used for FastAPI `RequestValidationError`) |
| `bad_request` | 400 | Well-formed but invalid request (e.g. empty PATCH, bad cursor) |
| `unauthenticated` | 401 | Missing/invalid/expired/revoked token |
| `forbidden` | 403 | Scope is visible to caller but capability missing; admin-only route |
| `not_found` | 404 | Record absent, deleted, or in a scope the caller cannot see (concealment) |
| `version_conflict` | 409 | `expected_version` != current version. `details.current_version` set |
| `idempotency_mismatch` | 409 | Same `Idempotency-Key` reused with a different request body |
| `memory_suppressed` | 409 | Agent tried to remember/update to content or fact_key that was forgotten in that scope |
| `fact_key_race` | 409 | Concurrent writer won the active `fact_key`; caller should re-read and retry. `retry_after` = 1 |
| `invalid_transition` | 409 | State change not allowed (e.g. agent promotes draft→active) |
| `payload_too_large` | 413 | Body over size limit |
| `dependency_unavailable` | 503 | DB unreachable |
| `internal_error` | 500 | Unexpected; message is generic |

```python
class AppError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, details: dict[str, Any] | None = None,
                 retry_after: int | None = None) -> None: ...
    code: ErrorCode; message: str; details: dict[str, Any]; retry_after: int | None
    @property
    def http_status(self) -> int: ...   # from STATUS_BY_CODE
STATUS_BY_CODE: dict[ErrorCode, int]
```

Error envelope (every non-2xx response):

```json
{"error": {"code": "version_conflict", "message": "safe text", "request_id": "…",
           "details": {"current_version": 3}, "retry_after": null}}
```
`details` is always an object (may be `{}`); `retry_after` is int or null. Also send HTTP `Retry-After` header when set.

## 3. HTTP conventions

- Auth header: `Authorization: Bearer mem_…`. Only `/health` is unauthenticated.
- `Idempotency-Key` header (1–200 chars, `[A-Za-z0-9._:-]`) accepted on `POST /v1/memories`, `PATCH /v1/memories/{id}`, `DELETE /v1/memories/{id}`. Optional. Ignored elsewhere.
- `X-Request-ID`: echoed if the client sends a valid one (≤100 chars, `[A-Za-z0-9._:-]`), else server generates `uuid4().hex`. Always returned as a response header and in error bodies.
- Max request body 64 KiB → 413 `payload_too_large`.
- JSON field names are snake_case, exactly as in §6.

## 4. Tokens — `auth/tokens.py` (implemented fully in Wave 1)

```python
TOKEN_PREFIX = "mem_"
def generate_token() -> str            # "mem_" + secrets.token_urlsafe(32)
def hash_token(token: str) -> str      # hashlib.sha256(token.encode("utf-8")).hexdigest()  (full string incl. prefix)
def display_prefix(token: str) -> str  # token[:12]
```

## 5. Database schema — `tables.py` (SQLAlchemy Core) + Alembic `0001_core`

MetaData naming convention: `ix_%(column_0_label)s`, `uq_%(table_name)s_%(column_0_name)s`, `ck_%(table_name)s_%(constraint_name)s`, `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`, `pk_%(table_name)s`. Names marked **pinned** below are set explicitly because code catches them.

Migration first runs `CREATE EXTENSION IF NOT EXISTS vector` (availability check; no vector columns in Phase 1).

**workspaces**: `id` uuid pk, `name` text not null, `created_at` timestamptz not null default now().

**scopes**: `id` uuid pk, `workspace_id` uuid not null → workspaces, `kind` text (ScopeKind check), `name` text not null (check `name ~ '^[a-z0-9][a-z0-9-]{0,62}$'`), `revision` bigint not null default 0 (check ≥0), `grant_revision` bigint not null default 0, `created_at`. Unique `(id, workspace_id)` **pinned `uq_scopes_id_workspace`**; unique `(workspace_id, name)`.

**actors**: `id` pk, `workspace_id` → workspaces, `kind` (ActorKind check), `display_name` text not null, `created_at`. Unique `(id, workspace_id)` **pinned `uq_actors_id_workspace`**. Partial unique index on `(workspace_id) WHERE kind='owner'` **pinned `uq_actors_one_owner`**.

**credentials**: `id` pk, `workspace_id`, `actor_id`; composite FK `(actor_id, workspace_id)` → `actors(id, workspace_id)`; `token_hash` text not null unique; `token_prefix` text not null; `is_admin` bool not null default false; `created_at`; `expires_at` timestamptz null; `revoked_at` timestamptz null. Unique `(id, workspace_id)` **pinned `uq_credentials_id_workspace`**.

**credential_grants**: `credential_id`, `scope_id`, `workspace_id`; composite FKs `(credential_id, workspace_id)` → credentials, `(scope_id, workspace_id)` → scopes; `capabilities` text[] not null; check `capabilities <@ ARRAY['memory:read','memory:write','memory:delete','source:ingest']::text[] AND cardinality(capabilities) > 0`; pk `(credential_id, scope_id)`.

**memories**: `id` pk; `workspace_id`; `scope_id`; composite FK `(scope_id, workspace_id)` → scopes; `type` (MemoryType check); `content` text **null** (null only when deleted); `content_hash` text null; `fact_key` text null (check `fact_key ~ '^[a-z0-9][a-z0-9_.:-]{0,199}$'`); `labels` text[] not null default '{}' (check `cardinality(labels) <= 20`); `trust` (Trust check); `importance` real not null default 0.5 (check 0..1); `state` (MemoryState check); `valid_from` timestamptz null; `valid_until` timestamptz null (check `valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from`); `version` integer not null default 1 (check ≥1); `created_by` uuid not null, composite FK `(created_by, workspace_id)` → actors; `created_at`, `updated_at` not null default now(). Check: `(state = 'deleted') OR (content IS NOT NULL AND content_hash IS NOT NULL)`. Unique `(id, workspace_id)` **pinned `uq_memories_id_workspace`**.
Indexes: `(scope_id, state, updated_at)`; GIN on `labels`; `(scope_id, type, content_hash)`; partial unique `(scope_id, type, fact_key) WHERE state='active' AND fact_key IS NOT NULL` **pinned `uq_memories_active_fact_key`**.

**memory_versions**: `id` pk; `memory_id` → memories (on delete cascade); `version` int not null; `content` text null; `state` text not null; `labels` text[] not null; `fact_key` text null; `actor_id` uuid not null; `reason` text not null (e.g. `created`, `updated`, `promoted`, `superseded`, `forgotten`); `created_at`. Unique `(memory_id, version)` **pinned `uq_memory_versions_memory_version`**.

**memory_evidence**: `id` pk; `memory_id` → memories (cascade); `kind` (EvidenceKind check); `role` (EvidenceRole check); `actor_id` uuid not null; `request_id` text not null; `created_at`.

**memory_relations**: `id` pk; `workspace_id`; `from_id`, `to_id` with composite FKs `(from_id, workspace_id)` and `(to_id, workspace_id)` → `memories(id, workspace_id)`; `type` (RelationType check); `origin` (RelationOrigin check); `status` (RelationStatus check, default active); `created_by` uuid not null; `created_at`. Check `from_id <> to_id` **pinned `ck_memory_relations_no_self_link`**. Unique `(from_id, to_id, type)` **pinned `uq_memory_relations_tuple`**. Direction: `supersedes` = new → old; `conflicts_with` = new draft → existing active.

**forget_suppressions**: `id` pk; `workspace_id`; `scope_id`; composite FK → scopes; `content_hmac` text null; `fact_key_hmac` text null; check at least one not null; `created_at`. Index `(scope_id, content_hmac)`, `(scope_id, fact_key_hmac)`. **No plain text ever.**

**operations**: `id` pk; `actor_id` uuid not null; `workspace_id` uuid not null; `operation` text not null; `idempotency_key` text not null; `request_hash` text not null; `response_status` int null; `response_body` jsonb null (**content-free**: ids, version, state, flags only); `created_at`; `expires_at` timestamptz not null. Unique `(actor_id, operation, idempotency_key)` **pinned `uq_operations_actor_op_key`**.

**activity**: `id` pk; `workspace_id`; `actor_id` uuid null; `action` text not null; `target_ids` uuid[] not null default '{}'; `scope_id` uuid null; `request_id` text not null; `result` text not null (`ok` or an ErrorCode); `latency_ms` int null; `created_at`. **No content columns.**

`tables.py` exports `metadata` and one `Table` object per table, named exactly as the table (`workspaces`, `scopes`, `actors`, `credentials`, `credential_grants`, `memories`, `memory_versions`, `memory_evidence`, `memory_relations`, `forget_suppressions`, `operations`, `activity`). `alembic check` must report no drift between `tables.py` and the migration.

## 6. API schemas — `api/schemas.py` (Pydantic v2; all request models `extra="forbid"`)

Requests:
- `RememberRequest`: `scope_id: UUID`, `type: MemoryType`, `content: str` (1–8000 chars after strip), `labels: list[str] = []` (≤20, each 1–64 chars, stripped, de-duplicated keeping order), `fact_key: str | None = None` (pattern `^[a-z0-9][a-z0-9_.:-]{0,199}$`), `importance: float = 0.5` (0–1), `valid_from: datetime | None = None`, `valid_until: datetime | None = None`. Clients **cannot** send `trust`, `state`, `owner_id`, `actor_id` (extra=forbid → 422).
- `UpdateRequest`: `expected_version: int` (≥1), optional `content`, `labels`, `fact_key`, `importance`, `valid_until`, `state: Literal["active"] | None` (promotion only). Validator: at least one field besides `expected_version` → else 422.
- `ScopeCreateRequest`: `kind: ScopeKind`, `name: str` (scope name pattern).
- `GrantIn`: `scope_id: UUID`, `capabilities: list[Capability]` (1–4, unique).
- `CredentialCreateRequest`: `display_name: str` (1–100), `grants: list[GrantIn]` (1–50), `expires_at: datetime | None = None`.

Responses:
- `MemoryWriteResponse`: `id`, `scope_id`, `state`, `version`, `trust`, `indexing_status: Literal["not_applicable"]`, `deduplicated: bool`, `superseded_id: UUID | None`, `conflict_with_id: UUID | None`, `replayed: bool`. **No content.**
- `ForgetResponse`: `id`, `state: Literal["deleted"]`, `version`, `replayed: bool`.
- `EvidenceOut`: `id`, `kind`, `role`, `actor_id`, `request_id`, `created_at`.
- `VersionOut`: `version`, `content: str | None`, `state`, `labels`, `fact_key`, `actor_id`, `reason`, `created_at`.
- `MemoryOut`: `id`, `scope_id`, `type`, `content`, `labels`, `fact_key`, `trust`, `importance`, `state` (**effective**: `expired` when stored `active` and `valid_until <= now()`), `valid_from`, `valid_until`, `version`, `created_by`, `created_at`, `updated_at`.
- `MemoryDetail(MemoryOut)`: + `evidence: list[EvidenceOut]`, `versions: list[VersionOut]` (ascending by version).
- `MemoryListResponse`: `items: list[MemoryOut]`, `next_cursor: str | None`.
- `ScopeOut`: `id`, `kind`, `name`, `revision`, `capabilities: list[Capability]` (caller's grant on it).
- `ScopeListResponse`: `items: list[ScopeOut]`.
- `GrantOut`: `scope_id`, `capabilities`.
- `CredentialCreateResponse`: `id`, `actor_id`, `token` (plain, shown once), `token_prefix`, `grants: list[GrantOut]`, `expires_at`.
- `CredentialRevokeResponse`: `id`, `revoked_at`.
- `ErrorBody`: `code`, `message`, `request_id`, `details: dict`, `retry_after: int | None`; `ErrorEnvelope`: `error: ErrorBody`.
- `HealthResponse`: `status: Literal["ok"]`.

## 7. Routes — `api/routes/*.py`, app factory `api/app.py`

`create_app(*, settings: Settings, engine: Engine) -> FastAPI`. Stores both on `app.state`. Tests build the app this way.

| Method/path | Capability | Success | Notes |
|---|---|---|---|
| `GET /health` | none | 200 `HealthResponse` | |
| `POST /v1/memories` | `memory:write` on `scope_id` | **201** new / **200** deduplicated or replay of a 200 | `MemoryWriteResponse` |
| `GET /v1/memories` | `memory:read` | 200 `MemoryListResponse` | query: `scope_id` (repeatable; default = all readable scopes; a requested scope not readable → 404), `label`, `type`, `state` (default `active`; `deleted` not allowed → 422), `limit` 1–100 (default 20), `cursor` |
| `GET /v1/memories/{id}` | `memory:read` | 200 `MemoryDetail` | deleted → 404 |
| `PATCH /v1/memories/{id}` | `memory:write` | 200 `MemoryWriteResponse` | |
| `DELETE /v1/memories/{id}` | `memory:delete` | 200 `ForgetResponse` | |
| `GET /v1/scopes` | any grant | 200 `ScopeListResponse` | only granted scopes |
| `POST /v1/scopes` | admin | 201 `ScopeOut` | grants all 4 capabilities to every non-revoked admin credential of the owner |
| `POST /v1/credentials` | admin | 201 `CredentialCreateResponse` | creates a new **agent** actor; grants must reference this workspace's scopes (else 404) |
| `DELETE /v1/credentials/{id}` | admin | 200 `CredentialRevokeResponse` | immediate; later requests with it → 401 |

Memory-route access rule (in `services/access.py`): if the memory is missing, deleted, in another workspace, or in a scope where the caller has **no grant at all** → 404 `not_found`. If the caller has some grant on the scope but lacks the needed capability → 403 `forbidden`. For `POST /v1/memories`, the same rule applies to `scope_id`.

## 8. Python contracts (module → functions). `conn` is always a `sqlalchemy.Connection` already inside a transaction opened by the route (`with engine.begin() as conn:`). Services never commit or open their own transactions.

`config.py` (Wave 1, full):
```python
class Settings(BaseSettings):  # env_file = <repo root>/.env, extra="ignore"
    database_url: str
    migration_database_url: str | None = None
    test_database_url: str | None = None
    memory_hmac_key: SecretStr           # 64 hex chars → 32 bytes
    idempotency_ttl_hours: int = 24
    max_body_bytes: int = 65536
    db_prepare_threshold: int | None = 5  # set None if Neon pooler rejects prepared statements
    @property
    def hmac_key_bytes(self) -> bytes: ...
def get_settings() -> Settings   # cached
```

`db.py` (Wave 1, full): `make_engine(url: str, *, pool_size: int = 5, max_overflow: int = 5, prepare_threshold: int | None = 5) -> Engine` (pool_pre_ping=True; passes `prepare_threshold` via `connect_args`).

`auth/principal.py`:
```python
@dataclass(frozen=True)
class Principal:
    actor_id: UUID; actor_kind: ActorKind; workspace_id: UUID; credential_id: UUID
    is_admin: bool; grants: Mapping[UUID, frozenset[Capability]]
    def scopes_with(self, cap: Capability) -> frozenset[UUID]      # Wave 1, full
    def has_grant(self, scope_id: UUID) -> bool                     # Wave 1, full
    def require(self, scope_id: UUID, cap: Capability) -> None      # Wave 1, full: not_found / forbidden per §7
def resolve_principal(conn: Connection, token: str) -> Principal    # Wave 2 (auth): hash → credential; reject revoked/expired/unknown with AppError(unauthenticated); load grants from credential_grants
```

`api/deps.py` (Wave 2, auth): `get_settings_dep`, `get_engine_dep` (from `app.state`), `get_request_id(request) -> str`, `get_principal(...) -> Principal` (opens its own short read transaction; missing/malformed header → 401), `get_idempotency_key(...) -> str | None` (validates format → 422).

`core/normalize.py` (Wave 2, core-rules):
```python
def normalize_text(text: str) -> str          # NFC, casefold, collapse all whitespace runs to one space, strip
def content_hash(normalized: str) -> str      # sha256 hex of normalized utf-8
def suppression_hmac(normalized: str, key: bytes) -> str   # hmac-sha256 hex
def normalize_fact_key(fact_key: str) -> str  # strip + lower
```

`core/policy.py` (Wave 2, core-rules):
```python
def trust_for_actor(kind: ActorKind) -> Trust                 # owner→owner_asserted, agent→agent_reported
def initial_state(trust: Trust, mtype: MemoryType) -> MemoryState
    # owner_asserted → active (all types)
    # agent_reported: fact, decision → active; experience, preference, procedure → draft
    # source_extracted / inferred → draft (not used by Phase 1 routes)
def can_transition(old: MemoryState, new: MemoryState, actor: ActorKind) -> bool
    # only draft→active, only by owner (forget/supersede are internal paths, not PATCH)
def effective_state(state: MemoryState, valid_until: datetime | None, now: datetime) -> MemoryState
```

`services/types.py` (Wave 1, full):
```python
@dataclass(frozen=True)
class WriteOutcome:
    status_code: int
    body: dict[str, Any]          # JSON-safe, content-free (str UUIDs)
    action: str                   # e.g. "memory.remember"
    scope_id: UUID | None         # scope whose revision is bumped; None = no searchable change
    target_ids: tuple[UUID, ...]
    replayed: bool = False
```

`services/versions.py` (Wave 1, full): `append_version(conn, *, memory_id, version, content, state, labels, fact_key, actor_id, reason) -> None`.
`services/evidence.py` (Wave 1, full): `add_assertion_evidence(conn, *, memory_id, actor_id, request_id, role=EvidenceRole.supports) -> UUID`.

`services/idempotency.py` (Wave 2, write-wrapper):
```python
def request_hash(operation: str, payload: Mapping[str, Any]) -> str   # sha256 of canonical JSON (sort_keys, separators=(",",":"))
def run_write(conn, principal, *, operation: str, idempotency_key: str | None,
              payload: Mapping[str, Any], request_id: str, ttl_hours: int,
              action: Callable[[], WriteOutcome]) -> WriteOutcome
    # 1. if key: INSERT INTO operations ... ON CONFLICT (actor_id, operation, idempotency_key) DO NOTHING RETURNING id
    #    - conflict: SELECT existing; expired → delete it and proceed as new; hash differs → AppError(idempotency_mismatch);
    #      response stored → return WriteOutcome(stored status/body, replayed=True) (no new activity, no revision bump)
    # 2. outcome = action()
    # 3. if key: UPDATE operations SET response_status, response_body (outcome.body + {"replayed": false})
    # 4. if outcome.scope_id: bump_scope_revision(conn, outcome.scope_id)
    # 5. record_activity(conn, principal=..., action=outcome.action, target_ids=..., scope_id=..., request_id=..., result="ok")
def bump_scope_revision(conn, scope_id: UUID) -> int
def record_activity(conn, *, workspace_id: UUID, actor_id: UUID | None, action: str, target_ids: Sequence[UUID],
                    scope_id: UUID | None, request_id: str, result: str, latency_ms: int | None = None) -> None
```
Replay body must equal the original body except `replayed: true`.

`services/access.py` (Wave 2, auth) — **the one scope-filter path**:
```python
def fetch_memory_checked(conn, principal, memory_id: UUID, cap: Capability, *, for_update: bool = False) -> RowMapping
    # SELECT memories WHERE id=:id AND workspace_id=principal.workspace_id AND state <> 'deleted' [FOR UPDATE]
    # missing → not_found; then principal.require(row.scope_id, cap)
def readable_scope_ids(principal, requested: Sequence[UUID] | None) -> list[UUID]
    # None → all scopes with memory:read; any requested scope without any grant → not_found; with grant but no read → forbidden
```

`services/supersede.py` (Wave 2, remember):
```python
@dataclass(frozen=True)
class ActiveFact: id: UUID; version: int; trust: Trust
def find_active_fact_for_update(conn, *, scope_id, mtype, fact_key) -> ActiveFact | None   # SELECT ... FOR UPDATE
def mark_superseded(conn, *, memory_id, actor_id) -> int     # state→superseded, version+1, updated_at, append_version(reason="superseded"); returns new version
def link(conn, *, workspace_id, from_id, to_id, rtype: RelationType, origin: RelationOrigin, actor_id) -> UUID  # ON CONFLICT (tuple) DO NOTHING; returns id
def is_fact_key_race(exc: IntegrityError) -> bool   # constraint name == "uq_memories_active_fact_key"
```

`services/remember.py` (Wave 2, remember):
```python
def remember(conn, principal, req: RememberRequest, *, request_id: str, hmac_key: bytes) -> WriteOutcome
```
Order: `principal.require(scope, memory:write)` → normalize → suppression check (content_hmac OR fact_key_hmac match in scope: agent → `memory_suppressed`; owner → delete matching suppression rows) → exact duplicate (active, same scope/type/content_hash) → add evidence, return 200 `deduplicated=true`, `scope_id=None` (no searchable change) → fact_key: owner+existing active → `mark_superseded` old, insert new active, `link(supersedes new→old, origin=system)`; agent+existing active → insert new as `draft`, `link(conflicts_with new→old, origin=system)` → else state from `initial_state` → insert memory (version 1), `append_version(reason="created")`, `add_assertion_evidence` → 201. IntegrityError on `uq_memories_active_fact_key` → `AppError(fact_key_race, retry_after=1)`. `action="memory.remember"`.

`services/memory_queries.py` (Wave 2, read-update):
```python
def get_memory(conn, principal, memory_id: UUID) -> MemoryDetail
def list_memories(conn, principal, *, scope_ids: Sequence[UUID] | None, label: str | None, mtype: MemoryType | None,
                  state: MemoryState, limit: int, cursor: str | None) -> MemoryListResponse
    # keyset on (updated_at DESC, id DESC); cursor = urlsafe base64 of JSON {"u": iso, "i": uuid}; bad cursor → bad_request
    # state=active excludes rows with valid_until <= now(); state=expired returns active rows with valid_until <= now() and stored 'expired'
```

`services/update.py` (Wave 2, read-update):
```python
def update_memory(conn, principal, memory_id: UUID, req: UpdateRequest, *, request_id: str, hmac_key: bytes) -> WriteOutcome
```
`fetch_memory_checked(..., memory:write, for_update=True)` → if `row.version != req.expected_version` → `version_conflict` with `details.current_version` → state change validated with `can_transition` (else `invalid_transition`) → content/fact_key change by agent runs suppression check → promotion with fact_key runs supersede of the other active row (owner) and adds owner assertion evidence → `UPDATE … SET …, version = version + 1 WHERE id = :id AND version = :expected` (rowcount 0 → `version_conflict`) → `append_version(reason="updated"|"promoted")` → 200. IntegrityError fact-key → `fact_key_race`. `action="memory.update"`.

`services/forget.py` (Wave 2, forget):
```python
def forget_memory(conn, principal, memory_id: UUID, *, request_id: str, hmac_key: bytes) -> WriteOutcome
```
`fetch_memory_checked(..., memory:delete, for_update=True)` → collect normalized content + fact_key from the row **and every** `memory_versions` row → insert `forget_suppressions` (content_hmac and/or fact_key_hmac; skip duplicates within this call) → `UPDATE memories SET content=NULL, content_hash=NULL, labels='{}', fact_key=NULL, state='deleted', version=version+1, updated_at=now()` → `UPDATE memory_versions SET content=NULL, labels='{}', fact_key=NULL WHERE memory_id=:id` → `append_version(content=None, state=deleted, labels=[], fact_key=None, reason="forgotten")` → 200 `ForgetResponse` body. Superseded predecessors are **not** reactivated. `action="memory.forget"`.

`services/scopes.py` (Wave 2, scopes-creds-admin):
```python
def list_scopes(conn, principal) -> ScopeListResponse
def create_scope(conn, principal, req: ScopeCreateRequest, *, request_id: str) -> ScopeOut   # admin only → forbidden
```
`services/credentials.py`:
```python
def create_credential(conn, principal, req: CredentialCreateRequest, *, request_id: str) -> CredentialCreateResponse  # admin only
def revoke_credential(conn, principal, credential_id: UUID, *, request_id: str) -> CredentialRevokeResponse            # admin only; other workspace → not_found
def bootstrap_workspace(conn, *, owner_name: str, workspace_name: str = "default") -> tuple[UUID, str]  # (workspace_id, owner token); creates workspace, owner actor, 'personal' scope, admin credential + full grants
```
Scope/credential services write `activity` via `record_activity` and bump `grant_revision` where grants change. They do **not** use idempotency replay (a token must never be stored for replay).

`admin.py` (Wave 2): Typer-free `argparse` CLI `memory-admin` with `bootstrap --owner-name NAME [--workspace-name NAME]` and `create-scope --kind KIND --name NAME`. Uses `MIGRATION_DATABASE_URL` if set, else `DATABASE_URL`. Prints the token once to stdout with a "store it now" note.

## 9. Test fixtures — `tests/conftest.py` (Wave 1)

- Session: create database `memtest_<8 hex>` on `TEST_DATABASE_URL`'s server, run `alembic upgrade head` against it programmatically, build `Settings(database_url=<test db>, memory_hmac_key=<fixed 64-hex test key>)`, engine via `make_engine(url, pool_size=2, max_overflow=2)`; drop the DB `WITH (FORCE)` at session end.
- Each test gets its **own workspace** (no truncation; isolation by workspace).

| Fixture | Type | Meaning |
|---|---|---|
| `settings` | `Settings` | test settings |
| `engine` | `Engine` | test engine |
| `app` | `FastAPI` | `create_app(settings=settings, engine=engine)` |
| `workspace` | `WorkspaceInfo` dataclass: `id`, `owner_actor_id`, `owner_credential_id`, `owner_token`, `personal_scope_id` | seeded by direct inserts |
| `owner_client` | `TestClient` | Bearer owner token |
| `make_scope` | `Callable[[str, str], UUID]` `(kind, name)` | direct insert + full grants for owner credential |
| `make_agent_client` | `Callable[[Sequence[UUID], Sequence[str]], TestClient]` `(scope_ids, capabilities)` | new agent actor + credential + grants; returns client (`client.token` attribute holds the token) |
| `client_for_token` | `Callable[[str], TestClient]` | fresh client for a token (one per thread in concurrency tests) |
| `db_conn` | `Connection` (autocommit) | raw SQL checks |

## 10. Agent rules

- Edit only the files you own. If a contract change is needed, report it — do not edit shared files.
- No new dependencies. Never print, read aloud, or edit `.env`. No git commits. No cloud tools.
- `uv run ruff format <your files>`, `uv run ruff check <your files>`, `uv run mypy backend/src` (report errors only in your files).
- Look up current library APIs (FastAPI, SQLAlchemy 2, Alembic, psycopg 3, Pydantic v2) with context7 when unsure.
- Comments: short, plain English, explain *why*. The owner is learning Python by reading this code.

## 11. Decisions after Wave 1 (binding; these override earlier text where they differ)

1. **Empty PATCH** (only `expected_version`) → **422 `validation_error`** (the `UpdateRequest` validator). Use `UpdateRequest.provided_fields()` (based on `model_fields_set`) to know which fields were sent; explicit `fact_key: null` / `valid_until: null` = clear the field.
2. **`GET /v1/memories?scope_id=X`**: no grant at all on X → **404**; some grant but no `memory:read` → **403**. (Same rule as `Principal.require`.)
3. **Duplicate scope name** in a workspace → **400 `bad_request`**.
4. **Owner-only PATCH fields: `state` and `fact_key`.** An agent sending either → **403 `forbidden`**. PATCH is allowed only when the stored state is `active` or `draft`; otherwise → **409 `invalid_transition`**. Owner `state: "active"` on a `draft` = promotion; on an already-`active` memory → `invalid_transition`. Owner changing `fact_key` (or promoting) so that it collides with another active memory → supersede that other memory (same as remember: `mark_superseded`, `link(supersedes, new→old)`).
5. **`normalize_text`** = NFC → remove U+200B (zero-width space) and U+FEFF → `casefold()` → `" ".join(text.split())`. Keep ZWJ/ZWNJ. (Burmese text often carries U+200B.)
6. **`get_principal`** also sets `request.state.principal = principal` so error handlers can log activity.
7. **Validation errors must never echo input values** (they can contain memory text). Envelope `details` for 422 = `{"errors": [{"loc": [...], "msg": "...", "type": "..."}]}` — no `input`, no `ctx` values.
8. **`bootstrap_workspace`** refuses (raises `AppError(bad_request)`; CLI exits 1) if any workspace already exists. Single owner in V1.
9. **Revoking the calling credential itself** → `400 bad_request` (prevents locking yourself out). Revoking an already-revoked credential returns its existing `revoked_at` (200).
10. Python `Capability` members are `memory_read`, `memory_write`, `memory_delete`, `source_ingest` (values keep the `:` form).
11. Test helpers import as `from tests.conftest import WorkspaceInfo, seed_workspace, insert_workspace, insert_actor, insert_credential, insert_scope, insert_grant`.
12. Exact-duplicate check in `remember` matches **stored state `active` only** (drafts are not deduplicated in Phase 1).
13. **Test file ownership (Wave 2):** `auth` → `tests/test_auth_unit.py`; `core-rules` → `tests/test_policy.py`; `write-wrapper` → `tests/test_idempotency_unit.py`; `remember` → `tests/test_remember_unit.py`; `read-update` → `tests/test_update_unit.py`; `forget` → `tests/test_forget_unit.py`; `scopes-creds-admin` → `tests/test_admin_unit.py`; `tests-concurrency` → `tests/test_version_conflict.py`, `tests/test_idempotency.py`, `tests/test_fact_key_race.py`; `tests-access` → `tests/test_access.py`; `tests-forget` → `tests/test_forget.py`; `tests-lifecycle` → `tests/test_supersede.py`, `tests/test_memories_crud.py`, `scripts/smoke_phase1.py`. Nobody edits `tests/conftest.py`, `tests/test_schema.py`, `tests/test_foundation.py` in Wave 2.
