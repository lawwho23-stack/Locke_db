"use client";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { message, request } from "@/lib/client";
type Scope = { id: string; name: string; capabilities: string[] };
type Connection = { id: string; display_name: string; token_prefix: string; revoked_at?: string; expires_at?: string; grants: {scope_id: string; capabilities: string[]}[] };
export default function Connections({ scope, scopes }: { scope: string; scopes: Scope[] }) {
  const [items, setItems] = useState<Connection[]>([]);
  const [caps, setCaps] = useState<string[]>(["memory:read", "task:read"]);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  const selected = scopes.find(item => item.id === scope);
  const load = useCallback(async () => { try { const data = await request("dashboard/connections"); if (alive.current) setItems(data.items); } catch (failure) { if (alive.current) setError(message(failure)); } }, []);
  useEffect(() => { alive.current = true; void load(); return () => { alive.current = false; }; }, [load]);
  useEffect(() => { setToken(""); setCaps(["memory:read", "task:read"].filter(cap => selected?.capabilities.includes(cap))); }, [scope, selected]);
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    setBusy(true); setError(""); setToken("");
    try {
      const data = await request("credentials", "POST", { display_name: form.get("name"), grants: [{ scope_id: scope, capabilities: caps }], expires_at: form.get("expires") ? new Date(String(form.get("expires"))).toISOString() : null });
      if (alive.current) { setToken(data.token); await load(); }
    } catch (failure) { if (alive.current) setError(message(failure)); } finally { if (alive.current) setBusy(false); }
  }
  async function revoke(item: Connection) {
    if (!confirm(`Revoke ${item.display_name}?`)) return;
    setBusy(true); setError("");
    try { await request(`credentials/${item.id}`, "DELETE"); if (alive.current) await load(); }
    catch (failure) { if (alive.current) setError(message(failure)); } finally { if (alive.current) setBusy(false); }
  }
  return <><p>Create one scoped credential per agent. Grant only the project and capabilities it needs.</p>{error && <p className="alert" role="alert">{error}</p>}<form className="upload" onSubmit={create}><label>Agent name<input name="name" maxLength={100} required placeholder="Codex · this project" /></label><fieldset><legend>Capabilities for {selected?.name}</legend><div className="capabilities">{selected?.capabilities.map(cap => <label key={cap}><input type="checkbox" checked={caps.includes(cap)} onChange={e => setCaps(current => e.target.checked ? [...current, cap] : current.filter(value => value !== cap))} />{cap}</label>)}</div></fieldset><label>Expiry (optional)<input name="expires" type="datetime-local" /></label><button disabled={busy || !scope || !caps.length}>Create scoped credential</button></form>{token && <div className="token-reveal"><h3>Save this credential now</h3><p>It is shown once and cannot be retrieved later.</p><code>{token}</code><button className="secondary" onClick={() => setToken("")}>I saved it · dismiss</button></div>}<div className="rows">{items.map(item => <div className="row" key={item.id}><strong>{item.display_name}</strong><p className="subtle">{item.token_prefix}… · {item.revoked_at ? "Revoked" : item.expires_at && new Date(item.expires_at) <= new Date() ? "Expired" : "Active"}{item.expires_at ? ` · Expires ${new Date(item.expires_at).toLocaleString()}` : ""}</p><p className="subtle">{item.grants.map(grant => `${scopes.find(s => s.id === grant.scope_id)?.name}: ${grant.capabilities.join(", ")}`).join("; ")}</p><button className="danger" disabled={busy || Boolean(item.revoked_at)} onClick={() => void revoke(item)}>Revoke credential</button></div>)}</div><div className="connection"><h3>Connect your coding client</h3><p>Configure the local MCP server with the new scoped credential, or use the Python API client and command line.</p><code>memory-mcp</code><code>memory --help</code><p className="subtle">Approved skill imports require owner approval of the exact package hash through the owner CLI.</p></div></>;
}
