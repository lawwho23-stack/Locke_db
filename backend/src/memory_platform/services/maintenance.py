"""Content-free maintenance reports; destructive cleanup requires explicit apply."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, delete, func, select, update

from memory_platform.tables import operations
from memory_platform.task_tables import task_events, task_sessions, tasks


def run_maintenance(
    conn: Connection,
    *,
    apply: bool = False,
    session_retention_days: int = 0,
    event_retention_days: int = 0,
) -> dict[str, Any]:
    """Erase old completed-event text, retaining deduplication and replay receipts.

    Sessions are removed only when neither tasks nor durable events reference them.
    Forgotten-content suppressions and approved packages are never retention targets.
    """
    if min(session_retention_days, event_retention_days) < 0:
        raise ValueError("Retention days must be nonnegative.")
    now = datetime.now(UTC)
    expired = operations.c.expires_at <= now
    count = int(
        conn.execute(select(func.count()).select_from(operations).where(expired)).scalar_one()
    )
    report: dict[str, Any] = {
        "apply": apply,
        "expired_operations": count,
        "event_summaries": 0,
        "sessions": 0,
    }
    if apply:
        conn.execute(delete(operations).where(expired))
    if event_retention_days:
        eligible_tasks = select(tasks.c.id).where(
            tasks.c.status.in_(["completed", "cancelled"]),
            tasks.c.updated_at < now - timedelta(days=event_retention_days),
        )
        old_events = (
            task_events.c.task_id.in_(eligible_tasks)
            & (task_events.c.created_at < now - timedelta(days=event_retention_days))
            & (task_events.c.checkpoint != {})
        )
        report["event_summaries"] = int(
            conn.execute(
                select(func.count()).select_from(task_events).where(old_events)
            ).scalar_one()
        )
        if apply:
            conn.execute(update(task_events).where(old_events).values(checkpoint={}))
    if session_retention_days:
        task_ref = (
            select(tasks.c.id)
            .where(
                (tasks.c.owner_session_id == task_sessions.c.id)
                | (tasks.c.handoff_session_id == task_sessions.c.id)
            )
            .exists()
        )
        event_ref = (
            select(task_events.c.id).where(task_events.c.session_id == task_sessions.c.id).exists()
        )
        old_sessions = (
            (task_sessions.c.last_seen_at < now - timedelta(days=session_retention_days))
            & ~task_ref
            & ~event_ref
        )
        report["sessions"] = int(
            conn.execute(
                select(func.count()).select_from(task_sessions).where(old_sessions)
            ).scalar_one()
        )
        if apply:
            conn.execute(delete(task_sessions).where(old_sessions))
    return report
