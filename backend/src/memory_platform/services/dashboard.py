"""Bounded owner views with explicit current-grant filtering."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, and_, func, or_, select

from memory_platform.auth.principal import Principal
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.knowledge_tables import source_versions, sources
from memory_platform.services.evidence import current_source_evidence
from memory_platform.skill_tables import skills
from memory_platform.tables import (
    activity,
    actors,
    credential_grants,
    credentials,
    memories,
    memory_relations,
    scopes,
)
from memory_platform.task_tables import tasks


def require_owner(principal: Principal) -> None:
    if principal.actor_kind != ActorKind.owner or not principal.is_admin:
        raise AppError(ErrorCode.forbidden, "Owner admin required.")


def _active_memories(principal: Principal) -> Any:
    now = datetime.now(UTC)
    return and_(
        memories.c.workspace_id == principal.workspace_id,
        memories.c.scope_id.in_(principal.scopes_with(Capability.memory_read)),
        memories.c.state == "active",
        or_(memories.c.valid_from.is_(None), memories.c.valid_from <= now),
        or_(memories.c.valid_until.is_(None), memories.c.valid_until > now),
        or_(memories.c.trust != "source_extracted", current_source_evidence()),
    )


def summary(conn: Connection, principal: Principal) -> dict[str, Any]:
    require_owner(principal)
    visible = conn.execute(
        select(scopes.c.id, scopes.c.name, scopes.c.kind)
        .where(
            scopes.c.workspace_id == principal.workspace_id,
            scopes.c.id.in_([scope_id for scope_id, caps in principal.grants.items() if caps]),
        )
        .order_by(scopes.c.name)
    ).mappings()
    counts = {
        "memories": conn.scalar(
            select(func.count()).select_from(memories).where(_active_memories(principal))
        ),
        "sources": conn.scalar(
            select(func.count())
            .select_from(sources)
            .where(
                sources.c.workspace_id == principal.workspace_id,
                sources.c.deleted_at.is_(None),
                sources.c.scope_id.in_(principal.scopes_with(Capability.memory_read)),
            )
        ),
        "tasks": conn.scalar(
            select(func.count())
            .select_from(tasks)
            .where(
                tasks.c.workspace_id == principal.workspace_id,
                tasks.c.deleted_at.is_(None),
                tasks.c.scope_id.in_(principal.scopes_with(Capability.task_read)),
            )
        ),
        "skills": conn.scalar(
            select(func.count())
            .select_from(skills)
            .where(
                skills.c.workspace_id == principal.workspace_id,
                skills.c.revoked.is_(False),
                skills.c.scope_id.in_(principal.scopes_with(Capability.skill_read)),
            )
        ),
    }
    return {
        "counts": counts,
        "scopes": [
            {**dict(row), "capabilities": sorted(cap.value for cap in principal.grants[row["id"]])}
            for row in visible
        ],
        "actor_id": principal.actor_id,
        "workspace_id": principal.workspace_id,
        "actor_kind": principal.actor_kind.value,
        "is_admin": principal.is_admin,
    }


def activity_feed(conn: Connection, principal: Principal) -> dict[str, Any]:
    require_owner(principal)
    granted = [scope_id for scope_id, caps in principal.grants.items() if caps]
    rows = (
        conn.execute(
            select(activity)
            .where(
                activity.c.workspace_id == principal.workspace_id,
                or_(
                    activity.c.scope_id.in_(granted),
                    and_(activity.c.scope_id.is_(None), activity.c.actor_id == principal.actor_id),
                ),
            )
            .order_by(activity.c.created_at.desc(), activity.c.id)
            .limit(101)
        )
        .mappings()
        .all()
    )
    return {"items": [dict(row) for row in rows[:100]], "truncated": len(rows) > 100}


def graph(conn: Connection, principal: Principal) -> dict[str, Any]:
    require_owner(principal)
    memory_rows = (
        conn.execute(
            select(
                memories.c.id,
                memories.c.scope_id,
                memories.c.type,
                memories.c.content,
                memories.c.version,
                memories.c.trust,
            )
            .where(_active_memories(principal))
            .order_by(memories.c.created_at.desc(), memories.c.id)
            .limit(76)
        )
        .mappings()
        .all()
    )
    source_rows = (
        conn.execute(
            select(
                sources.c.id,
                sources.c.scope_id,
                sources.c.title,
                sources.c.current_version,
                source_versions.c.status,
            )
            .select_from(
                sources.join(
                    source_versions,
                    and_(
                        sources.c.id == source_versions.c.source_id,
                        sources.c.current_version == source_versions.c.version,
                    ),
                )
            )
            .where(
                sources.c.workspace_id == principal.workspace_id,
                sources.c.deleted_at.is_(None),
                sources.c.scope_id.in_(principal.scopes_with(Capability.memory_read)),
            )
            .order_by(sources.c.created_at.desc(), sources.c.id)
            .limit(26)
        )
        .mappings()
        .all()
    )
    nodes = [
        {
            "id": row["id"],
            "kind": "memory",
            "scope_id": row["scope_id"],
            "label": row["content"][:120],
            "version": row["version"],
            "trust": row["trust"],
        }
        for row in memory_rows[:75]
    ] + [
        {
            "id": row["id"],
            "kind": "source",
            "scope_id": row["scope_id"],
            "label": row["title"],
            "version": row["current_version"],
            "status": row["status"],
        }
        for row in source_rows[:25]
    ]
    ids = [row["id"] for row in memory_rows[:75]]
    edges = (
        conn.execute(
            select(memory_relations)
            .where(
                memory_relations.c.workspace_id == principal.workspace_id,
                memory_relations.c.from_id.in_(ids),
                memory_relations.c.to_id.in_(ids),
                memory_relations.c.status == "active",
            )
            .order_by(memory_relations.c.created_at.desc(), memory_relations.c.id)
            .limit(201)
        )
        .mappings()
        .all()
    )
    return {
        "nodes": nodes,
        "edges": [dict(row) for row in edges[:200]],
        "truncated": len(memory_rows) > 75 or len(source_rows) > 25 or len(edges) > 200,
        "limits": {"nodes": 100, "edges": 200},
    }


def connections(conn: Connection, principal: Principal) -> dict[str, Any]:
    """Owner-only credential inventory without token hashes or plaintext."""
    require_owner(principal)
    visible = set(principal.grants)
    rows = (
        conn.execute(
            select(
                credentials.c.id,
                credentials.c.actor_id,
                credentials.c.token_prefix,
                credentials.c.created_at,
                credentials.c.expires_at,
                credentials.c.revoked_at,
                actors.c.display_name,
                actors.c.kind,
            )
            .select_from(credentials.join(actors, credentials.c.actor_id == actors.c.id))
            .where(credentials.c.workspace_id == principal.workspace_id, actors.c.kind == "agent")
            .order_by(credentials.c.created_at.desc(), credentials.c.id)
            .limit(101)
        )
        .mappings()
        .all()
    )
    items = []
    for row in rows[:100]:
        grants = (
            conn.execute(
                select(credential_grants.c.scope_id, credential_grants.c.capabilities).where(
                    credential_grants.c.credential_id == row["id"]
                )
            )
            .mappings()
            .all()
        )
        if any(grant["scope_id"] not in visible for grant in grants):
            continue
        items.append({**dict(row), "grants": [dict(grant) for grant in grants]})
    return {"items": items, "truncated": len(rows) > 100}
