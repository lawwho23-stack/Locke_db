# Phase 1 continuation

Plan: approved continuation plan in the October 6, 2026 conversation.
Authority: `phase1-contract.md`, including its binding section 11 decisions.

- Baseline: formatting, lint and mypy passed; 18 tests passed, seven database tests could not connect while Docker was stopped.
- Ruling: preserve the existing staged, unborn repository in place. A worktree needs a commit, and the user requested no commits or unrelated Git changes.
- Ruling: use two workers with disjoint file ownership under the parallel-agent skill; root integrates and owns memory services, HTTP routes, smoke verification and documentation.
- Shared interfaces: `Principal`, the dependency aliases and `WriteOutcome` retain the saved contract. Routes open transactions; services never commit.
- Complete: restored local Docker test DB; implemented authentication/policy, write wrapper/admin, memory lifecycle and API.
- Review reproduced suppression/write races, reversed fact-key row-lock deadlocks, and malformed cursor types causing 500.
- Ruling: take a shared scope transaction advisory lock for remember and an exclusive one for update/forget before memory-row locks. This closes the forget barrier and orders colliding updates while preserving concurrent remember fact-key race responses. Updates and deletes in the same scope serialize; this is acceptable for the Phase 1 workload.
- Ruling: owner promotion records owner assertion trust and evidence; ordinary edits retain the original trust, as the contract does not specify changing it.

No cloud database, deployment, commit or push is part of this continuation.

## Final acceptance

- `uv run pytest -o addopts='' -q --tb=short`: 220 passed.
- `uv run ruff format --check .`: 56 files already formatted.
- `uv run ruff check .`: all checks passed.
- `uv run mypy backend/src`: no issues in 35 source files.
- `uv run python scripts/smoke_phase1.py`: passed through real CLI and Uvicorn HTTP; Alembic reported no schema drift. The script stopped its server and dropped its owned database.
- `uv run memory-admin --help`: installed CLI entry point works.
- Independent review found three defects; red regression tests reproduced them, fixes passed, and re-review found no remaining blocker in those fixes.
- Protected Wave 1 fixtures, migration, tables, schemas, contract and lockfile remain unchanged. `.env` is not staged. Existing Git index preserved; implementation remains local and uncommitted.

Learning: retries replay a committed metadata response rather than repeat a write; expected versions prevent silent overwrites; transaction locks protect the gap between checking a forgotten-content barrier and writing.

Later phases and live cloud deployment acceptance have not been implemented or claimed.
