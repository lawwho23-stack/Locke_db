"use client";
import { FormEvent, useState } from "react";
import { encodeFile, message, request } from "@/lib/client";
export default function SourceReplacement({ id, version, reload }: { id: string; version: number; reload: () => Promise<void> }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  async function replace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const file = new FormData(event.currentTarget).get("document") as File;
    if (!file?.size || file.size > 20 * 1024 * 1024) { setError("Choose a document up to 20 MB."); return; }
    setBusy(true); setError("");
    try { await request(`sources/${id}`, "PUT", { expected_version: version, filename: file.name, content_base64: await encodeFile(file) }); await reload(); }
    catch (failure) { setError(message(failure)); } finally { setBusy(false); }
  }
  return <form className="upload" onSubmit={replace}><label>Replace document<input name="document" type="file" accept=".pdf,.md,.txt,.docx" required /></label><button disabled={busy}>Upload replacement</button><p className="subtle">Previous ready content stays searchable until processing succeeds.</p>{error && <p role="alert">{error}</p>}</form>;
}
