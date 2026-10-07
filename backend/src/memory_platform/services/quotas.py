"""Workspace resource limits, serialized with the write that consumes capacity."""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, func, select

from memory_platform.errors import AppError, ErrorCode
from memory_platform.knowledge_tables import source_versions, sources
from memory_platform.skill_tables import skill_files
from memory_platform.tables import memories, workspaces
from memory_platform.task_tables import tasks

DEFAULT_LIMITS = {
    "memories": 10_000,
    "tasks": 1_000,
    "sources": 1_000,
    "source_bytes": 100 * 1024 * 1024,
    "skill_bytes": 10 * 1024 * 1024,
}


def resource_usage(conn: Connection, workspace_id: UUID, resource: str) -> int:
    """Count retained storage, including history and soft-deleted source originals."""
    expression: Any
    condition: Any
    if resource == "memories":
        table, expression, condition = memories, func.count(), memories.c.state != "deleted"
    elif resource == "tasks":
        table, expression, condition = tasks, func.count(), tasks.c.deleted_at.is_(None)
    elif resource == "sources":
        table, expression, condition = sources, func.count(), sources.c.deleted_at.is_(None)
    elif resource == "source_bytes":
        # Deleted originals stop counting only after the durable cleanup job succeeds.
        from memory_platform.knowledge_tables import knowledge_jobs

        cleaned = (
            select(knowledge_jobs.c.id)
            .where(
                knowledge_jobs.c.workspace_id == workspace_id,
                knowledge_jobs.c.kind == "source_cleanup",
                knowledge_jobs.c.target_id == source_versions.c.source_id,
                knowledge_jobs.c.status == "completed",
            )
            .exists()
        )
        table, expression, condition = (
            source_versions,
            func.coalesce(func.sum(source_versions.c.byte_size), 0),
            ~cleaned,
        )
    elif resource == "skill_bytes":
        table, expression, condition = (
            skill_files,
            func.coalesce(func.sum(func.octet_length(skill_files.c.content)), 0),
            True,
        )
    else:
        raise ValueError("Unknown quota resource.")
    return int(
        conn.execute(
            select(expression)
            .select_from(table)
            .where(table.c.workspace_id == workspace_id, condition)
        ).scalar_one()
    )


def quota_limit(settings: Any, resource: str) -> int:
    limit = int(getattr(settings, f"quota_{resource}", DEFAULT_LIMITS[resource]))
    if limit < 0:
        raise ValueError("Quota limits must be nonnegative.")
    return limit


def lock_workspace(conn: Connection, workspace_id: UUID) -> None:
    """Acquire capacity lock before any scope/session/target locks to avoid deadlocks."""
    if (
        conn.execute(
            select(workspaces.c.id).where(workspaces.c.id == workspace_id).with_for_update()
        ).first()
        is None
    ):
        raise AppError(ErrorCode.not_found, "Not found.")


def enforce_quota(
    conn: Connection, workspace_id: UUID, settings: Any, resource: str, additional: int = 1
) -> None:
    """Hold the workspace row lock until the caller commits its checked write.

    Every capacity-consuming write must use this helper in the SAME transaction.
    The row lock prevents two writers both observing the last available slot.
    A zero limit disables that resource's quota.
    """
    if resource not in DEFAULT_LIMITS or additional < 0:
        raise ValueError("Invalid quota request.")
    limit = quota_limit(settings, resource)
    lock_workspace(conn, workspace_id)
    if limit and resource_usage(conn, workspace_id, resource) + additional > limit:
        raise AppError(
            ErrorCode.quota_exceeded,
            "Workspace resource limit exceeded.",
            details={"resource": resource, "limit": limit},
        )


def usage_snapshot(conn: Connection, workspace_id: UUID, settings: Any) -> dict[str, Any]:
    return {
        resource: {
            "used": resource_usage(conn, workspace_id, resource),
            "limit": quota_limit(settings, resource),
        }
        for resource in DEFAULT_LIMITS
    }
