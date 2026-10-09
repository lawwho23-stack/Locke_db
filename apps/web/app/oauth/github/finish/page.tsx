"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

function FinishInner() {
  const params = useSearchParams();
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  useEffect(() => {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const token = fragment.get("token") ?? "";
    // Clear the fragment so the credential never stays in the address bar.
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    if (!token) {
      setError("GitHub sign-in did not return a credential. Start again.");
      return;
    }
    const next = params.get("next") ?? "/";
    void fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    })
      .then(async (r) => {
        const data = await r.json();
        if (!r.ok) throw new Error(data.error?.message ?? "Sign-in failed.");
        setDone(true);
        window.location.href = next;
      })
      .catch((f: unknown) => {
        setError(f instanceof Error ? f.message : "Sign-in failed.");
      });
  }, [params]);

  return (
    <main className="login-shell">
      <section className="login">
        <p className="eyebrow">LOCKE · GITHUB SIGN-IN</p>
        <h1>{error ? "Sign-in failed" : done ? "Signed in" : "Finishing sign-in…"}</h1>
        {error ? (
          <>
            <p className="alert" role="alert">
              {error}
            </p>
            <a href="/">Back to workspace</a>
          </>
        ) : (
          <p className="subtle">Saving your owner session…</p>
        )}
      </section>
    </main>
  );
}

export default function GithubFinishPage() {
  return (
    <Suspense fallback={<main className="loading">Finishing sign-in…</main>}>
      <FinishInner />
    </Suspense>
  );
}
