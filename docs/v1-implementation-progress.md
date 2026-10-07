# V1 implementation ledger

Plan: finish Memory Platform V1 and add approved-only skill storage.

## Completion pass — 2026-10-07

- Executing the approved verification plan in place on the unborn feature branch.
- Preserve the Git index; its initial SHA-256 is
  `65210c5aed655cee75e7a111348d3d3d088289a2232484d95886df436b5bf5c0`.
- Ruling: use this existing ledger rather than commit-based skill bookkeeping;
  the repository has no commits and the user requires no commits or staging changes.
  Evidence will remain in this ledger and ignored local verification logs.
- Ruff baseline: long hook string, example import ordering, and formatting in the
  Python example/outbox test. Applied behavior-preserving fixes to those three files.
- Docker was unavailable; launched Docker and started the repository testdb service.
  Do not stop unrelated Docker services or delete pre-existing databases.
- Fresh planning checks: MyPy passes (62 source files), dashboard typecheck passes,
  dashboard policy tests pass (2). Cross-client acceptance initially failed at database
  setup because localhost:5433 was unavailable; this is not application acceptance.
- Process acceptance exposed harness issues: missing MCP initialize handshake,
  outdated SDK result attribute, wrong Uvicorn factory in dashboard test, and a
  case-sensitive cookie assertion. Corrected these at their source; both real
  process tests now pass (2 passed).
- Full baseline revealed missing `source_enrichment` in migration 0004's job-kind
  CHECK constraint. Updated the undeployed migration to match table metadata.
- Ruling: historical source chunks remain versioned evidence. The replacement-race
  test now asserts current-version results, absence of obsolete embeddings, cancelled
  obsolete jobs, and exclusion from recall instead of requiring historical data deletion.
  Deleting all old chunks would contradict the versioned-source behavior; the risk is
  a missed current-version filter, so recall is checked directly.
- Phase 1 real CLI/HTTP smoke passes, including migration drift check, revocation,
  version conflict, idempotency, and forgotten-history scrub.
- Full pre-review verification: **288 passed, no skips**, Ruff check/format clean,
  MyPy clean, dashboard policy tests (2)/typecheck/build clean. This count predates
  the review regressions; final results below supersede it.
- Independent read-only review covered the full staged/unstaged/untracked feature.
  Four Important findings reproduced as four failing regression tests: stale source
  worker resets ready status; replacement hides last-ready evidence; failed original-file
  erasure loses retries; same-model dimension change cannot publish new vectors.
- Implemented initial lease fencing, a shared latest-ready evidence query for recall/cache,
  durable source cleanup jobs, and dimension-aware vector uniqueness in undeployed 0004.
- Ruling: keep `sources.current_version` as the latest upload/concurrency version;
  derive published evidence from the highest ready version. Publication of chunks/status
  and scope revision remains atomic without adding a second public version field.
  If this distinction is misunderstood, clients may display queued status while recall
  correctly returns the prior ready citation; the README explains the distinction.
- Final review: minor (deferred): dashboard concurrent tab/scope fetches can apply stale UI
  responses; add cancellation/generation guarding in a separate UI change.
- Final review: minor (deferred): new validity windows can take up to cache TTL to appear;
  existing returned records are rechecked for expiry/deletion. TTL is at most 300 seconds.
- Final review: minor (deferred): dashboard source-delete button uses source:ingest instead
  of memory:delete; backend enforces memory:delete, but restricted-owner controls may mislead.
- Review exclusions: encrypted export/restore, hosted deployment, custom reviewer, broader
  architecture-vision features and real provider acceptance stay outside this pass.

## Final verified results — 2026-10-07

- `uv run pytest -o addopts='' -q --tb=short` equivalent executed using `.venv/bin/python`:
  **294 passed in 9.67s**, exit 0, **no skips**. Full output retained in ignored
  `.superpowers/verification-2026-10-07/pytest-final.log`.
- `ruff check .`: passed. `ruff format --check .`: 101 files already formatted.
- `mypy backend/src`: passed, 62 source files.
- Dashboard `npm test`: 2 passed; `npm run typecheck` and `npm run build`: exit 0.
  Build emits a non-fatal warning about an unrelated parent-directory lockfile.
- `python scripts/smoke_phase1.py`: passed. Migration comparison reports
  `No new upgrade operations detected.` Both schema migration drift and behavioral
  constraints are covered on fresh, disposable PostgreSQL databases.
- Real Uvicorn + CLI + MCP stdio test passes with separate scoped agent credentials.
  CLI checkpoint reads and `/github_review` resolution agree with Python/MCP results.
  Codex/Claude hook commands pass as subprocesses with SessionStart/UserPromptSubmit
  input; context includes authored checkpoints and omits supplied prompt text.
- Real built Next.js + API acceptance passes without skipping: agent login denied,
  owner cookie encrypted/HttpOnly/SameSite Strict, bearer concealed, scoped proxy,
  blocked skill import, wrong-origin logout rejected, logout invalidates cookie access.
- Final: fixed stale worker — regression reproduced ready→processing, now keeps
  ready status/completed job/chunks and recall intact after the stale worker resumes.
- Final: fixed replacement publication — queued/failed replacement retains prior-ready
  evidence; new publication changes citations and invalidates cached old-ready results.
- Final: fixed physical source erasure — durable cleanup survives unlink failure and
  worker restart; five terminal failures can be explicitly requeued by authorized DELETE.
  Logical concealment precedes physical cleanup. Pending cleanup requires a running worker.
- Final: fixed dimension changes — same-model new-dimensional vectors publish and
  participate in semantic retrieval without editing the memory. All four Important
  findings reproduced RED→GREEN; final full suite 294/294.
- Updated the prior recall expectation: the old version stays searchable while the new
  upload is queued and disappears only after successful publication.
- No new real skill imports, external provider calls, external Redis calls, commits,
  staging changes, pushes, deployments or cloud migrations occurred.
- Cleanup verified: zero `memtest_*`/`memsmoke_*` databases remain, no test API/MCP/Next
  processes remain, and the testdb service started for this pass was stopped. Other
  Docker services were untouched. Git index SHA-256 still matches the initial snapshot.

## V1 completion pass — 2026-10-07 (build)

Goal: finish full V1 across all seven architecture phases for shared use by
Claude Code, Codex, OpenCode, and custom agents. Same boundaries: no commit,
push, or deploy; $0 provider spend; approved-skills-only; extracted drafts only.

### Correctness fixes (all RED→GREEN with regression tests)

- Recall now filters source-extracted memories through `current_source_evidence()`,
  matching list/detail. Deleted or replaced sources leave no unsupported memories
  retrievable via recall. Same predicate applied to cache hydrate, graph
  projection, and dashboard active counts. New test:
  `test_recall_hides_source_extracted_without_current_evidence`.
- Quota lock order is workspace-row first, then scope advisory lock, in
  remember/tasks/extraction paths (`persist_extraction` fixed).
- `remember()` takes the workspace quota lock just before insert instead of
  across the read phase. The coarse lock deadlocked the designed concurrent
  fact-key race (both writers got 500); now the race resolves 201/409 as
  specified. No TOCTOU: suppression stays covered by the scope advisory lock,
  duplicates/conflicts by unique indexes.
- Credential issuance enforces grant-subset: an issuer cannot grant capabilities
  beyond what it holds per scope. Cross-workspace scope requests still 404;
  over-grant requests 403. Check order preserved (`not_found` before `forbidden`).
- Stale test expectations reconciled without weakening guarantees:
  `test_operations` retention count 1→2 (creation record and checkpoint are both
  scrubbed, replay receipts survive); replay asserts identical version receipt
  (matches `test_tasks_api` exact-equality contract); `test_extraction` scope
  name `Elsewhere`→`elsewhere` (frozen `0001` lowercase format check).
- Dashboard browser test: wait for the Upload button to enable before clicking;
  the disabled-while-loading click silently no-opped and timed out.

### New V1 interfaces

- Session-aware recall: optional `session_id` on `POST /v1/recall` returns a
  `session` section (summary + up to 5 recent selected events) prepended to the
  context within the token budget. Scope-concealed sessions 404. Cache hydrate
  re-derives the section. New test `test_recall_with_session_context`.
- Rate limiting: per-credential per-minute (`rate_limit_per_minute`, default 120,
  0 disables). Redis INCR primary when configured; bounded in-process fallback
  otherwise. Violations return 429 with `Retry-After`. New test
  `test_rate_limit_returns_retryable_429`.
- Readiness: authenticated `GET /ready` reports migration head match plus
  workspace-scoped job counts; 503 when the database is unreachable. New test
  `test_ready_reports_migration_and_queue_depth`.
- `memory-admin` now wires `backup-export`, `backup-restore` (key written to a
  0600 file, never printed; refuses overwrite/nonempty targets), and
  `maintenance [--apply]` (dry-run report by default). Verified live against
  disposable databases: export → restore roundtrip, wrong-passphrase refusal,
  tamper refusal, nonempty-target refusal with data intact.
- CLI: `outbox-retry` / `outbox-discard` for blocked events (payload immutable).
  Verified manually; recovery logic covered by existing outbox tests.
- Python SDK: thin `remember`, `get_memory`, `update`, `forget`, `ingest`,
  `record_event`, `session_state`, `update_session_state`, `graph`, `job`,
  `usage`, `ready` (recall now takes `session_id`). No ranking logic in client.
- MCP: added `memory_ingest`, `memory_record_event`, `memory_link`;
  `memory_recall` accepts `session_id`. New `tests/test_mcp_http.py` proves
  Streamable HTTP per-request auth: no token 401, bad token 401, scoped reader
  recalls, ungranted caller gets a concealed error.
- `integrations/opencode/` added (MCP + hooks examples). Cross-client test now
  issues three separate credentials (codex/claude/opencode) sharing checkpoints.

### Verified results — 2026-10-07 (build)

- `uv run pytest -o addopts='' -q --tb=short`: **329 passed**, no skips, no failures.
- `ruff check .`: passed. `ruff format --check .`: 118 files formatted.
- `mypy backend/src`: passed, 71 source files.
- Dashboard `npm test`: 3 passed; `npm run typecheck` and `npm run build`: pass.
- `DASHBOARD_BROWSER_ACCEPTANCE=1` with local Chrome: **passed** — real login,
  memory filter, graph focus/expand, credential issuance without localStorage
  leakage, usage, document upload showing queued status, mobile layout, sign
  out, zero JS exceptions. Desktop/mobile screenshots captured to temp evidence.
- `python scripts/smoke_phase1.py`: passed, `No new upgrade operations detected`
  (fresh migration 0001→0005 verified separately on a disposable database).
- No `memtest_*`/`memsmoke_*`/`memadmin*` databases remain; no test servers
  remain; Docker testdb untouched otherwise. Nothing committed, pushed, or
  deployed. No external provider calls; no secrets printed.

## Remaining live acceptance and limitations

- Actual Codex/Claude/OpenCode applications have not dispatched these example
  hooks/configurations; subprocess/transport/browser acceptance must not be
  presented as installed-client acceptance.
- Real provider retrieval quality/cost and external Redis service remain
  unverified by budget decision. Simulated provider/cache tests verify logic
  without spending money. MCP HTTP acceptance above is real local transport
  with caller-specific authorization.
- Hosted deployment remains unverified and unauthorized.
- Owner dashboard UI concurrency, restricted-owner source-delete controls, and
  validity-start cache latency are the three deferred review minors listed above;
  backend authorization remains enforced.
- Task outbox conflicts block later writes. There is no automatic conflict repair;
  use `memory outbox-retry` / `memory outbox-discard` after deliberately
  reconciling against current ownership/version first.
- `memory-admin` uses `migration_database_url` when set: always confirm the
  target database before running admin commands against anything but a
  disposable local database. No cloud writes occurred in this pass.

## Delivery stages

1. Additive schemas and permissions — implemented and verified locally.
2. Approved skills and shared task checkpoints — implemented and verified locally.
3. Python client, CLI, task outbox and MCP/client adapters — implemented; local CLI,
   stdio and hook subprocess acceptance passes; Streamable HTTP acceptance passes
   with caller-specific authorization; OpenCode examples added; three-credential
   cross-client sharing verified. Installed-client acceptance pending.
4. Recall, versioned sources and durable worker — local lexical/fake-provider acceptance
   passes, including session-aware recall and unsupported-evidence filtering;
   real provider quality/cost acceptance pending by budget decision.
5. Cache and owner dashboard — implemented; local cookie/proxy/build, cache logic,
   and real browser acceptance verified; external Redis pending; three review minors
   remain with backend authorization enforced.
6. Encrypted export/restore, quotas and retention operations — implemented and
   verified locally via `memory-admin` (roundtrip, tamper/wrong-password/nonempty
   refusal), quotas enforced on creation paths, retention dry-run/apply tested.
7. Complete feature review and local regression checks — completed: four Important
   findings fixed earlier; this pass fixed recall evidence leakage, quota lock
   ordering/deadlock, credential grant-subset, and three stale test expectations
   plus the browser upload race; full suite 329/329. Hosted/external-service
   acceptance remains pending.

## Constraints and execution decisions

- Preserve the uncommitted Phase 1 work and staged snapshot; no commit, push or cloud migration.
- Work on `feature/shared-agent-memory-v1`; this repository has no initial commit, so a separate worktree cannot carry its current baseline. Preserve all files in place.
- Never store unapproved skill packages. Only owner-authenticated, explicitly approved exact-byte imports are supported.
- Keep custom reviewer construction outside this work.
- External model calls default disabled, spending cap zero. Do not use or print secrets from `.env`.
- Tests use disposable local databases; no tests against configured cloud data.
- Existing Phase 1 schema assertions will retain their original constraints and include additive V1 tables.
- Subsystems share one SQLAlchemy metadata; separate static migrations avoid changing `0001`.
- Subscription quota cannot currently be read reliably. Do not equate context usage with subscription remaining.
