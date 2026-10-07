"""Permission-filtered typed graph projection and asserted manual relationships."""

import base64
import binascii
import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, and_, literal, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert

from memory_platform.auth.principal import Principal
from memory_platform.enums import Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.knowledge_tables import sources
from memory_platform.services.access import fetch_memory_checked
from memory_platform.services.dashboard import require_owner
from memory_platform.services.evidence import current_source_evidence
from memory_platform.services.idempotency import record_activity
from memory_platform.services.locking import lock_scope
from memory_platform.tables import memories, memory_relations, scopes
from memory_platform.task_tables import task_sessions, tasks


def _projection(principal: Principal) -> Any:
    statements = []
    for table, kind, label, cap, state in (
        (scopes, "scope", scopes.c.name, None, literal(None)),
        (memories, "memory", memories.c.content, Capability.memory_read, memories.c.state),
        (sources, "source", sources.c.title, Capability.memory_read, literal(None)),
        (tasks, "task", tasks.c.title, Capability.task_read, tasks.c.status),
        (task_sessions, "session", task_sessions.c.client, Capability.task_read, literal(None)),
    ):
        ids = (
            principal.scopes_with(cap)
            if cap
            else [sid for sid, caps in principal.grants.items() if caps]
        )
        stmt = select(
            table.c.id,
            literal(kind).label("kind"),
            label.label("label"),
            (table.c.id if kind == "scope" else table.c.scope_id).label("scope_id"),
            state.label("state"),
        ).where(table.c.workspace_id == principal.workspace_id)
        stmt = stmt.where((table.c.id if kind == "scope" else table.c.scope_id).in_(ids))
        if kind == "memory":
            stmt = stmt.where(table.c.state != "deleted").where(
                or_(
                    table.c.trust != "source_extracted",
                    current_source_evidence(),
                )
            )
        if kind in ("source", "task"):
            stmt = stmt.where(table.c.deleted_at.is_(None))
        statements.append(stmt)
    return union_all(*statements).subquery()


def graph(
    conn: Connection,
    principal: Principal,
    *,
    scope_id: UUID | None = None,
    query: str = "",
    kind: str | None = None,
    focus_id: UUID | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    projection = _projection(principal)
    stmt = select(projection)
    if scope_id:
        if not principal.grants.get(scope_id):
            raise AppError(ErrorCode.not_found, "Not found.")
        stmt = stmt.where(projection.c.scope_id == scope_id)
    if focus_id:
        focus = (
            conn.execute(select(projection).where(projection.c.id == focus_id)).mappings().first()
        )
        if not focus:
            raise AppError(ErrorCode.not_found, "Not found.")
        neighbors = {focus_id, focus["scope_id"]}
        if focus["kind"] == "memory":
            neighboring_relations = conn.execute(
                select(memory_relations.c.from_id, memory_relations.c.to_id).where(
                    memory_relations.c.workspace_id == principal.workspace_id,
                    memory_relations.c.status == "active",
                    or_(
                        memory_relations.c.from_id == focus_id, memory_relations.c.to_id == focus_id
                    ),
                )
            )
            for row in neighboring_relations:
                neighbors.update(row)
        if focus["kind"] == "task":
            neighbors.add(
                conn.scalar(select(tasks.c.owner_session_id).where(tasks.c.id == focus_id))
            )
        if focus["kind"] == "session":
            neighbors.update(
                conn.scalars(select(tasks.c.id).where(tasks.c.owner_session_id == focus_id))
            )
        stmt = stmt.where(
            projection.c.scope_id == focus_id
            if focus["kind"] == "scope"
            else projection.c.id.in_(neighbors)
        )
    if kind:
        stmt = stmt.where(projection.c.kind == kind)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(projection.c.label.ilike(f"%{escaped}%", escape="\\"))
    fingerprint = hashlib.sha256(
        json.dumps([str(scope_id), query, kind, str(focus_id)]).encode()
    ).hexdigest()[:16]
    if cursor:
        try:
            if len(cursor) > 512:
                raise ValueError
            value = json.loads(
                base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            )
            if value["f"] != fingerprint:
                raise ValueError
            after_kind, after_id = str(value["k"]), UUID(value["i"])
        except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
            raise AppError(ErrorCode.bad_request, "Invalid graph cursor.") from None
        stmt = stmt.where(
            or_(
                projection.c.kind > after_kind,
                and_(projection.c.kind == after_kind, projection.c.id > after_id),
            )
        )
    rows = (
        conn.execute(stmt.order_by(projection.c.kind, projection.c.id).limit(101)).mappings().all()
    )
    nodes = [{**dict(row), "label": (row["label"] or "")[:160]} for row in rows[:100]]
    ids = [node["id"] for node in nodes]
    node_map = {node["id"]: node for node in nodes}
    relations = (
        conn.execute(
            select(memory_relations)
            .where(
                memory_relations.c.workspace_id == principal.workspace_id,
                memory_relations.c.status == "active",
                memory_relations.c.from_id.in_(ids),
                memory_relations.c.to_id.in_(ids),
            )
            .order_by(memory_relations.c.created_at, memory_relations.c.id)
            .limit(201)
        )
        .mappings()
        .all()
    )
    edges = [dict(row) for row in relations]
    for node in nodes:
        if node["kind"] != "scope" and node["scope_id"] in node_map:
            edges.append(
                {
                    "id": f"scope:{node['id']}",
                    "from_id": node["id"],
                    "to_id": node["scope_id"],
                    "type": "belongs_to",
                    "origin": "projected",
                    "evidence": "Stored scope membership",
                }
            )
    for task in conn.execute(
        select(tasks.c.id, tasks.c.owner_session_id).where(tasks.c.id.in_(ids))
    ).mappings():
        if task["owner_session_id"] in node_map:
            edges.append(
                {
                    "id": f"owner:{task['id']}",
                    "from_id": task["id"],
                    "to_id": task["owner_session_id"],
                    "type": "owned_by",
                    "origin": "projected",
                    "evidence": "Current task ownership fencing",
                }
            )
    next_cursor = None
    if len(rows) > 100:
        last = rows[99]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps({"f": fingerprint, "k": last["kind"], "i": str(last["id"])}).encode()
            )
            .decode()
            .rstrip("=")
        )
    return {
        "nodes": nodes,
        "edges": edges[:200],
        "next_cursor": next_cursor,
        "truncated": len(rows) > 100 or len(edges) > 200,
        "limits": {"nodes": 100, "edges": 200},
    }


def _endpoints(conn: Connection, principal: Principal, from_id: UUID, to_id: UUID) -> list[UUID]:
    require_owner(principal)
    rows = [
        fetch_memory_checked(conn, principal, mid, Capability.memory_read)
        for mid in sorted((from_id, to_id), key=str)
    ]
    scope_ids = sorted({row["scope_id"] for row in rows}, key=str)
    for sid in scope_ids:
        principal.require(sid, Capability.memory_write)
        lock_scope(conn, sid)
    for mid in sorted((from_id, to_id), key=str):
        fetch_memory_checked(conn, principal, mid, Capability.memory_write, for_update=True)
    return scope_ids


def create_relation(
    conn: Connection, principal: Principal, from_id: UUID, to_id: UUID, *, request_id: str
) -> dict[str, Any]:
    scope_ids = _endpoints(conn, principal, from_id, to_id)
    stmt = (
        insert(memory_relations)
        .values(
            workspace_id=principal.workspace_id,
            from_id=from_id,
            to_id=to_id,
            type="related_to",
            origin="asserted",
            created_by=principal.actor_id,
        )
        .on_conflict_do_update(
            constraint="uq_memory_relations_tuple",
            set_={"status": "active", "origin": "asserted", "created_by": principal.actor_id},
        )
        .returning(memory_relations)
    )
    row = conn.execute(stmt).mappings().one()
    conn.execute(
        update(scopes).where(scopes.c.id.in_(scope_ids)).values(revision=scopes.c.revision + 1)
    )
    record_activity(
        conn,
        workspace_id=principal.workspace_id,
        actor_id=principal.actor_id,
        action="relation.create",
        target_ids=(row["id"],),
        scope_id=scope_ids[0],
        request_id=request_id,
        result="ok",
    )
    return dict(row)


def delete_relation(
    conn: Connection, principal: Principal, relation_id: UUID, *, request_id: str
) -> dict[str, Any]:
    row = (
        conn.execute(
            select(memory_relations).where(
                memory_relations.c.id == relation_id,
                memory_relations.c.workspace_id == principal.workspace_id,
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise AppError(ErrorCode.not_found, "Not found.")
    scope_ids = _endpoints(conn, principal, row["from_id"], row["to_id"])
    if row["type"] != "related_to":
        raise AppError(
            ErrorCode.bad_request, "Only manual related_to relationships can be removed."
        )
    conn.execute(
        update(memory_relations)
        .where(memory_relations.c.id == relation_id)
        .values(status="removed")
    )
    conn.execute(
        update(scopes).where(scopes.c.id.in_(scope_ids)).values(revision=scopes.c.revision + 1)
    )
    record_activity(
        conn,
        workspace_id=principal.workspace_id,
        actor_id=principal.actor_id,
        action="relation.delete",
        target_ids=(relation_id,),
        scope_id=scope_ids[0],
        request_id=request_id,
        result="ok",
    )
    return {"id": relation_id, "status": "removed"}
