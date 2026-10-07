# Agent Memory Platform

A shared memory service for AI agents: memories, authored task checkpoints, versioned
documents, and owner-approved skills. Each record lives in a *scope* (for example `personal`
or a project), and each token can only use its explicitly granted capabilities.

The full design is in `agent-memory-platform-v1-architecture.md`. The exact Phase 1 rules are
in `docs/phase1-contract.md`.

Stack: Python >=3.13 (verified on 3.14), FastAPI, SQLAlchemy 2 (Core, sync), psycopg 3,
Alembic, PostgreSQL + pgvector, and a Next.js owner dashboard.
The code is in `backend/src/memory_platform/`; tests are in `tests/`.

## Setup

```bash
uv sync                      # install Python packages into .venv
cp .env.example .env         # then fill in real values (never commit .env)
```

## Start the test database

Tests use a throw-away Postgres in Docker on port **5433** (the Homebrew Postgres on 5432 is not
touched). Docker Desktop must be running.

```bash
docker compose up -d         # start it
pg_isready -h localhost -p 5433
docker compose down          # stop it (all its data disappears; that is fine)
```

## Run the tests

```bash
uv run pytest -o addopts='' -q
```

Each pytest run creates its own database called `memtest_<random>` on the test server, builds
the schema with Alembic, and drops the database at the end. Runs do not interfere with each other.

## Checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy backend/src
cd apps/web
npm ci
npm test
npm run typecheck
npm run build
```

Build the dashboard **before** the full pytest run: `test_dashboard_web.py` starts the built
Next.js application and otherwise skips. That test and the cross-client test start their
own loopback servers and use the disposable pytest database. No cloud data is touched.

## Database migrations

Run these from the `backend/` folder. Alembic prints the target (`host:port/database`) first,
so check that line before trusting the run.

```bash
cd backend
uv run alembic upgrade head          # uses MIGRATION_DATABASE_URL from .env
uv run alembic -x url=postgresql+psycopg://postgres:postgres@localhost:5433/mydb upgrade head
uv run alembic downgrade base        # undo everything
uv run alembic check                 # compares combined table metadata with the target database
```

The local schema test runs this check against its disposable database. Do not run the
commands above against a cloud database as part of tests. Alembic does not compare every
CHECK constraint; the behavioral database tests also matter.

Migrations `0002`–`0005` add skills, tasks, knowledge, and selected capture/provenance.
For an existing Phase 1 installation,
after explicitly choosing and migrating the intended database, run
`uv run memory-admin backfill-owner-permissions` once. It expands only existing active owner
admin grants; agent grants stay unchanged. Fresh bootstrap grants all current capabilities.

## First start (bootstrap)

There is no signup. Create the first workspace, owner and token yourself. The token is shown
**once**, so store it right away:

```bash
uv run memory-admin bootstrap --owner-name "Your Name"
uv run memory-admin create-scope --kind project --name my-project
```

## Local API smoke

The smoke script starts a real Uvicorn server and exercises bootstrap, scoped credentials,
the memory lifecycle, concurrent-version rejection, retry replay, forgetting and revocation:

```bash
uv run python scripts/smoke_phase1.py
```

It creates and drops only its own random database on localhost:5433, stops its own server,
and never prints its generated credentials or uses the deployment database.

## Run the API

After configuring `.env`, migrating the intended database and bootstrapping the owner:

```bash
uv run uvicorn memory_platform.api.app:create_default_app --factory --host 127.0.0.1 --port 8000
```

`GET /health` is a public liveness check. Authenticated `GET /ready` reports
migration currency and workspace job depth (503 when the database is unavailable).
All `/v1` routes require `Authorization: Bearer <token>`, including skills, tasks,
sources, recall and dashboard views. Per-credential rate limiting defaults to 120
requests/minute (`RATE_LIMIT_PER_MINUTE`, 0 disables); violations return 429 with
`Retry-After`. Writes can supply `Idempotency-Key` to retry without repeating
effects. PATCH requires `expected_version`; stale versions receive a 409 conflict
instead of silently overwriting.

Forgetting erases content, labels and fact keys from the current row and every historical
version. Keyed hashes block agents from adding that content/fact identity again in the same
scope; an explicit owner remember can clear matching suppressions. Retain `MEMORY_HMAC_KEY`
with database backups: replacing it breaks matching against existing suppressions.

Updates and forgetting serialize within a scope to protect concurrent corrections and
suppression checks. Remember calls can run concurrently and return a retryable fact-key
conflict when another writer wins.

## Shared clients and task checkpoints

The Python client, `memory` CLI and `memory-mcp` Model Context Protocol (MCP) server use the
same HTTP API. Identity and permission checks happen in the backend on every request.
Give Codex, Claude, and OpenCode **separate agent credentials** for the intended project
scope; do not put the owner credential in any agent configuration.

| Capability | Purpose |
| --- | --- |
| `memory:read`, `memory:write`, `memory:delete` | Read/recall, create/update, and explicit deletion |
| `task:read`, `task:write` | Shared checkpoints and credential-bound sessions |
| `source:ingest` | Upload/replace documents |
| `skill:read` | Resolve/download approved packages |
| `skill:write` | Skill lifecycle, additionally restricted to owner admin |

Set `MEMORY_API_URL`, `MEMORY_API_TOKEN` and `MEMORY_SCOPE_ID` in the client environment.
Examples are under `integrations/codex/`, `integrations/claude/` and `integrations/opencode/`;
replace their absolute repository path and verify hook support in your installed client
before enabling them.
`clients/python/example.py` also uses `MEMORY_SKILLS_SCOPE_ID` for a separate skill scope.

```sh
uv run memory tasks --scope "$MEMORY_SCOPE_ID"
uv run memory-mcp
```

The MCP process uses stdin/stdout as its protocol transport; a client must initialize the
connection before listing/calling tools. Tools cover recall (with optional `session_id`),
remember/get/update/forget, ingest, selected event capture, typed linking, skills, tasks,
sessions and handoffs. The optional HTTP bridge is
`memory_platform.mcp_server:create_http_app`; it requires each request's scoped bearer
and verifies it before any protocol handling. Local HTTP transport acceptance with
caller-specific authorization is tested; installed-client acceptance is still pending.

Hooks read authored checkpoints at lifecycle boundaries and register/heartbeat a session
when an external session ID is supplied. They do not capture prompts or transcripts, execute
checkpoint text, automatically publish progress, or complete tasks when a turn ends.

Write flow: register session → create task → publish checkpoint → offer/accept handoff.
Task writes require a stable `event_id`, increasing session `sequence`, `expected_version`
and ownership `generation`. Keep the same event ID/payload when retrying. A handoff advances
the generation, so the previous session cannot keep writing. Owner recovery is explicit.

`memory request METHOD /v1/...` reads a JSON body from stdin. `--queue` stores supported task
events in a private SQLite outbox before sending. `memory outbox` displays metadata;
`memory sync` retries queued events with their original IDs. A blocked event (conflict or
permission rejection) stops later events; after reconciling, use
`memory outbox-retry EVENT_ID` to requeue the identical payload or
`memory outbox-discard EVENT_ID` to drop it. Default location:
`~/.local/state/memory-platform/outbox.sqlite`. Protect it as authored checkpoint data.
Transport/server failures retry; a conflict or permission rejection blocks later events.
Re-read the shared task and deliberately reconcile that event before continuing; never
silently change an event's payload under the same ID. There is no automatic conflict repair.
Skills and credentials are prohibited outbox inputs.

## Owner-approved skills

Unapproved skill packages must never enter the database. Preview reads files locally and
prints the exact package hash without uploading anything:

```sh
uv run memory skill-preview /path/to/package
```

After the owner approves that exact hash, use an owner admin credential with `skill:write`:

```sh
uv run memory skill-store /path/to/package --scope "$MEMORY_SKILLS_SCOPE_ID" \
  --keyword /github_review --approved-hash OWNER_APPROVED_HASH
```

If any bytes, paths or executable flags change, preview and obtain approval again. Updates
also require the registry's current `--expected-revision`. Approved versions are immutable;
rollback selects a previously approved version and revoke blocks further downloads.

Agents resolve only by explicit command, using `skill:read`:

```sh
uv run memory skill-resolve /github_review --scope "$MEMORY_SKILLS_SCOPE_ID"
uv run memory skill-install /github_review --scope "$MEMORY_SKILLS_SCOPE_ID" \
  --destination /path/to/new-managed-directory
```

Installation verifies the hash and writes into a new directory; it does not execute scripts
or authorize dependencies. Unknown, denied or revoked lookup must stop instead of substituting
a draft. The example command is a resolver adapter; no custom GitHub reviewer is built.

## Documents, recall and worker

Upload text-based PDF, Markdown, TXT or DOCX through `/v1/sources` or the dashboard (up to
20 MiB; PDF up to 300 pages). OCR, macros, and executing document instructions are unsupported.
Original files live privately at `STORAGE_DIR` (default `.data/sources`); PostgreSQL stores
versions, chunks, job leases and citations. Back up both originals and the database together.

## Operations: backup, maintenance, quotas

`memory-admin` runs against the migration database (`MIGRATION_DATABASE_URL` when set,
else `DATABASE_URL`). Always confirm the target database before running it; never point
it at a deployment database for a test:

```sh
uv run memory-admin backup-export --output ./backup.bin
uv run memory-admin backup-restore --input ./backup.bin \
  --hmac-key-out ./backup-hmac-key  # private 0600 file, never printed
uv run memory-admin maintenance            # dry-run report
uv run memory-admin maintenance --apply    # erase eligible text
```

Export encrypts a snapshot of every table plus referenced originals; the passphrase
comes only from `MEMORY_BACKUP_PASSPHRASE` (16–1024 bytes). Restore works only into an
empty local database and refuses tampered, wrong-passphrase, and nonempty targets.
The restored suppression key is written to `--hmac-key-out` (refuses to overwrite);
set `MEMORY_HMAC_KEY` to its contents before starting the API, and keep it with backups.

Workspace quotas (`quota_memories`, `quota_tasks`, `quota_sources`, `quota_source_bytes`,
`quota_skill_bytes`) are enforced inside the consuming transaction; over-quota writes
return 429-style `quota_exceeded`. `GET /v1/usage` (owner) reports usage, provider
estimates, and job states. Maintenance scrubs expired idempotency receipts and, with
retention days configured, old completed-event text and unreferenced sessions — never
suppressions or approved packages.

```sh
PROVIDER_DAILY_BUDGET_USD=0 uv run python -m memory_platform.worker --once
```

Run without `--once` for continuous polling. Lexical parsing works without a model provider.
Optional enrichment has separate jobs and bounded retries, so an unavailable provider does
not discard ready lexical chunks. Publication rechecks version/deletion and lease ownership
after external work. `/v1/recall` returns authorized evidence with versioned citations and a
bounded UTF-8 context; token counts are estimates. Historical versions are not current recall.
Pass `session_id` to prepend the authorized session summary and recent selected events to
the context within the same budget. Memories extracted from a source stay hidden from
recall, listing, graph, and cache once their source is deleted or replaced without
supporting evidence.

`current_version` is the latest upload used for replacement conflict checks; recall serves
the highest ready version. The prior ready evidence remains searchable while a replacement
is pending or fails, and successful publication switches recall and invalidates cached results
atomically. A deleted source is concealed immediately. Original-file removal is attempted
immediately and also recorded as a durable cleanup job. Run the worker for retries; after
five failed cleanup attempts, repair storage access and retry the authorized source DELETE
to requeue cleanup. A successful DELETE confirms logical deletion; physical erasure may still
be pending after a filesystem failure.

External provider spending defaults to **zero**. Enabling embeddings requires an explicitly
chosen positive `PROVIDER_DAILY_BUDGET_USD`, HTTPS `PROVIDER_BASE_URL`, `PROVIDER_API_KEY`,
`EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, and a positive
`EMBEDDING_USD_PER_MILLION_TOKENS`. Extraction additionally uses `EXTRACTION_MODEL` and
`EXTRACTION_USD_PER_MILLION_TOKENS`. Reservations serialize per workspace/day; failed or
uncertain calls retain their reservation. No real provider calls were made for acceptance.

Redis REST caching is optional (`REDIS_REST_URL`, `REDIS_REST_TOKEN`). Entries contain IDs and
scores rather than source text. Authorization/revisions are checked in PostgreSQL and cached
IDs are rehydrated from eligible records. Cache failures fall back to database recall.
`CACHE_TTL_SECONDS` defaults to 180, maximum 300. External Redis acceptance remains unverified.

## Owner dashboard

See [dashboard setup](apps/web/README.md). Run the API separately, then start the dashboard
with a random server-only `SESSION_SECRET` and matching `WEB_ORIGIN`. Owner sign-in uses an
encrypted, HttpOnly, SameSite Strict cookie with a one-hour session. The bearer stays out of
browser responses/localStorage. Backend requests remain limited by explicit grants.

The dashboard supports memory edit/forget, source upload/delete, task/checkpoint inspection,
approved skill download/revoke, activity and a bounded graph. Skill import remains in the
owner CLI. There is no public signup or configured deployment.

## Verified status and remaining acceptance

On 2026-10-07 (V1 completion): **329 Python tests passed with no skips**. Ruff lint/format
checks, strict MyPy (71 files), dashboard policy tests (3), typecheck and production build
passed. Real local acceptance covers Phase 1 CLI/HTTP, migration drift through `0005`,
three separate scoped Codex/Claude/OpenCode-style clients sharing checkpoints, hook
subprocess events, `/github_review` resolution, MCP stdio **and** Streamable HTTP with
caller-specific authorization, session-aware recall, rate-limit 429s, readiness,
encrypted backup roundtrip (plus wrong-passphrase/tamper/nonempty refusal) via
`memory-admin`, outbox retry/discard, and dashboard sign-in/proxy/origin/logout plus a
real headless-Chrome run (memory filter, graph focus/expand, credential issuance,
document upload, mobile layout, sign out, zero JS exceptions). See
[progress ledger](docs/v1-implementation-progress.md) for fixes, review findings,
final counts and limitations.

Actual Codex/Claude/OpenCode application hook dispatch, real provider retrieval
quality/cost, external Redis, and cloud deployment remain unverified. No real provider
calls were made (spending stays $0 by default). The custom reviewer stays out of scope.
Nothing has been committed, pushed, or deployed.
