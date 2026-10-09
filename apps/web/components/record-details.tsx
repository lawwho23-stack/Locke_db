"use client";
function readable(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}
export default function RecordDetails({
  data,
}: {
  data: Record<string, unknown>;
}) {
  const arrays = ["versions", "events", "evidence"];
  const values = Object.entries(data).filter(
    ([key, value]) =>
      ![...arrays, "files", "content", "id", "kind", "package_hash"].includes(
        key,
      ) &&
      value !== null &&
      value !== undefined &&
      typeof value !== "object",
  );
  return (
    <>
      <dl className="record-fields">
        {values.map(([key, value]) => (
          <div key={key}>
            <dt>{key.replaceAll("_", " ")}</dt>
            <dd>{readable(value)}</dd>
          </div>
        ))}
      </dl>
      {arrays.map((key) =>
        Array.isArray(data[key]) && (data[key] as unknown[]).length > 0 ? (
          <section className="history" key={key}>
            <h3>{key}</h3>
            {(data[key] as unknown[]).map((entry, index) => {
              const record =
                typeof entry === "object" && entry
                  ? (entry as Record<string, unknown>)
                  : { content: entry };
              return (
                <article key={index}>
                  <span className="badge">
                    {readable(
                      record.version
                        ? `v${record.version}`
                        : (record.kind ?? record.status ?? `#${index + 1}`),
                    )}
                  </span>
                  {Object.entries(record)
                    .filter(
                      ([name]) =>
                        ![
                          "id",
                          "workspace_id",
                          "source_id",
                          "lease_token",
                        ].includes(name),
                    )
                    .map(([name, value]) => (
                      <div className="history-field" key={name}>
                        <span>{name.replaceAll("_", " ")}</span>
                        <p>{readable(value)}</p>
                      </div>
                    ))}
                </article>
              );
            })}
          </section>
        ) : null,
      )}
      <details className="raw-details">
        <summary>Raw JSON</summary>
        <pre>
          {JSON.stringify(
            {
              ...data,
              files: Array.isArray(data.files)
                ? (data.files as Record<string, unknown>[]).map((file) => ({
                    path: file.path,
                    executable: file.executable,
                  }))
                : undefined,
            },
            null,
            2,
          )}
        </pre>
      </details>
    </>
  );
}
