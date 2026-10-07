"""Retention preserves replay fencing, while operational metadata obeys grants."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update

from memory_platform.knowledge_tables import knowledge_jobs, provider_usage, sources
from memory_platform.services.maintenance import run_maintenance
from memory_platform.tables import forget_suppressions, operations
from memory_platform.task_tables import task_events, task_sessions, tasks
from tests.test_tasks_api import _session, _task


def test_retention_dry_run_and_apply_preserve_dedup(owner_client, workspace, engine):
    session_id = _session(owner_client, workspace.personal_scope_id)
    task = _task(owner_client, workspace.personal_scope_id, session_id)
    request = {
        "session_id": session_id,
        "event_id": str(uuid4()),
        "sequence": 2,
        "expected_version": 1,
        "generation": 1,
        "status": "completed",
        "summary": "private checkpoint",
    }
    checkpoint = owner_client.patch(f"/v1/tasks/{task['task_id']}/checkpoint", json=request)
    assert checkpoint.status_code == 200, checkpoint.text
    unused = _session(owner_client, workspace.personal_scope_id, "unused")
    old = datetime.now(UTC) - timedelta(days=60)
    with engine.begin() as conn:
        conn.execute(
            update(tasks).where(tasks.c.id == UUID(task["task_id"])).values(updated_at=old)
        )
        conn.execute(
            update(task_events)
            .where(task_events.c.task_id == UUID(task["task_id"]))
            .values(created_at=old)
        )
        conn.execute(
            update(task_sessions)
            .where(task_sessions.c.id.in_([UUID(session_id), UUID(unused)]))
            .values(last_seen_at=old)
        )
        conn.execute(
            insert(operations).values(
                actor_id=workspace.owner_actor_id,
                workspace_id=workspace.id,
                operation="retention-test",
                idempotency_key=uuid4().hex,
                request_hash="hash",
                expires_at=old,
            )
        )
        conn.execute(
            insert(forget_suppressions).values(
                workspace_id=workspace.id,
                scope_id=workspace.personal_scope_id,
                content_hmac="retention-must-preserve",
                created_at=old,
            )
        )
        report = run_maintenance(conn, event_retention_days=30, session_retention_days=30)
        # Both the creation record (title/goal) and the checkpoint summary are old
        # completed-event text, so both are scrubbed while replay receipts survive.
        assert report["event_summaries"] == 2 and report["sessions"] == 1
        assert (
            conn.scalar(
                select(task_events.c.checkpoint).where(
                    task_events.c.id == UUID(request["event_id"])
                )
            )["summary"]
            == "private checkpoint"
        )
        applied = run_maintenance(
            conn, apply=True, event_retention_days=30, session_retention_days=30
        )
        assert applied == {**report, "apply": True}
        assert (
            conn.scalar(
                select(task_events.c.checkpoint).where(
                    task_events.c.id == UUID(request["event_id"])
                )
            )
            == {}
        )
        assert (
            conn.scalar(select(task_sessions.c.id).where(task_sessions.c.id == UUID(session_id)))
            is not None
        )
        assert (
            conn.scalar(select(task_sessions.c.id).where(task_sessions.c.id == UUID(unused)))
            is None
        )
        assert (
            conn.scalar(
                select(forget_suppressions.c.content_hmac).where(
                    forget_suppressions.c.workspace_id == workspace.id
                )
            )
            == "retention-must-preserve"
        )
    # Removing old checkpoint text does not remove its immutable replay receipt:
    # the same event replays the same version instead of conflicting.
    replay = owner_client.patch(f"/v1/tasks/{task['task_id']}/checkpoint", json=request)
    assert replay.status_code == 200
    assert replay.json()["version"] == 2
    assert replay.json()["task_id"] == task["task_id"]


def test_retention_zero_default_keeps_session_and_event_text(owner_client, workspace, engine):
    session_id = _session(owner_client, workspace.personal_scope_id)
    _task(owner_client, workspace.personal_scope_id, session_id)
    with engine.begin() as conn:
        report = run_maintenance(conn, apply=True)
    assert report["sessions"] == 0 and report["event_summaries"] == 0


def test_job_metadata_scope_and_usage_owner_boundaries(
    owner_client, workspace, engine, make_agent_client
):
    source_id, job_id = uuid4(), uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(sources).values(
                id=source_id,
                workspace_id=workspace.id,
                scope_id=workspace.personal_scope_id,
                title="private title",
                created_by=workspace.owner_actor_id,
            )
        )
        conn.execute(
            insert(knowledge_jobs).values(
                id=job_id,
                workspace_id=workspace.id,
                kind="source_cleanup",
                target_id=source_id,
                target_version=1,
                dedup_key="private-dedup-key",
                lease_token=uuid4(),
            )
        )
        conn.execute(
            insert(provider_usage).values(
                workspace_id=workspace.id,
                operation="embedding",
                model="test",
                day=datetime.now(UTC).date(),
                reserved_usd="0.10",
                charged_usd="0.01",
                tokens=10,
                completed_at=datetime.now(UTC),
            )
        )
    hidden = make_agent_client([], [])
    reader = make_agent_client([workspace.personal_scope_id], ["memory:read"])
    wrong_cap = make_agent_client([workspace.personal_scope_id], ["task:read"])
    assert hidden.get(f"/v1/jobs/{job_id}").status_code == 404
    assert wrong_cap.get(f"/v1/jobs/{job_id}").status_code == 403
    status = reader.get(f"/v1/jobs/{job_id}")
    assert status.status_code == 200
    assert "lease_token" not in status.json() and "dedup_key" not in status.json()
    assert "private title" not in status.text
    assert reader.get("/v1/usage").status_code == 403
    response = owner_client.get("/v1/usage")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["resources"]["sources"]["used"] == 1
    assert result["pending_cleanup"] == 1
    assert result["provider"]["measured_requests"] == 1
    assert result["provider"]["cost_basis"] == "configured_price_estimate"


def test_ready_reports_migration_and_queue_depth(owner_client, workspace):
    response = owner_client.get("/ready")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] in ("ok", "degraded")
    assert data["migration_current"] is True
    assert isinstance(data["pending_jobs"], int)
    assert isinstance(data["jobs"], dict)
    assert (
        owner_client.get("/ready", headers={"Authorization": "Bearer mem_invalid"}).status_code
        == 401
    )


def test_rate_limit_returns_retryable_429(app, engine, workspace):
    from fastapi.testclient import TestClient

    from memory_platform.api.app import create_app

    settings = app.state.settings.model_copy(update={"rate_limit_per_minute": 3})
    # Fresh app with a low limit on the same engine; other tests keep the default.
    throttled = TestClient(
        create_app(settings=settings, engine=engine),
        headers={"Authorization": f"Bearer {workspace.owner_token}"},
    )
    statuses = [throttled.get("/v1/scopes").status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429 and statuses[4] == 429
    rejected = throttled.get("/v1/scopes")
    assert rejected.status_code == 429
    assert rejected.headers.get("Retry-After", "").isdecimal()
    assert rejected.json()["error"]["code"] == "rate_limited"
