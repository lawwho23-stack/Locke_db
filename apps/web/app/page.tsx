"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Graph from "@/components/graph";
import Connections from "@/components/connections";
import Job from "@/components/job";
import Usage from "@/components/usage";
import Processing from "@/components/processing";
import { uploadDocument } from "@/lib/upload";
import SourceReplacement from "@/components/source-version";
import Icon, { type IconName } from "@/components/icon";
import { useConfirm } from "@/components/confirmation";
import RecordDetails from "@/components/record-details";
import "@xyflow/react/dist/style.css";

type Scope = { id: string; name: string; kind: string; capabilities: string[] };
type Summary = {
  scopes: Scope[];
  counts: Record<string, number>;
  workspace_id: string;
  actor_id: string;
};
type Item = {
  id: string;
  scope_id?: string;
  content?: string;
  title?: string;
  command?: string;
  type?: string;
  status?: string;
  state?: string;
  trust?: string;
  revision?: number;
  version?: number;
  active_version?: number;
  current_version?: number;
  summary?: string;
  next_step?: string;
  action?: string;
  result?: string;
  created_at?: string;
};
type Detail = Item & {
  kind?: string;
  label?: string;
  evidence?: unknown[];
  versions?: unknown[];
  events?: unknown[];
  files?: { path: string; content_base64: string; executable: boolean }[];
  package_hash?: string;
};
const tabs = [
  "Memories",
  "Sources",
  "Tasks",
  "Approved skills",
  "Activity",
  "Graph",
  "Connections",
  "Usage",
] as const;
type Tab = (typeof tabs)[number];
const sectionIcons: Record<Tab, IconName> = {
  Memories: "memory",
  Sources: "source",
  Tasks: "task",
  "Approved skills": "skill",
  Activity: "activity",
  Graph: "graph",
  Connections: "connection",
  Usage: "usage",
};
const descriptions: Record<Tab, string> = {
  Memories: "The knowledge your agents carry forward.",
  Sources: "Documents that ground your agents in evidence.",
  Tasks: "Shared checkpoints, ownership, and what comes next.",
  "Approved skills": "Versioned tools, approved by you.",
  Activity: "A trace of changes across your workspace.",
  Graph: "Explore how your knowledge connects.",
  Connections: "Give each agent the access it needs.",
  Usage: "Resources, processing, and provider spending.",
};

async function request(
  path: string,
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
) {
  const response = await fetch(`/api/proxy/${path}`, {
    method,
    signal,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message ?? "Request failed.");
  return data;
}
function date(value?: string) {
  return value ? new Date(value).toLocaleString() : "";
}

export default function Dashboard() {
  const confirm = useConfirm();
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [compactNavigation, setCompactNavigation] = useState(false);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 760px)");
    const update = () => setCompactNavigation(media.matches);
    update(); media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [token, setToken] = useState("");
  const [tab, setTab] = useState<Tab>("Memories");
  const [scope, setScope] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [editing, setEditing] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");
  const [label, setLabel] = useState("");
  const [memoryType, setMemoryType] = useState("");
  const [state, setState] = useState("active");
  const [cursor, setCursor] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [uploadProgress, setUploadProgress] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const loadController = useRef<AbortController | null>(null);
  const detailGeneration = useRef(0);
  const view = useRef({ tab, scope });
  view.current = { tab, scope };
  const [truncated, setTruncated] = useState(false);

  const authenticate = useCallback(async () => {
    const response = await fetch("/api/session", { cache: "no-store" });
    const data = await response.json();
    if (response.ok) {
      setSummary(data);
      setScope((current) => current || data.scopes[0]?.id || "");
    } else if (response.status !== 401)
      setError(data.error?.message ?? "Unable to connect.");
    setInitializing(false);
  }, []);
  useEffect(() => {
    void authenticate();
  }, [authenticate]);

  const load = useCallback(
    async (next?: string | null, nextOffset = 0, quiet = false) => {
      if (!summary) return;
      loadController.current?.abort();
      const controller = new AbortController();
      loadController.current = controller;
      const selectedTab = tab,
        selectedScope = scope;
      const current = () =>
        !controller.signal.aborted &&
        view.current.tab === selectedTab &&
        view.current.scope === selectedScope;
      if (!quiet) {
        setBusy(true);
        setError("");
        setDetail(null);
        detailGeneration.current++;
      }
      setTruncated(false);
      try {
        if (["Connections", "Graph", "Usage"].includes(tab)) {
          setItems([]);
          return;
        }
        if (tab === "Activity") {
          const data = await request(
            "dashboard/activity",
            "GET",
            undefined,
            controller.signal,
          );
          if (current()) {
            setItems(data.items);
            setTruncated(data.truncated);
          }
          return;
        }
        if (!scope) {
          setItems([]);
          return;
        }
        const routes: Record<string, string> = {
          Memories: "memories",
          Sources: "sources",
          Tasks: "tasks",
          "Approved skills": "skills",
        };
        const params = new URLSearchParams({ scope_id: scope });
        if (tab !== "Approved skills") params.set("limit", "30");
        if (tab === "Memories") {
          if (search) params.set("q", search);
          if (label) params.set("label", label);
          if (memoryType) params.set("type", memoryType);
          params.set("state", state);
          if (next) params.set("cursor", next);
        }
        if (tab === "Sources") params.set("offset", String(nextOffset));
        const data = await request(
          `${routes[tab]}?${params}`,
          "GET",
          undefined,
          controller.signal,
        );
        if (!current()) return;
        setItems(data.items);
        setCursor(data.next_cursor ?? null);
        setOffset(nextOffset);
        setTruncated(
          Boolean(data.next_cursor) ||
            (tab === "Sources" && data.items.length === 30),
        );
      } catch (failure) {
        if (current())
          setError(
            failure instanceof Error
              ? failure.message
              : "Unable to load records.",
          );
      } finally {
        if (current() && !quiet) setBusy(false);
      }
    },
    [summary, scope, tab, search, label, memoryType, state],
  );
  useEffect(() => {
    setJobId(null);
  }, [scope, tab]);
  useEffect(() => {
    setItems([]);
    setCursor(null);
    setOffset(0);
    void load();
    return () => loadController.current?.abort();
  }, [load]);
  useEffect(() => {
    if (tab !== "Sources" || !summary) return;
    const timer = setInterval(() => {
      void load(null, offset, true);
    }, 4000);
    return () => clearInterval(timer);
  }, [tab, summary, offset, load]);
  useEffect(() => {
    if (detail?.kind !== "source") return;
    const controller = new AbortController();
    const id = detail.id;
    const timer = setInterval(() => {
      void request(`sources/${id}`, "GET", undefined, controller.signal)
        .then((data) => {
          if (!controller.signal.aborted)
            setDetail((current) =>
              current?.id === id ? { ...data, kind: "source" } : current,
            );
        })
        .catch(() => {});
    }, 4000);
    return () => {
      clearInterval(timer);
      controller.abort();
    };
  }, [detail?.id, detail?.kind]);

  async function login(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message);
      setToken("");
      await authenticate();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  }
  async function logout() {
    await fetch("/api/session", { method: "DELETE" });
    loadController.current?.abort();
    detailGeneration.current++;
    setSummary(null);
    setItems([]);
    setDetail(null);
  }
  async function inspect(item: Item, kind?: string) {
    const generation = ++detailGeneration.current;
    const selectedView = view.current;
    const current = () =>
      generation === detailGeneration.current &&
      view.current.tab === selectedView.tab &&
      view.current.scope === selectedView.scope;
    setBusy(true);
    setError("");
    try {
      const selected =
        kind ??
        (tab === "Sources"
          ? "source"
          : tab === "Tasks"
            ? "task"
            : tab === "Approved skills"
              ? "skill"
              : "memory");
      let data: Detail;
      if (selected === "scope") data = item;
      else if (selected === "skill")
        data = await request(
          `skills/resolve?scope_id=${item.scope_id}&command=${encodeURIComponent(item.command ?? "")}`,
        );
      else
        data = await request(
          `${selected === "source" ? "sources" : selected === "task" ? "tasks" : selected === "session" ? "sessions" : "memories"}/${item.id}`,
        );
      if (selected === "task")
        data.events = (await request(`tasks/${item.id}/events`)).items;
      if (current()) {
        setDetail({ ...data, kind: selected });
        setEditing(data.content ?? "");
      }
    } catch (failure) {
      if (current())
        setError(
          failure instanceof Error ? failure.message : "Unable to load detail.",
        );
    } finally {
      if (current()) setBusy(false);
    }
  }
  async function action(path: string, method: string, body?: unknown) {
    setBusy(true);
    setError("");
    const selectedView = view.current;
    try {
      const data = await request(path, method, body);
      if (
        view.current.tab === selectedView.tab &&
        view.current.scope === selectedView.scope
      ) {
        if (data.job_id) setJobId(data.job_id);
        await load();
        await authenticate();
      }
    } catch (failure) {
      setError(
        failure instanceof Error ? failure.message : "Operation failed.",
      );
    } finally {
      setBusy(false);
    }
  }
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const element = event.currentTarget;
    const form = new FormData(element);
    const file = form.get("document") as File;
    const selectedView = view.current;
    setBusy(true);
    setError("");
    try {
      const data = await uploadDocument(
        file,
        { scope, title: String(form.get("title") || file.name) },
        setUploadProgress,
      );
      if (
        view.current.tab === selectedView.tab &&
        view.current.scope === selectedView.scope
      ) {
        await load();
        if (data.job_id) setJobId(data.job_id);
        await authenticate();
        element.reset();
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Upload failed.");
    } finally {
      setBusy(false);
      setUploadProgress("");
    }
  }
  function downloadPackage() {
    if (!detail?.files) return;
    const blob = new Blob([JSON.stringify(detail, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${detail.command?.slice(1) || "skill"}-v${detail.active_version}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }
  if (initializing)
    return (
      <main className="loading">
        <span className="loading-mark">
          <Icon name="memory" size={30} />
        </span>
        <p>Connecting to your workspace…</p>
      </main>
    );
  if (!summary)
    return (
      <main className="login-shell">
        <div className="login-art" aria-hidden="true">
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="orbit orbit-three" />
          <span className="art-core">
            <Icon name="memory" size={48} />
          </span>
          <span className="art-caption">CONTEXT / CONTINUITY / CONTROL</span>
        </div>
        <section className="login">
          <div className="wordmark">
            <span className="brand-mark">
              <Icon name="memory" size={24} />
            </span>
            locke<span className="badge">PRIVATE WORKSPACE</span>
          </div>
          <p className="eyebrow">YOUR AGENTS. ONE MEMORY.</p>
          <h1>
            Good work starts
            <br />
            with context.
          </h1>
          <p>
            Your decisions, documents, and progress.
            <br />
            Ready for the next session.
          </p>
          {error && (
            <p className="alert" role="alert">
              {error}
            </p>
          )}
          <form onSubmit={login}>
            <label>
              Owner credential
              <input
                type="password"
                autoComplete="off"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                required
                placeholder="Paste your owner credential"
              />
            </label>
            <button disabled={busy}>
              {busy ? "Checking…" : "Open workspace"}
              <Icon name="arrow" />
            </button>
          </form>
          <p className="subtle">
            <span className="status-dot" /> Private access · encrypted session ·
            no browser token storage
          </p>
        </section>
        <footer className="login-footer">
          locke / agent memory platform<span>Built for continuity.</span>
        </footer>
      </main>
    );
  const selectedScope = summary.scopes.find((item) => item.id === scope);
  return (
    <main className={`shell ${navigationOpen ? "navigation-open" : ""}`}>
      <a className="skip-link" href="#workspace-content">
        Skip to content
      </a>
      <aside className="sidebar" inert={compactNavigation && !navigationOpen}>
        <div className="wordmark">
          <span className="brand-mark">
            <Icon name="memory" size={22} />
          </span>
          locke
          <button
            className="icon-button secondary mobile-close"
            aria-label="Close navigation"
            onClick={() => setNavigationOpen(false)}
          >
            <Icon name="close" />
          </button>
        </div>
        <div className="workspace-label">
          <span className="workspace-avatar">L</span>
          <div>
            <strong>Personal workspace</strong>
            <span>Owner access</span>
          </div>
          <span className="status-dot" />
        </div>
        <p className="nav-label">WORKSPACE</p>
        <nav className="tabs" aria-label="Workspace sections">
          {tabs.map((name) => (
            <button
              key={name}
              aria-label={name}
              className={tab === name ? "active" : ""}
              onClick={() => {
                setTab(name);
                setNavigationOpen(false);
              }}
              aria-current={tab === name ? "page" : undefined}
            >
              <Icon name={sectionIcons[name]} />
              <span>{name}</span>
              {summary.counts[name.toLowerCase().replace("approved ", "")] !==
                undefined && (
                <small>
                  {summary.counts[name.toLowerCase().replace("approved ", "")]}
                </small>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="connection-note">
            <Icon name="terminal" />
            <strong>Context that carries forward.</strong>
            <p>
              One workspace for every agent.
              <br />
              You stay in control.
            </p>
          </div>
          <button aria-label="Sign out" className="signout secondary" onClick={logout}>
            <Icon name="logout" />
            Sign out<span className="mono">owner</span>
          </button>
        </div>
      </aside>
      {navigationOpen && (
        <button
          className="nav-overlay"
          aria-label="Close navigation"
          onClick={() => setNavigationOpen(false)}
        />
      )}
      <div className="main-area">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button secondary mobile-menu"
              aria-label="Open navigation"
              onClick={() => setNavigationOpen(true)}
            >
              <Icon name="menu" />
            </button>
            <span>Workspace</span>
            <span className="separator">/</span>
            <strong>{tab}</strong>
          </div>
          <div className="actions">
            <span className="private-label">
              <span className="status-dot" /> PRIVATE
            </span>
            {!["Activity", "Usage"].includes(tab) && (
              <select
                aria-label="Project scope"
                value={scope}
                onChange={(event) => setScope(event.target.value)}
              >
                {summary.scopes.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} ({item.kind})
                  </option>
                ))}
              </select>
            )}
            <button
              className="icon-button secondary"
              aria-label="Refresh section"
              onClick={() => void load()}
              disabled={busy}
            >
              <Icon name="refresh" />
            </button>
          </div>
        </header>
        <div className="content-area" id="workspace-content">
          <div className="page-heading">
            <div>
              <p className="eyebrow">
                AGENT MEMORY / {selectedScope?.name ?? "WORKSPACE"}
              </p>
              <h1>{tab}</h1>
              <p>{descriptions[tab]}</p>
            </div>
            <span className="section-glyph">
              <Icon name={sectionIcons[tab]} size={32} />
            </span>
          </div>
          <section className="counts" aria-label="Workspace totals">
            {Object.entries(summary.counts).map(([name, count]) => (
              <div key={name}>
                <span>{name === "skills" ? "approved skills" : name}</span>
                <strong>
                  {count}
                  <span className="count-dash">—</span>
                </strong>
              </div>
            ))}
          </section>
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          <div className={`workspace ${detail ? "has-detail" : ""}`}>
            <section className="panel records-panel">
              <div className="toolbar">
                <div className="actions">
                  <Icon name={sectionIcons[tab]} />
                  <h2>
                    {tab === "Graph"
                      ? "Knowledge graph"
                      : tab === "Connections"
                        ? "Agent access"
                        : tab === "Usage"
                          ? "Workspace usage"
                          : "All " + tab.toLowerCase()}
                  </h2>
                </div>
                <span className="subtle">
                  {busy
                    ? "Updating…"
                    : ["Graph", "Connections", "Usage"].includes(tab)
                      ? ""
                      : `${items.length} records loaded`}
                </span>
              </div>
              {tab === "Memories" && (
                <div className="filters">
                  <label>
                    Search
                    <input
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      maxLength={200}
                      placeholder="Find stored knowledge"
                    />
                  </label>
                  <label>
                    Label
                    <input
                      value={label}
                      onChange={(e) => setLabel(e.target.value)}
                      maxLength={64}
                      placeholder="Exact label"
                    />
                  </label>
                  <label>
                    Type
                    <select
                      value={memoryType}
                      onChange={(e) => setMemoryType(e.target.value)}
                    >
                      <option value="">All types</option>
                      {[
                        "fact",
                        "preference",
                        "decision",
                        "experience",
                        "procedure",
                      ].map((value) => (
                        <option key={value}>{value}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    State
                    <select
                      value={state}
                      onChange={(e) => setState(e.target.value)}
                    >
                      {["active", "draft", "superseded", "expired"].map(
                        (value) => (
                          <option key={value}>{value}</option>
                        ),
                      )}
                    </select>
                  </label>
                </div>
              )}
              {tab === "Sources" && (
                <Processing
                  reload={async () => {
                    await load();
                    await authenticate();
                  }}
                />
              )}
              {tab === "Sources" &&
                selectedScope?.capabilities.includes("source:ingest") && (
                  <form className="upload" onSubmit={upload}>
                    <label>
                      Document title
                      <input
                        name="title"
                        maxLength={300}
                        placeholder="Optional title"
                      />
                    </label>
                    <label>
                      PDF, Markdown, text, or DOCX
                      <input
                        name="document"
                        type="file"
                        accept=".pdf,.md,.txt,.docx"
                        required
                      />
                    </label>
                    <button disabled={busy}>
                      {uploadProgress || "Upload document"}
                    </button>
                    <span className="subtle">
                      Text documents only. Maximum 20 MiB and 300 pages.
                    </span>
                  </form>
                )}
              {tab === "Sources" && jobId && <Job key={jobId} id={jobId} />}
              {tab === "Graph" ? (
                <Graph
                  key={scope}
                  scope={scope}
                  inspect={(record, kind) => void inspect(record, kind)}
                />
              ) : tab === "Connections" ? (
                <Connections
                  key={scope}
                  scope={scope}
                  scopes={summary.scopes}
                />
              ) : tab === "Usage" ? (
                <Usage />
              ) : (
                <div className="rows">
                  {items.map((item) => (
                    <button
                      key={item.id}
                      className={`row ${detail?.id === item.id ? "selected" : ""}`}
                      onClick={() =>
                        tab === "Activity"
                          ? setDetail({ ...item, kind: "activity" })
                          : void inspect(item)
                      }
                    >
                      <div className="row-title">
                        <span className="record-icon">
                          <Icon name={sectionIcons[tab]} size={16} />
                        </span>
                        <strong>
                          {item.command ||
                            item.title ||
                            item.action ||
                            item.type ||
                            "Memory"}
                        </strong>
                        <Icon name="arrow" size={15} />
                      </div>
                      {item.content && <p>{item.content.slice(0, 260)}</p>}
                      {item.summary && <p>{item.summary.slice(0, 180)}</p>}
                      <div className="meta">
                        {[
                          item.status || item.state,
                          item.trust,
                          item.result,
                          item.version
                            ? `Version ${item.version}`
                            : item.active_version
                              ? `Approved version ${item.active_version}`
                              : item.current_version
                                ? `Version ${item.current_version}`
                                : null,
                        ]
                          .filter(Boolean)
                          .map((value, index) => (
                            <span key={index} className="badge">
                              {value}
                            </span>
                          ))}
                        <span>{date(item.created_at)}</span>
                      </div>
                    </button>
                  ))}
                  {!items.length && (
                    <div className="empty">
                      <Icon name={sectionIcons[tab]} size={32} />
                      <h3>
                        {busy ? "Loading your workspace" : "A clean slate"}
                      </h3>
                      <p>
                        {busy
                          ? "Loading…"
                          : tab === "Approved skills"
                            ? "No approved skills in this scope. Import an approved package using the owner CLI."
                            : `No ${tab.toLowerCase()} available in this scope.`}
                      </p>
                    </div>
                  )}
                </div>
              )}
              {truncated && (
                <div className="actions">
                  <p className="subtle">This view is bounded.</p>
                  {cursor && (
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() => void load(cursor)}
                    >
                      Next page
                    </button>
                  )}
                  {!cursor && tab === "Sources" && (
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() => void load(null, offset + 30)}
                    >
                      Next page
                    </button>
                  )}
                </div>
              )}
            </section>
            <aside
              className={`panel detail ${detail ? "detail-open" : ""}`}
              aria-label="Record inspector"
            >
              <div className="dialog-heading">
                <h2>
                  {detail
                    ? detail.command || detail.title || "Record inspector"
                    : "Record inspector"}
                </h2>
                {detail && (
                  <button
                    className="icon-button secondary"
                    aria-label="Close inspector"
                    onClick={() => setDetail(null)}
                  >
                    <Icon name="close" />
                  </button>
                )}
              </div>
              {!detail ? (
                <p className="subtle">
                  Select a record to inspect its content, versions, evidence,
                  and current progress.
                </p>
              ) : (
                <>
                  <dl>
                    <dt>ID</dt>
                    <dd>{detail.id}</dd>
                    {detail.version && (
                      <>
                        <dt>Version</dt>
                        <dd>{detail.version}</dd>
                      </>
                    )}
                    {detail.trust && (
                      <>
                        <dt>Trust</dt>
                        <dd>{detail.trust}</dd>
                      </>
                    )}
                  </dl>
                  {detail.kind === "memory" && detail.content !== undefined && (
                    <>
                      <label>
                        Memory content
                        <textarea
                          value={editing}
                          onChange={(event) => setEditing(event.target.value)}
                          maxLength={8000}
                        />
                      </label>
                      <div className="actions">
                        <button
                          disabled={
                            busy ||
                            !summary.scopes
                              .find((item) => item.id === detail.scope_id)
                              ?.capabilities.includes("memory:write")
                          }
                          onClick={() =>
                            void action(`memories/${detail.id}`, "PATCH", {
                              expected_version: detail.version,
                              content: editing,
                            })
                          }
                        >
                          Save edit
                        </button>
                        <button
                          className="danger"
                          disabled={
                            busy ||
                            !summary.scopes
                              .find((item) => item.id === detail.scope_id)
                              ?.capabilities.includes("memory:delete")
                          }
                          onClick={async () => {
                            if (
                              await confirm(
                                "Forget this memory and scrub its stored text history?",
                              )
                            )
                              void action(`memories/${detail.id}`, "DELETE");
                          }}
                        >
                          Forget
                        </button>
                      </div>
                    </>
                  )}
                  {detail.files && (
                    <>
                      <p className="subtle">
                        Approved package: {detail.package_hash}
                      </p>
                      <ul>
                        {detail.files.map((file) => (
                          <li key={file.path}>
                            {file.path}
                            {file.executable ? " (executable file)" : ""}
                          </li>
                        ))}
                      </ul>
                      <div className="actions">
                        <button onClick={downloadPackage}>
                          Download package
                        </button>
                        <button
                          className="danger"
                          disabled={
                            busy ||
                            !summary.scopes
                              .find((item) => item.id === detail.scope_id)
                              ?.capabilities.includes("skill:write")
                          }
                          onClick={async () => {
                            if (
                              await confirm(
                                "Revoke this skill from future resolution? Downloaded copies remain outside this service.",
                              )
                            )
                              void action(
                                `skills/${detail.id}/revoke`,
                                "POST",
                                { expected_revision: detail.revision },
                              );
                          }}
                        >
                          Revoke
                        </button>
                      </div>
                    </>
                  )}
                  {detail.kind === "source" && (
                    <>
                      {summary.scopes
                        .find((item) => item.id === detail.scope_id)
                        ?.capabilities.includes("source:ingest") && (
                        <SourceReplacement
                          key={detail.id}
                          id={detail.id}
                          version={detail.current_version ?? 1}
                          scope={detail.scope_id ?? scope}
                          reload={() => load()}
                        />
                      )}
                      <button
                        className="danger"
                        disabled={
                          busy ||
                          !summary.scopes
                            .find((item) => item.id === detail.scope_id)
                            ?.capabilities.includes("memory:delete")
                        }
                        onClick={async () => {
                          if (
                            await confirm(
                              "Delete this document and its indexed content?",
                            )
                          )
                            void action(`sources/${detail.id}`, "DELETE");
                        }}
                      >
                        Delete source
                      </button>
                    </>
                  )}
                  <RecordDetails data={detail} />
                </>
              )}
            </aside>
          </div>
          <footer className="workspace-footer">
            <span>
              <span className="status-dot" /> Owner session
            </span>
            <span className="mono">
              workspace / {summary.workspace_id.slice(0, 8)}
            </span>
          </footer>
        </div>
      </div>
    </main>
  );
}
