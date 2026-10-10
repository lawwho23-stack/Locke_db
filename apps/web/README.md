# Owner dashboard

Run the backend first, then run this application locally:

```sh
npm install
npm run dev
```

Generate one secret once and reuse it. Example (first setup only):

```sh
openssl rand -hex 32
```

Put that value in `.env.local` as `SESSION_SECRET` and keep it. Generating a
new secret logs out every session. Do not commit it.

Open `http://127.0.0.1:3000`. Sign in with an owner admin bearer credential that has explicit grants on the scopes you want to inspect. The bearer is sealed in an encrypted HttpOnly, SameSite Strict cookie; it is never returned to client code or stored in localStorage.

Server-only configuration:

- `MEMORY_API_URL`: defaults to `http://127.0.0.1:8000`.
- `WEB_ORIGIN`: defaults to `http://127.0.0.1:3000`. Write requests must match this origin exactly. Change it when changing the host or port.
- `SESSION_SECRET`: required, at least 32 random characters. Keep it stable: changing it logs out every session. Do not commit it. Owner sessions have no time expiry; they survive refresh, tab close, and browser restart until Sign out, credential revoke, or secret rotation.

HTTPS is required when WEB_ORIGIN is outside localhost. Cookie Secure is enabled for HTTPS. The Next.js process defaults to the loopback interface. No signup or public deployment is configured.

The dashboard reads bounded lists and a maximum of 100 graph nodes / 200 edges. It supports memory edits/forget, source upload/delete, task checkpoints/history inspection, approved skill downloads/revocation, and metadata activity. Owner-only approved skill import remains in the CLI. Graph layout is a deterministic grid; it does not suggest new relationships.

Verification: `npm test`, `npm run typecheck`, and `npm run build`.
