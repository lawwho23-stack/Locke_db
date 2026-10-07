"""Limits serialize concurrent resource writes and retain historical byte accounting."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import insert, update

from memory_platform.errors import AppError, ErrorCode
from memory_platform.knowledge_tables import knowledge_jobs, source_versions, sources
from memory_platform.services.quotas import enforce_quota, resource_usage


def test_concurrent_quota_last_slot_serialized(engine, workspace, settings):
    limits = settings.model_copy(update={"quota_sources": 1})
    barrier = Barrier(2)

    def consume():
        barrier.wait(timeout=5)
        try:
            with engine.begin() as conn:
                enforce_quota(conn, workspace.id, limits, "sources")
                conn.execute(
                    insert(sources).values(
                        id=uuid4(),
                        workspace_id=workspace.id,
                        scope_id=workspace.personal_scope_id,
                        title="Quota",
                        created_by=workspace.owner_actor_id,
                    )
                )
            return "created"
        except AppError as exc:
            return exc.code.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sorted(results) == ["created", "quota_exceeded"]
    with engine.connect() as conn:
        assert resource_usage(conn, workspace.id, "sources") == 1


def test_zero_disables_and_exact_limit_allowed(engine, workspace, settings):
    with engine.begin() as conn:
        enforce_quota(
            conn,
            workspace.id,
            settings.model_copy(update={"quota_sources": 0}),
            "sources",
            1_000_000,
        )
        enforce_quota(
            conn, workspace.id, settings.model_copy(update={"quota_sources": 1}), "sources", 1
        )
        with pytest.raises(AppError) as error:
            enforce_quota(
                conn, workspace.id, settings.model_copy(update={"quota_sources": 1}), "sources", 2
            )
        assert error.value.code == ErrorCode.quota_exceeded


def test_original_bytes_released_only_after_cleanup(engine, workspace):
    source_id, version_id, job_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(sources).values(
                id=source_id,
                workspace_id=workspace.id,
                scope_id=workspace.personal_scope_id,
                title="Quota",
                created_by=workspace.owner_actor_id,
            )
        )
        conn.execute(
            insert(source_versions).values(
                id=version_id,
                source_id=source_id,
                workspace_id=workspace.id,
                version=1,
                filename="quota.txt",
                format="txt",
                byte_size=100,
                storage_key=str(version_id),
                content_hash="a" * 64,
            )
        )
        conn.execute(
            insert(knowledge_jobs).values(
                id=job_id,
                workspace_id=workspace.id,
                kind="source_cleanup",
                target_id=source_id,
                target_version=1,
                dedup_key="cleanup-quotas",
            )
        )
        assert resource_usage(conn, workspace.id, "source_bytes") == 100
        conn.execute(
            update(knowledge_jobs).where(knowledge_jobs.c.id == job_id).values(status="failed")
        )
        assert resource_usage(conn, workspace.id, "source_bytes") == 100
        conn.execute(
            update(knowledge_jobs).where(knowledge_jobs.c.id == job_id).values(status="completed")
        )
        assert resource_usage(conn, workspace.id, "source_bytes") == 0
