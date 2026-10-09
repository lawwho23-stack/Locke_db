"use client";
import { useRef, useState } from "react";
import { message, request } from "@/lib/client";
import Icon from "./icon";
export default function Processing({
  reload,
}: {
  reload?: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const running = useRef(false);
  async function process() {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setError("");
    let steps = 0;
    try {
      for (; steps < 10; steps++) {
        setStatus(`Processing step ${steps + 1}…`);
        const result = await request("jobs/process", "POST", {});
        if (!result.worked && !result.uploads_cleaned) {
          setStatus(
            steps
              ? `Finished ${steps} processing step(s). No jobs ready to run.`
              : "No jobs ready to run. Delayed retries may still be pending.",
          );
          break;
        }
      }
      if (steps === 10)
        setStatus(
          "Finished 10 steps. Run again to continue any remaining jobs.",
        );
      await reload?.();
    } catch (failure) {
      setError(message(failure));
      setStatus("Stopped. Completed steps are saved; you can retry.");
    } finally {
      running.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="processing">
      <button
        className="secondary"
        disabled={busy}
        onClick={() => void process()}
      >
        <Icon name="terminal" />
        {busy ? "Processing…" : "Process pending jobs"}
      </button>
      <p role="status">
        {status || "Run queued work now. Scheduled recovery runs daily."}
      </p>
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
