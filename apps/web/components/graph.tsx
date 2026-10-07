"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Background, Controls, MiniMap, ReactFlow, type Edge, type Node } from "@xyflow/react";
import { request, message } from "@/lib/client";
import "@xyflow/react/dist/style.css";
type RecordNode = { id: string; kind: string; label: string; scope_id?: string };
type Relation = { id: string; from_id: string; to_id: string; type: string; origin: string; created_by?: string; evidence?: string };
export default function Graph({ scope, inspect }: { scope: string; inspect: (record: RecordNode, kind: string) => void }) {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("");
  const [focus, setFocus] = useState("");
  const [selected, setSelected] = useState<RecordNode | null>(null);
  const [relation, setRelation] = useState<Relation | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const controller = useRef<AbortController | null>(null);
  const load = useCallback(async (next?: string | null) => {
    controller.current?.abort(); const current = new AbortController(); controller.current = current;
    setBusy(true); setError("");
    const params = new URLSearchParams({ scope_id: scope });
    if (query) params.set("q", query);
    if (kind) params.set("kind", kind);
    if (focus) params.set("focus_id", focus);
    if (next) params.set("cursor", next);
    try {
      const data = await request(`graph?${params}`, "GET", undefined, current.signal);
      if (current.signal.aborted) return;
      setNodes(data.nodes.map((record: RecordNode, index: number) => ({ id: record.id, data: { label: `${record.kind}: ${record.label}`, record }, position: { x: index % 4 * 270, y: Math.floor(index / 4) * 140 }, style: { width: 230, fontSize: 12, background: record.kind === "scope" ? "#faf1e4" : "#f3f8fb", borderColor: record.kind === "scope" ? "#9e7540" : "#4388a4" } })));
      setEdges(data.edges.map((edge: Relation) => ({ id: edge.id, source: edge.from_id, target: edge.to_id, label: `${edge.type} (${edge.origin})`, data: edge, style: { stroke: edge.origin === "suggested" ? "#ad8949" : "#4b788e", strokeDasharray: edge.origin === "suggested" ? "5 4" : undefined } })));
      setCursor(data.next_cursor); setTruncated(data.truncated);
    } catch (failure) { if (!current.signal.aborted) setError(message(failure)); }
    finally { if (!current.signal.aborted) setBusy(false); }
  }, [scope, query, kind, focus]);
  useEffect(() => { setSelected(null); setRelation(null); void load(); return () => controller.current?.abort(); }, [load]);
  async function mutate(path: string, method: string, body?: unknown) {
    setBusy(true); setError("");
    try { await request(path, method, body); setRelation(null); await load(); }
    catch (failure) { setError(message(failure)); } finally { setBusy(false); }
  }
  const memories = nodes.filter(node => (node.data.record as RecordNode).kind === "memory");
  return <>
    <div className="filters"><label>Graph search<input maxLength={200} value={query} onChange={e => setQuery(e.target.value)} /></label><label>Node type<select value={kind} onChange={e => setKind(e.target.value)}><option value="">All types</option>{["scope", "memory", "source", "task", "session"].map(value => <option key={value}>{value}</option>)}</select></label>{focus && <button className="secondary" onClick={() => setFocus("")}>Clear focus</button>}<button className="secondary" disabled={busy} onClick={() => void load()}>Refresh graph</button></div>
    {error && <p className="alert" role="alert">{error}</p>}
    <p className="subtle">Stored relationships display their origin. Scope membership and task ownership are projected from stored metadata. Suggestions use dashed lines.</p>
    <div className="graph"><ReactFlow nodes={nodes} edges={edges} fitView nodesDraggable={false} onNodeClick={(_, node) => { const record = node.data.record as RecordNode; setSelected(record); setRelation(null); inspect(record, record.kind); }} onEdgeClick={(_, edge) => { setRelation(edge.data as Relation); setSelected(null); }}><Background /><Controls /><MiniMap pannable zoomable /></ReactFlow></div>
    {selected && <div className="actions"><span className="subtle">{selected.kind}: {selected.label}</span><button className="secondary" onClick={() => { setQuery(""); setKind(""); setFocus(selected.id); }}>Expand neighbors</button></div>}
    {relation && <div className="connection"><strong>{relation.type} · {relation.origin}</strong><p className="subtle">{relation.evidence ?? `Created by ${relation.created_by ?? "stored relation"}`}</p>{relation.type === "related_to" && <button className="danger" disabled={busy} onClick={() => { if (confirm("Remove this manual relationship?")) void mutate(`relations/${relation.id}`, "DELETE"); }}>Remove relationship</button>}</div>}
    {truncated && <p className="subtle">Limited to 100 nodes and 200 edges per page. Search or expand a node to inspect its neighborhood.</p>}{cursor && <button className="secondary" disabled={busy} onClick={() => void load(cursor)}>Next graph page</button>}
    <form className="relation-form" onSubmit={e => { e.preventDefault(); void mutate("relations", "POST", { from_id: from, to_id: to }); }}><h3>Assert a related relationship</h3><div className="actions"><label>From memory<select required value={from} onChange={e => setFrom(e.target.value)}><option value="">Choose memory</option>{memories.map(node => <option key={node.id} value={node.id}>{String(node.data.label).slice(0, 70)}</option>)}</select></label><label>To memory<select required value={to} onChange={e => setTo(e.target.value)}><option value="">Choose memory</option>{memories.map(node => <option key={node.id} value={node.id}>{String(node.data.label).slice(0, 70)}</option>)}</select></label><button disabled={busy || !from || !to || from === to}>Create relationship</button></div></form>
  </>;
}
