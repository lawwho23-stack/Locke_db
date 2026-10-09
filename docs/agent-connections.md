# Connect an agent to Locke

Sign in as the owner, choose the intended scope, and open **Connections**.
Choose Claude Code, Codex, OpenCode, or Custom Agent. Create a separate credential
for each agent. Read/write is selected by default; it permits creation and editing,
not deletion. Scope grants are enforced by the API.

Save the token privately when it is shown. The configuration and starter prompt
contain no token: supply `LOCKE_AGENT_TOKEN` to the process that launches your
client. Merge the generated configuration rather than replacing existing settings:

- OpenCode (one-click OAuth): add `https://locke-db-api.vercel.app/mcp` as a
  remote MCP server with OAuth enabled. OpenCode discovers
  `/.well-known/oauth-protected-resource`, registers itself at `/oauth/register`,
  and opens the dashboard approval page (`/oauth/authorize`). Sign in with
  GitHub or paste the owner credential, approve the scope, and the client
  receives short-lived access plus rotating refresh tokens. No token copy-paste.
  Revoke any time from Connections.
- Claude Code: project `.mcp.json`. Approve the server when the client requests it.
- Codex: the `mcp_servers.locke` section of `~/.codex/config.toml`.
- Custom: map the descriptor to your Streamable HTTP MCP client, supplying
  `Authorization: Bearer <token>` on every request. The Python example is a REST
  alternative and requires installing this repository's `memory-platform` package.
  OAuth access tokens (`mcp_at_...`) work on both REST and MCP; long-lived
  `mem_...` tokens keep working.

Start the client with the variable available. Existing desktop processes may not
inherit a newly exported variable; use your host's supported environment setup and
restart it. Config listing alone does not prove authentication. Initialize MCP,
list tools, then recall in the displayed scope. Paste the starter prompt into the
agent session to explain how to use memory.

For write retries, reuse the same `idempotency_key` and payload. Agent facts and
decisions start active; experiences and other types may remain drafts awaiting the
owner. Do not store secrets or assume connecting MCP makes the client automatically
recall context. Shared personality/rule storage is outside this release.

## GitHub sign-in setup

GitHub login is owner-only. Only the `OWNER_EMAIL` address can sign in; every
other GitHub account gets 403. No public signup exists.

1. Create a GitHub OAuth App. Set its callback URL to
   `https://locke-db-api.vercel.app/oauth/github/callback` (production). For
   local testing, create a second OAuth App with
   `http://127.0.0.1:8000/oauth/github/callback`, since GitHub allows one
   callback URL per app.
2. Set backend env vars (never commit them): `GITHUB_CLIENT_ID`,
   `GITHUB_CLIENT_SECRET`, `OWNER_EMAIL` (your GitHub account email),
   `OAUTH_AUTHORIZE_URL=https://lockedb-web.vercel.app/oauth/authorize`.
3. Redeploy the backend. The approval page shows Continue with GitHub only when
   the backend reports it at `GET /oauth/config`.
4. Sign-in mints a 12-hour owner credential (`GITHUB_OAUTH_TTL_HOURS`). It
   appears in Connections like any credential and can be revoked there. The
   plain token travels once, in the URL fragment, straight into the encrypted
   owner session. GitHub tokens are never stored.

## Hosting

The backend serves stateless Streamable HTTP at `/mcp` with JSON responses. Set its
`MEMORY_API_URL` to the stable HTTPS backend origin and redeploy. It forwards each
request with that caller's credential to the existing REST authorization layer.
The SDK lifespan runs with FastAPI's lifespan; no persistent MCP session is needed.
The existing stdio and standalone local HTTP bridges remain supported.

The URL in generated configurations is the production backend. Protocol tests use
only disposable Docker databases; live acceptance uses temporary scoped credentials
and removes test memories/revokes credentials afterward. Actual client acceptance
must be reported separately from SDK protocol acceptance.
