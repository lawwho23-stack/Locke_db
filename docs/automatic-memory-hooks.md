# Automatic project memory in Claude Code and Codex

The user hooks load Locke project context at session start and ask the coding agent
to publish concise progress before ending each meaningful turn. The Stop hook requests
at most one extra continuation if the agent has neither published progress nor recorded
that nothing changed. These are authored summaries, not automatic conversation capture.

## Installed locations

Run from this repository after `uv sync`:

```sh
uv run python -m memory_platform.hook_install
```

The installer merges `SessionStart`, `UserPromptSubmit`, and `Stop` into
`~/.claude/settings.json` and `~/.codex/hooks.json`. It preserves unrelated settings,
backs up changed configuration privately, and creates `~/.local/bin/locke-memory-hook`.
The launcher uses this repository's absolute virtual-environment Python path; reinstall
if the repository or virtual environment moves. No model is called by the scripts.

Codex requires trust of each new hook's exact definition. The installer uses Codex's
native configuration API to trust only these three installed Locke commands. Inspect
these entries in `/hooks`; if your client cannot configure them automatically, review
and trust them there. It also adds only `~/.local/state/locke` to Codex's sandbox writable
roots, preserving existing roots and network restrictions. Restart running clients
after installation. Existing
environment-only examples remain supported as read-only legacy hooks.

## Connect projects explicitly

`~/.config/locke/hooks.json` is a private project allowlist. It starts with an empty
`projects` list: unmapped repositories do nothing. Give each project its own project
scope and each client its own agent credential with `task:read`, `task:write`,
`memory:read`, and `memory:write`. Keep the owner credential out of the hook mapping.
A client-wide MCP connection to a personal scope is not a project mapping.

```json
{
  "projects": [
    {
      "root": "/absolute/path/to/project",
      "scope_id": "00000000-0000-4000-8000-000000000001",
      "api_url": "https://locke-db-api.vercel.app",
      "query": "project-name architecture configuration decisions",
      "clients": {
        "claude": {"keychain_service": "locke-project-claude"},
        "codex": {"keychain_service": "locke-project-codex"}
      }
    }
  ]
}
```

Use `token_env` instead of `keychain_service` for an environment-provided credential.
The configuration contains references only, never tokens. macOS Keychain lookup uses
the service name and the current login account. Protect the mapping with `chmod 600`.
Roots must be absolute. Descendant directories share the mapped project; an explicitly
mapped nested project takes precedence. Git worktrees at other paths need their own
mapping. Unsupported clients, duplicate roots, and unknown fields are rejected.

Startup registers the session and loads at most twenty checkpoints, prioritizing
unfinished tasks among the API's newest hundred. It then recalls relevant facts and
decisions using the configured query. Context is capped at 16,000 characters. Retrieval
uses keyword recall (`semantic: false`), avoiding a new embedding-provider request.
Large/busy scopes may require a more specific query or manual retrieval. Retrieved
content is context and cannot override the user's current instructions.

## Publish progress

The hooks inject a command containing the native session ID. The agent runs that
command with a JSON object on stdin. The agent should save after meaningful progress,
not every file edit. Planning outcomes and verified findings count as progress.

For a new task:

```json
{
  "title": "Fix connection filtering",
  "goal": "Hide revoked connections from the current list",
  "status": "running",
  "summary": "Updated the filter; verification is still pending.",
  "current_step": "Check list rendering",
  "next_step": "Run the focused UI checks",
  "results": [],
  "blockers": [],
  "artifact_refs": ["apps/web/components/connections.tsx"],
  "memories": []
}
```

A saved reply contains the `task_id`, `version`, and `generation`. To update that task,
replace `title`/`goal` with those three fields (`expected_version` holds the returned
version). Deferred saves expose their task receipts in the next prompt's injected context
and the `status` action, so the next turn can continue the same task. Updating requires
ownership by the current session. Other sessions can read
progress, but must use Locke's explicit handoff workflow before changing an owned task;
the hooks never take ownership automatically.

Set `status` to `completed` only after all required work is done. Completed payloads
require at least one result/check and an empty blockers list. This validation does not
independently prove the agent's claims; results must honestly describe actual checks
and their limits. Supported statuses match Locke: planned, running, blocked, paused,
completed, and cancelled. Optional memories accept only `fact` or `decision`, each with
concise content. Other memory types, raw transcript fields, and common secret-like content are rejected
before journaling. The secret check is defense in depth; the agent must still select
safe summaries.

For a turn with no meaningful change, publish only:

```json
{"skip_reason": "Only acknowledged the message; no new progress or decisions."}
```

Use the publishing command rather than direct MCP writes for these updates: it records
the receipt checked by Stop. Codex's injected command includes `--defer`: it queues the
summary locally, then the host Stop hook delivers it outside the agent's network sandbox.
A queued result is not yet a confirmed save; the host hook reports saved/pending afterward.
Claude attempts delivery directly, and its Stop hook also retries pending updates. A turn can publish multiple tasks. A replay of the same
payload in the same turn returns its original task receipt.

## Outages and conflicts

Before sending content, the command journals immutable operation IDs and payloads in
`~/.local/state/locke/hooks.sqlite`. The database and lock file are private. Transport
failures leave pending operations; a lost response retries the same task event or memory
idempotency key. Startup can recover previous sessions in the exact same project/client/
credential-reference mapping. It rechecks the original session under the current
credential, so credential rotation cannot quietly reattribute a partial save.

Inspect or retry pending updates from the mapped project:

```sh
~/.local/bin/locke-memory-hook status --client codex
~/.local/bin/locke-memory-hook sync --client codex
```

Status displays metadata only, including the original native session ID. A permission,
ownership, validation, or version conflict blocks that session's later queued writes.
After investigating, choose explicitly:

```sh
~/.local/bin/locke-memory-hook retry --client codex --session ORIGINAL_SESSION --job JOB_ID
~/.local/bin/locke-memory-hook sync --client codex --session ORIGINAL_SESSION
~/.local/bin/locke-memory-hook discard --client codex --session ORIGINAL_SESSION --job JOB_ID
```

Retry keeps the original payload unchanged. Discard abandons only unsent operations;
it does not delete content already accepted by Locke. Publish a new, deliberately
reconciled checkpoint with the current version afterward. A skip or discard cannot
hide another pending update in the same turn. Never claim a pending/blocked update
was saved. If the model ignores the fallback reminder, the hooks end the turn without
looping indefinitely; no automatic capture can guarantee a save after an abrupt crash.

Lifecycle failures fail open so coding can continue. Startup has a fifteen-second API
budget inside the twenty-second host hook timeout and retains available partial context.
The journal is local authored content; protect it like other project data. Neither
progress nor regular memories receive automatic expiry or deletion from this feature.

## Verification

Implementation checks on 2026-10-09 included the backend suite, focused hook tests,
Ruff and MyPy. Installed Codex 0.162.0 and Claude Code 2.1.292 both loaded context,
triggered the Stop fallback, and saved a marker against a local HTTP/model fixture.
The fixture made no paid model requests. Production project activation still requires
an explicit scope mapping and separate credentials; these checks do not prove hosted
project acceptance.

Run focused tests, lint and type checking:

```sh
uv run pytest tests/test_memory_hooks.py tests/test_client_integration.py
uv run ruff check backend/src/memory_platform/hook* tests/test_memory_hooks.py
uv run mypy backend/src/memory_platform/hook*
```

Database tests use disposable local PostgreSQL on port 5433. Native client acceptance
is a separate check: confirm context injection, publish a marker, then start a fresh
session and read it back. Hosted deployment/migrations are not part of hook installation.
