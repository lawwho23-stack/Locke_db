"use client";
import { useConfirm } from "./confirmation";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { message, request } from "@/lib/client";
import { agentClients, agentSetup, mcpUrl } from "@/lib/agent-setup.mjs";
type Scope = { id: string; name: string; capabilities: string[] };
type Connection = {
  id: string;
  display_name: string;
  token_prefix: string;
  revoked_at?: string;
  expires_at?: string;
  grants: { scope_id: string; capabilities: string[] }[];
};
export default function Connections({
  scope,
  scopes,
}: {
  scope: string;
  scopes: Scope[];
}) {
  const confirm = useConfirm();
  const [items, setItems] = useState<Connection[]>([]);
  const [caps, setCaps] = useState<string[]>(["memory:read", "memory:write"]);
  const [client, setClient] = useState("claude");
  const [issued, setIssued] = useState<{ scope: string; caps: string[]; client: string } | null>(null);
  const [notice, setNotice] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const issuance = useRef(0);
  const selected = scopes.find((item) => item.id === scope);
  const load = useCallback(async () => {
    try {
      const data = await request("dashboard/connections");
      if (alive.current) setItems(data.items.filter((item: Connection) => !item.revoked_at));
    } catch (failure) {
      if (alive.current) setError(message(failure));
    }
  }, []);
  useEffect(() => {
    alive.current = true;
    void load();
    return () => {
      alive.current = false;
    };
  }, [load]);
  useEffect(() => {
    issuance.current += 1;
    setToken("");
    setIssued(null);
    setNotice("");
    setCaps(
      ["memory:read", "memory:write"].filter((cap) =>
        selected?.capabilities.includes(cap),
      ),
    );
  }, [scope]);
  const kit = token && issued && issued.scope === scope
    ? agentSetup(issued.client, issued.scope, issued.caps) : null;
  function dismiss() {
    issuance.current += 1;
    setToken(""); setIssued(null); setNotice("");
  }
  async function copy(value: string) {
    try { await navigator.clipboard.writeText(value); setNotice("Copied."); }
    catch { setNotice("Copy unavailable. Select and copy the text manually."); }
  }
  function download(filename: string, content: string) {
    const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url; link.download = filename; link.click();
    URL.revokeObjectURL(url);
  }
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    setToken("");
    setIssued(null);
    setNotice("");
    const generation = ++issuance.current;
    const issuedScope = scope;
    const issuedClient = client;
    try {
      const data = await request("credentials", "POST", {
        display_name: form.get("name"),
        grants: [{ scope_id: scope, capabilities: caps }],
        expires_at: form.get("expires")
          ? new Date(String(form.get("expires"))).toISOString()
          : null,
      });
      if (alive.current && generation === issuance.current && currentScope.current === issuedScope) {
        setToken(data.token);
        setIssued({ scope: issuedScope, caps: data.grants[0].capabilities, client: issuedClient });
        await load();
      } else if (alive.current) {
        setError("A credential was created for the previous scope. Its token was cleared. Revoke it from the connection list if you did not save it.");
        await load();
      }
    } catch (failure) {
      if (alive.current) setError(message(failure));
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  async function revoke(item: Connection) {
    if (!(await confirm(`Revoke ${item.display_name}?`))) return;
    setBusy(true);
    setError("");
    try {
      await request(`credentials/${item.id}`, "DELETE");
      if (alive.current) {
        setItems((current) => current.filter((connection) => connection.id !== item.id));
        await load();
      }
    } catch (failure) {
      if (alive.current) setError(message(failure));
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  return (
    <>
      <p>
        Create one scoped credential per agent. Grant only the project and
        capabilities it needs.
      </p>
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      <form className="upload" onSubmit={create}>
        <label>
          Connect with
          <select value={client} onChange={(event) => setClient(event.target.value)} disabled={busy}>
            {agentClients.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <label>
          Agent name
          <input
            name="name"
            maxLength={100}
            required
            placeholder="Codex · this project"
          />
        </label>
        <fieldset>
          <legend>Capabilities for {selected?.name}</legend>
          <div className="capabilities">
            {selected?.capabilities.map((cap) => (
              <label key={cap}>
                <input
                  type="checkbox"
                  checked={caps.includes(cap)}
                  onChange={(e) =>
                    setCaps((current) =>
                      e.target.checked
                        ? [...current, cap]
                        : current.filter((value) => value !== cap),
                    )
                  }
                />
                {cap}
              </label>
            ))}
          </div>
        </fieldset>
        <p className="subtle">Read/write allows creating and editing memories in this scope. Deletion requires a separate permission.</p>
        <label>
          Expiry (optional)
          <input name="expires" type="datetime-local" />
        </label>
        <button disabled={busy || !scope || !caps.length}>
          Create scoped credential
        </button>
      </form>
      {kit && issued && (
        <div className="token-reveal">
          <h3>Save this credential now</h3>
          <p>It is shown once and cannot be retrieved later.</p>
          <code>{token}</code>
          <button className="secondary" onClick={() => void copy(token)}>Copy credential</button>
          <h3>Connect {agentClients.find((item) => item.id === issued.client)?.name}</h3>
          <p>In the terminal you use to launch this client, paste the credential when the command waits, then press Enter:</p>
          <pre><code>{"read -r -s LOCKE_AGENT_TOKEN\nexport LOCKE_AGENT_TOKEN"}</code></pre>
          <p className="subtle">Start the client from that terminal so it receives the variable. Restart existing sessions. Keep each agent’s credential separate. Never commit the token.</p>
          <p>{issued.client === "custom" ? "Generic connection descriptor: map these fields to your MCP client's API." : `Merge this into your existing ${kit.filename}; preserve other servers and settings.`}</p>
          <pre><code>{kit.config}</code></pre>
          <button className="secondary" onClick={() => void copy(kit.config)}>Copy configuration</button>
          <button className="secondary" onClick={() => download(kit.filename, kit.config)}>Download configuration</button>
          {issued.client === "custom" && <>
            <p>Python API alternative: install the memory-platform Python package from this repository first. It uses REST, without an MCP runtime.</p>
            <pre><code>{kit.python}</code></pre>
            <button className="secondary" onClick={() => void copy(kit.python)}>Copy Python example</button>
          </>}
          <h3>Starter prompt</h3>
          <p>Paste this into the agent’s session. Connecting MCP makes tools available; this prompt explains when to use them.</p>
          <pre><code>{kit.prompt}</code></pre>
          <button className="secondary" onClick={() => void copy(kit.prompt)}>Copy starter prompt</button>
          <button className="secondary" onClick={() => download("locke-starter-prompt.md", kit.prompt)}>Download starter prompt</button>
          <h3>Test your connection</h3>
          <ol>
            <li>Restart the client with LOCKE_AGENT_TOKEN available.</li>
            <li>Confirm Locke connects and lists memory tools ({issued.client === "claude" ? "/mcp in Claude Code" : issued.client === "codex" ? "codex mcp list, then start a session" : issued.client === "opencode" ? "opencode mcp list" : "initialize and tools/list in your MCP client"}). Listing a saved configuration alone does not prove authentication.</li>
            <li>Ask the agent to recall context in scope {issued.scope}. A new scope may return no memories.</li>
            {issued.caps.includes("memory:write") && <li>Ask it to save a harmless fact, then recall that fact. Do not use secrets as a test.</li>}
          </ol>
          <p role="status">{notice}</p>
          <button className="secondary" onClick={dismiss}>
            I saved it · dismiss
          </button>
        </div>
      )}
      <div className="rows">
        {!items.length && <p className="subtle">No credentials to show. Create one above to connect an agent.</p>}
        {items.map((item) => (
          <div className="row" key={item.id}>
            <strong>{item.display_name}</strong>
            <p className="subtle">
              {item.token_prefix}… ·{" "}
              {item.revoked_at
                ? "Revoked"
                : item.expires_at && new Date(item.expires_at) <= new Date()
                  ? "Expired"
                  : "Active"}
              {item.expires_at
                ? ` · Expires ${new Date(item.expires_at).toLocaleString()}`
                : ""}
            </p>
            <p className="subtle">
              {item.grants
                .map(
                  (grant) =>
                    `${scopes.find((s) => s.id === grant.scope_id)?.name}: ${grant.capabilities.join(", ")}`,
                )
                .join("; ")}
            </p>
            <button
              className="danger"
              disabled={busy || Boolean(item.revoked_at)}
              onClick={() => void revoke(item)}
            >
              Revoke credential
            </button>
          </div>
        ))}
      </div>
      <div className="connection">
        <h3>One-click connect (OpenCode)</h3>
        <p>
          Paste this URL into OpenCode as a remote MCP server with OAuth enabled.
          OpenCode opens a login link; sign in here as owner and approve the scope.
          No token copy-paste is needed.
        </p>
        <code>{mcpUrl}</code>
        <button className="secondary" onClick={() => void copy(mcpUrl)}>Copy MCP URL</button>
        <p className="subtle">
          Flow: OpenCode discovers OAuth metadata → registers itself → opens the approval
          page → receives a short-lived code → swaps it for access and refresh tokens.
          Revoke any time by revoking the credential below or via OAuth revocation.
        </p>
        <h3>Manual tokens (Claude Code, Codex, custom)</h3>
        <p>
          Create a credential above to get client configuration and a starter prompt.
          Hosted MCP works from your Mac or a remote server. The local bridge remains available.
        </p>
        <code>memory-mcp</code>
        <code>memory --help</code>
        <p className="subtle">
          Approved skill imports require owner approval of the exact package
          hash through the owner CLI.
        </p>
      </div>
    </>
  );
}
