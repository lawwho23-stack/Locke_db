# Private dashboard on Vercel Hobby

The UI and hosting code are delivered together, but production activation is a separate
acceptance step. Existing hosted frontend: `lawwho23-stacks-projects/locke_db-web`,
root `apps/web`, production origin `https://lockedb-web.vercel.app`.
Backend project: `locke-db-api`, deployed from `backend` as a standalone FastAPI project
(project root `.` relative to the uploaded directory). Its checked standalone `uv.lock`
and `.python-version` keep deployment at the tested Python/dependency versions.
Regenerate both root and standalone locks when updating backend dependencies.
Both project configs use `sin1` to stay near the current Neon database.

## Runtime configuration

| Project | Required configuration |
| --- | --- |
| Backend | `DATABASE_URL` (runtime role), `MEMORY_API_URL=https://locke-db-api.vercel.app`, existing `MEMORY_HMAC_KEY`, `STORAGE_PROVIDER=vercel_blob`, `BLOB_READ_WRITE_TOKEN`, `CRON_SECRET`, `CRON_WORKSPACE_ID` |
| Frontend | `MEMORY_API_URL` (backend HTTPS origin), `WEB_ORIGIN=https://lockedb-web.vercel.app`, stable `SESSION_SECRET`, `STORAGE_PROVIDER=vercel_blob`, same private `BLOB_READ_WRITE_TOKEN` |

The HMAC key must remain unchanged. Set migration credentials only in the local admin
execution environment, not the dashboard. Keep all tokens server-only. No `NEXT_PUBLIC`
secret variables. Configure separate Preview Neon and Blob environments; do not copy
production credentials into Preview. `WEB_ORIGIN` must match each environment's exact
host; stable preview aliases avoid unpredictable origins.

Enable Fluid Compute so the checked 300-second Hobby duration applies. The dashboard
proxy waits up to 270 seconds for finalization and processing; regular reads retain a
30-second limit. Expired backend leases recover jobs killed by a function timeout.
Production agent API access requires the backend to accept external requests through
application bearer authentication; deployment protection must not silently block agents.
Verify the intended protection settings before changing them.

## Private originals

Create a **private** Blob store and connect the same store to both production projects.
Originals are keyed by application-generated UUIDs under `sources/`; no public URLs are
accepted by finalization. An expiring receipt binds caller, workspace, scope, filename,
size and optional replacement version. Direct browser upload bypasses the 4.5 MB
Function request limit. Finalization verifies bytes, parsing limits, current permission,
quota and source version, and publishes one durable source/job transaction.

Keep 20 MiB and 300-page limits. Failed/interrupted uploads expire after 15 minutes.
Recovery removes at most one expired unfinalized original per invocation after a one-hour
grace period. Finalized originals are cleaned through existing durable source deletion.
Receipts retain completed response metadata for safe retries. No provider text is stored
in them. Local development retains filesystem storage and the original base64 routes.
Existing base64 clients remain subject to Vercel's request-size limit; use the dashboard's
direct upload for larger documents.

As checked on 2026-10-07, Hobby Blob includes 1 GB storage, 10 GB transfer, 10,000 simple
operations and 2,000 advanced operations. Hobby does not charge automatic overages;
Blob becomes unavailable after limits are exceeded. Do not enable Pro/trials as part of
this setup. Check current terms: https://vercel.com/docs/vercel-blob/usage-and-pricing.
Cloud model-provider spending is separate; automated checks use a zero daily provider
budget and no real model calls. The user's configured budget remains unchanged.

## Jobs and maintenance

The owner can run ten processing steps with **Process pending jobs**, then run it again
for more work. Each backend request processes one durable job and at most one expired
upload. Source enrichment saves up to eight chunk embeddings and releases the lease
without using a failure attempt, then resumes from saved chunks. Real failures still use
bounded retries. Processing requires owner administration and full workspace grants;
a narrowed credential cannot operate on hidden scopes.

Daily recovery is `GET /internal/cron` at `0 3 * * *` (UTC), authenticated with `CRON_SECRET`.
It processes only `CRON_WORKSPACE_ID`. The schedule is a recovery pass, not a continuous
worker: queued jobs need repeated on-demand processing for larger backlogs. Never put
the recovery endpoint in the dashboard proxy allowlist. Cron is unavailable to agents.

## Migration and acceptance order

1. Back up the current Neon database with the unchanged HMAC key. Confirm the target.
2. Apply migration `0006_hosted_uploads` using the direct migration connection. It adds
   upload receipts and repairs the valid knowledge-job kind constraint in existing DBs.
3. Create/configure the private store and backend project; deploy the backend.
4. Verify `/health`, authenticated `/ready`, permissions, cron rejection without a secret,
   and external agent access before pointing the frontend at it.
5. Deploy the redesigned frontend; verify owner login, mobile navigation, all eight
   sections, a near-20-MiB direct upload, replacement conflict, processing and deletion.
6. Use a deliberately approved, scoped smoke credential for production writes. Local
   deterministic acceptance is not evidence that the external Blob integration works.

Backup export now reads through the configured storage adapter. Run it locally with
`STORAGE_PROVIDER=vercel_blob` and the same private store credentials. Export includes
referenced originals and upload receipt metadata. Restore remains limited to an empty
local database and local originals: inspect that restored snapshot before deciding to
restore cloud storage. Store backup passphrase and HMAC key separately from public code.

Code rollback: redeploy the previous frontend/backend. Migration 0006 is additive; leave
it in place during an application rollback. Never delete a store or downgrade a cloud
database as part of rollback. Keep backend and frontend URL/configuration changes paired.

## Activation evidence (2026-10-07)

Applied migration 0006 to the confirmed Neon database after encrypted backup; verified
backup authentication and every table checksum. Runtime receipt-table grants verified.
Created private `locke-private-originals` (`store_WzYyGLXXCzrudmSF`) in Singapore and
configured both production runtimes. Python adapter put/get/delete round-trip passed.
Backend `https://locke-db-api.vercel.app` is deployed in `sin1`: `/health` returns 200,
workspace reads without credentials return 401, and secret-authenticated recovery
returns 200 with no pending jobs. Daily cron configuration confirmed in deployment metadata.
Provider budget and existing HMAC key preserved. No paid plan activated.
The pre-existing frontend returned 503 because its cookie-encryption `SESSION_SECRET`
was shorter than 32 characters (confirmed with value-free runtime diagnostics).
Replaced that invalid production cookie key with a generated secret; database owner
credentials remain unchanged. Preview resources remain unconfigured for this rollout.

Local acceptance: 345 Python tests, 7 Node tests, Ruff, MyPy, TypeScript, Next build;
real Chrome login, all sections, inspector/confirmation, local upload/processing,
390px and 320px navigation checks passed. Live owner login, direct browser upload and
replacement/delete acceptance still require the requested temporary credential approval.

Frontend promoted to `https://lockedb-web.vercel.app` (`dpl_89cXaM4S7Td3f8PffqzPrFTHCgLa`).
Public homepage 200, unauthenticated session/upload-mode/workspace proxy 401, invalid
owner login 403, and cross-origin upload 403 verified. Code remains uncommitted on
`feature/locke-dark-theme`; no repository push or automatic backend Git connection.

Production Chrome sign-in rendering verified at 1440px and 320px with no JavaScript
exceptions or horizontal overflow; screenshots under ignored `.data/ui-preview/`.
Existing DB owner is active with full scope grants. User data remains unchanged:
zero memories, sources, tasks, skills, and upload receipts after deployment checks.
Real model-provider calls were not part of acceptance; no test credential was minted.
