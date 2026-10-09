"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { apiUrl } from "@/lib/agent-setup.mjs";

function AuthorizeInner() {
  const params = useSearchParams();
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [githubEnabled, setGithubEnabled] = useState(false);

  const clientId = params.get("client_id") ?? "";
  const redirectUri = params.get("redirect_uri") ?? "";
  const scopeId = params.get("scope_id") ?? params.get("scope") ?? "";
  const caps = params.get("capabilities") ?? params.get("scope") ?? "memory:read";
  const state = params.get("state") ?? "";
  const challenge = params.get("code_challenge") ?? "";

  useEffect(() => {
    void fetch("/api/session", { cache: "no-store" })
      .then((r) => setSignedIn(r.ok))
      .catch(() => setSignedIn(false));
    void fetch(`${apiUrl}/oauth/config`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && data.github === true) setGithubEnabled(true);
      })
      .catch(() => {});
  }, []);

  function githubLoginUrl() {
    return `${apiUrl}/oauth/github/login?next=${encodeURIComponent(window.location.href)}`;
  }

  async function login() {
    setBusy(true);
    setError("");
    try {
      const r = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error?.message ?? "Sign-in failed.");
      setToken("");
      setSignedIn(true);
    } catch (f) {
      setError(f instanceof Error ? f.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    setBusy(true);
    setError("");
    try {
      const r = await fetch("/api/oauth/authorize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: clientId,
          redirect_uri: redirectUri,
          code_challenge: challenge,
          scope_id: scopeId,
          capabilities: caps,
          state,
        }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error?.message ?? data.error?.code ?? "Approval failed.");
      const target = new URL(data.redirect_uri ?? redirectUri);
      target.searchParams.set("code", data.code);
      if (state) target.searchParams.set("state", state);
      window.location.href = target.toString();
    } catch (f) {
      setError(f instanceof Error ? f.message : "Approval failed.");
    } finally {
      setBusy(false);
    }
  }

  function deny() {
    try {
      const target = new URL(redirectUri);
      target.searchParams.set("error", "access_denied");
      if (state) target.searchParams.set("state", state);
      window.location.href = target.toString();
    } catch {
      setError("Invalid redirect URI.");
    }
  }

  return (
    <main className="login-shell">
      <section className="login">
        <p className="eyebrow">LOCKE · CONNECT A CODING TOOL</p>
        <h1>Approve this connection?</h1>
        <dl>
          <dt>Client</dt>
          <dd>{clientId || "Unknown client"}</dd>
          <dt>Returns to</dt>
          <dd>{redirectUri || "Unknown address"}</dd>
          <dt>Scope</dt>
          <dd>{scopeId || "No scope selected"}</dd>
          <dt>Permissions</dt>
          <dd>{caps}</dd>
        </dl>
        {error && (
          <p className="alert" role="alert">
            {error}
          </p>
        )}
        {signedIn === false && (
          <>
            {githubEnabled && (
              <button disabled={busy} onClick={() => (window.location.href = githubLoginUrl())}>
                Continue with GitHub
              </button>
            )}
            <label>
              Owner credential
              <input
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="Paste your owner credential"
              />
            </label>
            <button disabled={busy || !token} onClick={login}>
              {busy ? "Checking…" : "Sign in as owner"}
            </button>
          </>
        )}
        {signedIn === true && (
          <div className="actions">
            <button disabled={busy || !clientId || !redirectUri || !challenge} onClick={approve}>
              {busy ? "Approving…" : "Approve connection"}
            </button>
            <button className="secondary" disabled={busy} onClick={deny}>
              Deny
            </button>
          </div>
        )}
        {signedIn === null && <p className="subtle">Checking owner session…</p>}
        <p className="subtle">
          A short-lived code is issued to this client only. Tokens expire and can be revoked from
          Connections at any time.
        </p>
      </section>
    </main>
  );
}

export default function OAuthAuthorizePage() {
  return (
    <Suspense fallback={<main className="loading">Loading approval…</main>}>
      <AuthorizeInner />
    </Suspense>
  );
}
