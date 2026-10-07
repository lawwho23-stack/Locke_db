"use client";
import { useEffect, useState } from "react";
import { request, message } from "@/lib/client";
type JobData = { status: string; attempts: number; error_code?: string };
export default function Job({ id }: { id: string }) {
  const [data, setData] = useState<JobData | null>(null); const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController(); let finished = false;
    const update = async () => { try { const result = await request(`jobs/${id}`, "GET", undefined, controller.signal); if (!controller.signal.aborted) { setData(result); finished = ["completed", "failed", "cancelled"].includes(result.status); } } catch (failure) { if (!controller.signal.aborted) setError(message(failure)); } };
    void update(); const timer = setInterval(() => { if (!finished) void update(); }, 2000);
    return () => { clearInterval(timer); controller.abort(); };
  }, [id]);
  return <p className="subtle" role="status">Job {id.slice(0, 8)}: {data?.status ?? "queued"}{data ? ` · ${data.attempts} attempt(s)` : ""}{data?.error_code ? ` · ${data.error_code}` : ""}{error ? ` · ${error}` : ""}</p>;
}
