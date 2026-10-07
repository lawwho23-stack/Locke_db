"""Shared task ownership, replay and isolation acceptance checks."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, update

from memory_platform.api.task_schemas import (
    CheckpointRequest,
    EventRequest,
    HandoffRequest,
    SessionRequest,
    TaskCreateRequest,
)
from memory_platform.auth.principal import resolve_principal
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.tasks import (
    create_task,
    list_events,
    read_task,
    register_session,
    session_detail,
    write_event,
)
from memory_platform.task_tables import task_events, task_sessions
from tests.conftest import WorkspaceInfo


def test_task_handoff_fences_previous_session(engine: Engine, workspace: WorkspaceInfo) -> None:
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
        first = register_session(
            conn,
            principal,
            SessionRequest(scope_id=workspace.personal_scope_id, client="codex", external_id="one"),
        )
        second = register_session(
            conn,
            principal,
            SessionRequest(
                scope_id=workspace.personal_scope_id, client="claude", external_id="two"
            ),
        )
        created = create_task(
            conn,
            principal,
            TaskCreateRequest(
                scope_id=workspace.personal_scope_id,
                session_id=first["id"],
                event_id=uuid4(),
                sequence=1,
                title="Work",
                goal="Share checkpoints",
            ),
        )
        offered = write_event(
            conn,
            principal,
            created["task_id"],
            HandoffRequest(
                session_id=first["id"],
                event_id=uuid4(),
                sequence=2,
                expected_version=1,
                generation=1,
                target_session_id=second["id"],
            ),
            "handoff",
        )
        accepted = write_event(
            conn,
            principal,
            created["task_id"],
            EventRequest(
                session_id=second["id"],
                event_id=uuid4(),
                sequence=1,
                expected_version=offered["version"],
                generation=1,
            ),
            "accept_handoff",
        )
        assert accepted["generation"] == 2
        with pytest.raises(AppError) as exc:
            write_event(
                conn,
                principal,
                created["task_id"],
                CheckpointRequest(
                    session_id=first["id"],
                    event_id=uuid4(),
                    sequence=3,
                    expected_version=accepted["version"],
                    generation=1,
                    summary="Late",
                ),
                "checkpoint",
            )
        assert exc.value.code == ErrorCode.version_conflict
        assert read_task(conn, principal, created["task_id"])["owner_session_id"] == second["id"]


def test_persistent_replay_sequence_and_deleted_fence(
    engine: Engine, workspace: WorkspaceInfo
) -> None:
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
        session = register_session(
            conn,
            principal,
            SessionRequest(scope_id=workspace.personal_scope_id, client="codex", external_id="one"),
        )
        create = TaskCreateRequest(
            scope_id=workspace.personal_scope_id,
            session_id=session["id"],
            event_id=uuid4(),
            sequence=1,
            title="Work",
            goal="Goal",
        )
        created = create_task(conn, principal, create)
        assert create_task(conn, principal, create) == created
        req = CheckpointRequest(
            session_id=session["id"],
            event_id=uuid4(),
            sequence=2,
            expected_version=1,
            generation=1,
            summary="Checkpoint",
            results=["220 tests passed"],
        )
        updated = write_event(conn, principal, created["task_id"], req, "checkpoint")
        assert write_event(conn, principal, created["task_id"], req, "checkpoint") == updated
        assert len(list_events(conn, principal, created["task_id"])) == 2
        with pytest.raises(AppError) as exc:
            write_event(
                conn,
                principal,
                created["task_id"],
                CheckpointRequest(
                    session_id=session["id"],
                    event_id=uuid4(),
                    sequence=1,
                    expected_version=2,
                    generation=1,
                ),
                "checkpoint",
            )
        assert exc.value.code == ErrorCode.version_conflict
        delete_req = EventRequest(
            session_id=session["id"], event_id=uuid4(), sequence=3, expected_version=2, generation=1
        )
        deleted = write_event(conn, principal, created["task_id"], delete_req, "delete")
        assert write_event(conn, principal, created["task_id"], delete_req, "delete") == deleted
        with pytest.raises(AppError) as exc:
            write_event(
                conn,
                principal,
                created["task_id"],
                CheckpointRequest(
                    session_id=session["id"],
                    event_id=uuid4(),
                    sequence=4,
                    expected_version=3,
                    generation=1,
                    summary="Resurrection",
                ),
                "checkpoint",
            )
        assert exc.value.code == ErrorCode.not_found
        assert conn.execute(
            select(task_events.c.checkpoint).where(task_events.c.task_id == created["task_id"])
        ).scalars().all() == [{}, {}, {}]


def test_session_stale_and_heartbeat_not_task_revision(
    engine: Engine, workspace: WorkspaceInfo
) -> None:
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
        session = register_session(
            conn,
            principal,
            SessionRequest(scope_id=workspace.personal_scope_id, client="codex", external_id="one"),
        )
        same = register_session(
            conn,
            principal,
            SessionRequest(scope_id=workspace.personal_scope_id, client="codex", external_id="one"),
        )
        assert same["id"] == session["id"]
        conn.execute(
            update(task_sessions)
            .where(task_sessions.c.id == session["id"])
            .values(last_seen_at=datetime.now(UTC) - timedelta(seconds=181))
        )
        assert session_detail(conn, principal, session["id"])["observed_status"] == "stale"
        assert (
            session_detail(conn, principal, session["id"], heartbeat=True)["observed_status"]
            == "active"
        )
        with pytest.raises(AppError) as exc:
            register_session(
                conn,
                principal,
                SessionRequest(scope_id=uuid4(), client="codex", external_id="unknown"),
            )
        assert exc.value.code == ErrorCode.not_found
