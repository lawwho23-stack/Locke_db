# Private Dashboard Implementation Plan

Goal: completely redesign the private dashboard and adapt the existing stack for Vercel Hobby.
Spec: ../specs/2026-10-07-private-dashboard-design.md

- [x] Redesign shared shell, all sections, inspector, confirmations, and responsive states.
- [x] Add private storage adapter, upload tickets/direct-upload finalization and regression tests.
- [x] Add workspace-bounded processing, authenticated daily recovery and regression tests.
- [x] Add deployment configuration and operational documentation; check available accounts/costs.
- [x] Run full local checks and browser acceptance; review and resolve material findings.
- [x] Deploy after storage costs and intended external targets are confirmed; report live gaps.

## Execution ledger

Ruling: work in the existing feature/locke-dark-theme checkout and preserve next-env.d.ts —
user has existing work here; moving it risks dropping that context. No commits or pushes assumed.

Ruling: use separate Next.js and FastAPI Vercel projects — stable framework deployment with
an explicit server-only backend URL rather than depending on beta multi-service routing.

Ruling: direct upload uses expiring database receipts and authorized Blob client tokens — permits retries,
prevents reuse across scopes/replacements, and avoids trusting client-supplied URLs.

UI: sidebar/login/structured inspector/shared confirmation/mobile panels implemented;
frontend typecheck and build passed. Meaningful browser regressions added to existing Chrome acceptance.
Storage: receipt/finalization, original adapters, backup export and near-20-MiB test passed.
Processing: workspace isolation, secret cron, abandoned originals and resumable batch tests passed.
Ruling: owner processing requires full workspace source/write/delete grants — otherwise a
narrowed owner credential could mutate hidden job targets. Cost: narrowed owners must use
full owner credential for processing; agent credentials remain unable to process jobs.
External state: Vercel account is Hobby; existing frontend project confirmed. No Blob
stores were available in the initial team inventory. Production activation is in progress.

Final checks: 345 backend tests including real Chrome owner/mobile acceptance passed;
7 frontend policy/upload recovery tests passed; Ruff, MyPy, TypeScript and Next build passed.
Review: resolved lost upload-response recovery with same-receipt finalization and regression
tests. Also strengthened replacement-version database CHECK against SQL NULL semantics.

Production: encrypted pre-0006 snapshot verified; applied additive migration and runtime
grants checked. Private store and standalone backend activated; health, auth denial,
secret recovery and cloud adapter round-trip verified. Fixed pre-existing invalid
production cookie key after diagnostic proof; frontend promoted to https://lockedb-web.vercel.app.
Public production session/auth checks passed after cookie-key repair. Live owner login
and direct browser upload acceptance remain pending temporary credential approval.
