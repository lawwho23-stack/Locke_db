# Private dashboard design

Approved in conversation: complete dark developer workspace redesign, owner-only access,
Next.js and FastAPI on Vercel Hobby, existing Neon, private direct uploads up to 20 MiB,
on-demand bounded jobs and daily scheduled recovery. No signup or billing.

Charcoal surfaces, blue accent, persistent sidebar, compact lists, structured inspector,
responsive navigation and detail panels. Cover login and all eight dashboard sections.
Preserve permission checks, version fencing, approved-only skills, and server-only tokens.

Upload authorization binds a generated UUID storage key to credential/workspace/scope,
filename and optional expected source version. Finalization rechecks authorization and
file limits. Keep existing local/base64 API for clients; hosted dashboard uses direct Blob
uploads. Processing keeps the existing leased database queue and exposes no job secrets.
