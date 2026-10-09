"use client";
import { useCallback, useEffect, useState } from "react";
import Processing from "./processing";
import { request, message } from "@/lib/client";
type UsageData = {
  resources: Record<string, { used: number; limit: number | null }>;
  provider: {
    requests: number;
    reserved_usd: number;
    charged_usd: number;
    pending: number;
    measured_requests: number;
    estimated_requests: number;
    daily_budget_usd: number;
    cost_basis?: string;
  };
  jobs: Record<string, number>;
  pending_cleanup: number;
};
export default function Usage() {
  const [data, setData] = useState<UsageData | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await request("usage", "GET", undefined, signal);
      if (!signal?.aborted) setData(result);
    } catch (failure) {
      if (!signal?.aborted) setError(message(failure));
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);
  return (
    <>
      <Processing reload={() => load()} />
      <p className="subtle">
        Database counts and provider ledger values. Dollar costs use the
        configured price estimate; provider usage is measured where returned.
      </p>
      <button className="secondary" onClick={() => void load()}>
        Refresh usage
      </button>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      {data && (
        <>
          <h3>Storage and quotas</h3>
          <table className="usage-table">
            <thead>
              <tr>
                <th>Resource</th>
                <th>Used</th>
                <th>Configured limit</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data.resources).map(([name, value]) => (
                <tr key={name}>
                  <td>{name.replaceAll("_", " ")}</td>
                  <td>{value.used.toLocaleString()}</td>
                  <td>
                    {value.limit === null
                      ? "Unlimited"
                      : value.limit.toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>Provider budget</h3>
          <div className="usage-summary">
            <div>
              <span>Daily cap</span>
              <strong>${data.provider.daily_budget_usd}</strong>
            </div>
            <div>
              <span>Charged estimate</span>
              <strong>${Number(data.provider.charged_usd).toFixed(4)}</strong>
            </div>
            <div>
              <span>Reserved</span>
              <strong>${Number(data.provider.reserved_usd).toFixed(4)}</strong>
            </div>
            <div>
              <span>Requests</span>
              <strong>{data.provider.requests}</strong>
            </div>
          </div>
          <p className="subtle">
            Measured requests: {data.provider.measured_requests} · Estimated
            requests: {data.provider.estimated_requests} · Pending reservations:{" "}
            {data.provider.pending}
          </p>
          <p className="subtle">
            Cost basis: {data.provider.cost_basis ?? "configured prices"}. A
            zero cap keeps external spending disabled.
          </p>
          <h3>Processing jobs</h3>
          {Object.entries(data.jobs).map(([status, count]) => (
            <p key={status}>
              {status}: {count}
            </p>
          ))}
          <p>Pending storage cleanup: {data.pending_cleanup}</p>
        </>
      )}
    </>
  );
}
