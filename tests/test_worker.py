"""Durable leases, bounded retries, publication fences and spending caps."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from memory_platform.api.knowledge_schemas import RecallRequest
from memory_platform.auth.principal import resolve_principal
from memory_platform.enums import Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.knowledge_tables import (
    knowledge_jobs,
    memory_embeddings,
    provider_usage,
    source_chunks,
    source_versions,
    sources,
)
from memory_platform.providers import APIProvider, configured_provider
from memory_platform.services.recall import recall
from memory_platform.services.sources import checked_source, delete_source, upload_source
from memory_platform.storage import LocalStorage
from memory_platform.worker import (
    claim_job,
    process_job,
    queue_missing_memory_embeddings,
    queue_missing_source_embeddings,
)
from tests.test_recall import FakeProvider, create_memory
from tests.test_sources import encoded


def create_source(engine, workspace, storage, text="document database evidence"):
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
    result = upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Evidence",
        filename="e.txt",
        encoded=encoded(text),
    )
    return principal, result


def test_crashed_worker_reclaimed_and_old_lease_fenced(engine, workspace, tmp_path):
    storage = LocalStorage(tmp_path)
    _, source = create_source(engine, workspace, storage)
    first = claim_job(engine, workspace.id)
    assert first is not None
    assert claim_job(engine, workspace.id) is None
    with engine.begin() as conn:
        conn.execute(
            update(knowledge_jobs)
            .where(knowledge_jobs.c.id == first["id"])
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    second = claim_job(engine, workspace.id)
    assert second is not None and second["lease_token"] != first["lease_token"]
    process_job(engine, first, storage, FakeProvider())
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(source_chunks.c.id).where(
                    source_chunks.c.source_version_id == source["version_id"]
                )
            ).first()
            is None
        )
    process_job(engine, second, storage, FakeProvider())
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(knowledge_jobs.c.status).where(knowledge_jobs.c.id == first["id"])
            ).scalar_one()
            == "completed"
        )
        assert (
            conn.execute(
                select(source_chunks.c.id).where(
                    source_chunks.c.source_version_id == source["version_id"]
                )
            ).first()
            is not None
        )


def test_delete_during_external_call_cannot_restore_source(engine, workspace, tmp_path):
    storage = LocalStorage(tmp_path)
    principal, source = create_source(engine, workspace, storage)

    class DeletingProvider(FakeProvider):
        def embed(self, workspace_id, texts):
            delete_source(engine, principal, storage, source["id"])
            return super().embed(workspace_id, texts)

    process_job(engine, claim_job(engine, workspace.id), storage)
    queue_missing_source_embeddings(engine, DeletingProvider())
    job = claim_job(engine, workspace.id)
    process_job(engine, job, storage, DeletingProvider())
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(source_chunks.c.id).where(source_chunks.c.workspace_id == workspace.id)
            ).first()
            is None
        )
        assert (
            conn.execute(
                select(knowledge_jobs.c.status).where(knowledge_jobs.c.id == job["id"])
            ).scalar_one()
            == "cancelled"
        )
        assert (
            conn.execute(
                select(source_versions.c.status).where(source_versions.c.id == source["version_id"])
            ).scalar_one()
            == "cancelled"
        )


def test_replace_during_external_call_cannot_publish_obsolete_chunks(engine, workspace, tmp_path):
    storage = LocalStorage(tmp_path)
    principal, source = create_source(engine, workspace, storage)

    class ReplacingProvider(FakeProvider):
        def embed(self, workspace_id, texts):
            upload_source(
                engine,
                principal,
                storage,
                scope_id=workspace.personal_scope_id,
                title="Evidence",
                filename="e.txt",
                encoded=encoded("replacement content"),
                source_id=source["id"],
                expected_version=1,
            )
            return super().embed(workspace_id, texts)

    process_job(engine, claim_job(engine, workspace.id), storage)
    queue_missing_source_embeddings(engine, ReplacingProvider())
    first = claim_job(engine, workspace.id)
    process_job(engine, first, storage, ReplacingProvider())
    second = claim_job(engine, workspace.id)
    process_job(engine, second, storage)
    with engine.begin() as conn:
        current_chunks = (
            conn.execute(
                select(source_chunks.c.content)
                .select_from(source_chunks.join(source_versions).join(sources))
                .where(
                    sources.c.id == source["id"],
                    source_versions.c.version == sources.c.current_version,
                )
            )
            .scalars()
            .all()
        )
        assert current_chunks == ["replacement content"]
        # Historical lexical chunks may remain, but the obsolete external call
        # must not attach embeddings or complete its cancelled job.
        assert (
            conn.execute(
                select(source_chunks.c.embedding).where(
                    source_chunks.c.source_version_id == source["version_id"]
                )
            ).scalar_one()
            is None
        )
        assert (
            conn.execute(
                select(knowledge_jobs.c.status).where(knowledge_jobs.c.id == first["id"])
            ).scalar_one()
            == "cancelled"
        )
    result = recall(engine, principal, RecallRequest(query="database", semantic=False))
    assert result.items == []


def test_five_enrichment_attempts_stop_but_lexical_chunks_remain_ready(engine, workspace, tmp_path):
    storage = LocalStorage(tmp_path)
    _, source = create_source(engine, workspace, storage)
    process_job(engine, claim_job(engine, workspace.id), storage)

    class FailingProvider(FakeProvider):
        def embed(self, workspace_id, texts):
            raise AppError(ErrorCode.dependency_unavailable, "Provider unavailable.")

    queue_missing_source_embeddings(engine, FailingProvider())
    for attempt in range(1, 6):
        job = claim_job(engine, workspace.id)
        assert job is not None and job["attempts"] == attempt
        process_job(engine, job, storage, FailingProvider())
        with engine.begin() as conn:
            conn.execute(
                update(knowledge_jobs)
                .where(knowledge_jobs.c.id == job["id"])
                .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    assert claim_job(engine, workspace.id) is None
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(source_versions.c.status).where(source_versions.c.id == source["version_id"])
            ).scalar_one()
            == "ready"
        )
        assert (
            conn.execute(
                select(source_chunks.c.content).where(
                    source_chunks.c.source_version_id == source["version_id"]
                )
            ).scalar_one()
            == "document database evidence"
        )
        assert (
            conn.execute(
                select(knowledge_jobs.c.status).where(knowledge_jobs.c.id == job["id"])
            ).scalar_one()
            == "failed"
        )


def test_crashed_final_attempt_marks_source_failed(engine, workspace, tmp_path):
    storage = LocalStorage(tmp_path)
    _, source = create_source(engine, workspace, storage)
    job = claim_job(engine, workspace.id)
    with engine.begin() as conn:
        conn.execute(
            update(knowledge_jobs)
            .where(knowledge_jobs.c.id == job["id"])
            .values(attempts=5, lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        conn.execute(
            update(source_versions)
            .where(source_versions.c.id == source["version_id"])
            .values(status="processing")
        )
    assert claim_job(engine, workspace.id) is None
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(source_versions.c.status).where(source_versions.c.id == source["version_id"])
            ).scalar_one()
            == "failed"
        )


def test_memory_deleted_during_embedding_is_not_published(
    owner_client, engine, workspace, tmp_path
):
    mid = create_memory(owner_client, workspace, "database delete fence")

    class DeletingProvider(FakeProvider):
        def embed(self, workspace_id, texts):
            result = owner_client.delete(f"/v1/memories/{mid}")
            assert result.status_code == 200
            return super().embed(workspace_id, texts)

    provider = DeletingProvider()
    queue_missing_memory_embeddings(engine, provider, limit=10000)
    job = claim_job(engine, workspace.id)
    process_job(engine, job, LocalStorage(tmp_path), provider)
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(memory_embeddings.c.id).where(memory_embeddings.c.memory_id == mid)
            ).first()
            is None
        )
        assert (
            conn.execute(
                select(knowledge_jobs.c.status).where(knowledge_jobs.c.id == job["id"])
            ).scalar_one()
            == "cancelled"
        )


def test_daily_reservations_cannot_overspend_concurrently(engine, workspace, settings):
    configured = settings.model_copy(
        update={
            "provider_base_url": "https://provider.invalid/v1",
            "provider_api_key": SecretStr("test-secret"),
            "embedding_model": "fake",
            "embedding_dimensions": 3,
            "provider_daily_budget_usd": 1.0,
            "embedding_usd_per_million_tokens": 1000000.0,
        }
    )
    provider = APIProvider(engine, configured)

    def reserve(_):
        try:
            return provider._reserve(workspace.id, "embedding", "fake", 1, 1000000.0)
        except AppError:
            return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(reserve, range(3)))
    assert sum(r is not None for r in results) == 1
    with engine.begin() as conn:
        rows = (
            conn.execute(
                select(provider_usage).where(provider_usage.c.workspace_id == workspace.id)
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1 and rows[0]["reserved_usd"] == 1
        assert rows[0]["metadata"] == {} and rows[0]["charged_usd"] is None
    provider._reconcile(next(r for r in results if r), 0, 1000000.0, 1)
    assert provider._reserve(workspace.id, "embedding", "fake", 1, 1000000.0)


def test_default_provider_disabled_and_unknown_usage_keeps_reservation(engine, settings):
    assert configured_provider(engine, settings) is None
    with pytest.raises(AppError):
        APIProvider(engine, settings).embed(UUID(int=1), ["must never leave computer"])


def test_stale_worker_cannot_hide_already_published_source(engine, workspace, tmp_path):
    storage = LocalStorage(tmp_path)
    principal, source = create_source(engine, workspace, storage)
    stale = claim_job(engine, workspace.id)
    with engine.begin() as conn:
        conn.execute(
            update(knowledge_jobs)
            .where(knowledge_jobs.c.id == stale["id"])
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    current = claim_job(engine, workspace.id)
    process_job(engine, current, storage)
    process_job(engine, stale, storage)
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(source_versions.c.status).where(source_versions.c.id == source["version_id"])
            ).scalar_one()
            == "ready"
        )
        assert (
            conn.execute(
                select(knowledge_jobs.c.status).where(knowledge_jobs.c.id == current["id"])
            ).scalar_one()
            == "completed"
        )
        assert conn.execute(
            select(source_chunks.c.content).where(
                source_chunks.c.source_version_id == source["version_id"]
            )
        ).scalars().all() == ["document database evidence"]
    assert (
        len(recall(engine, principal, RecallRequest(query="evidence", semantic=False)).items) == 1
    )


def test_changed_dimensions_for_same_model_publish_new_memory_vector(
    owner_client, engine, workspace, tmp_path
):
    mid = create_memory(owner_client, workspace, "dimension migration memory")
    storage = LocalStorage(tmp_path)
    first = FakeProvider()
    queue_missing_memory_embeddings(engine, first, limit=10000)
    process_job(engine, claim_job(engine, workspace.id), storage, first)

    class NewDimensions(FakeProvider):
        dimensions = 4

        def embed(self, workspace_id, texts):
            return [[1.0, 0.1, 0.1, 0.1] for _ in texts]

    second = NewDimensions()
    queue_missing_memory_embeddings(engine, second, limit=10000)
    process_job(engine, claim_job(engine, workspace.id), storage, second)
    with engine.begin() as conn:
        dimensions = (
            conn.execute(
                select(memory_embeddings.c.dimensions).where(memory_embeddings.c.memory_id == mid)
            )
            .scalars()
            .all()
        )
    assert 4 in dimensions
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
    result = recall(engine, principal, RecallRequest(query="unmatching semantic query"), second)
    assert [item.id for item in result.items] == [mid]


def test_source_cleanup_retries_after_unlink_failure(engine, workspace, tmp_path, monkeypatch):
    storage = LocalStorage(tmp_path)
    principal, source = create_source(engine, workspace, storage)
    original_delete = storage.delete

    def unavailable(key):
        raise PermissionError("Simulated temporary filesystem failure")

    monkeypatch.setattr(storage, "delete", unavailable)
    # Logical deletion must succeed and durably retain physical cleanup work.
    result = delete_source(engine, principal, storage, source["id"])
    assert result["deleted"]
    assert list(storage.directory.iterdir())
    with engine.begin() as conn:
        with pytest.raises(AppError) as hidden:
            checked_source(conn, principal, source["id"], Capability.memory_read)
        assert hidden.value.code == ErrorCode.not_found
        assert (
            conn.execute(
                select(source_chunks.c.id).where(source_chunks.c.workspace_id == workspace.id)
            ).first()
            is None
        )
    cleanup = claim_job(engine, workspace.id)
    assert cleanup is not None and cleanup["kind"] == "source_cleanup"
    process_job(engine, cleanup, storage)
    with engine.begin() as conn:
        conn.execute(
            update(knowledge_jobs)
            .where(knowledge_jobs.c.id == cleanup["id"])
            .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    monkeypatch.setattr(storage, "delete", original_delete)
    # A new worker/storage instance recovers the durable obligation.
    storage = LocalStorage(tmp_path)
    cleanup = claim_job(engine, workspace.id)
    assert cleanup is not None and cleanup["attempts"] == 2
    process_job(engine, cleanup, storage)
    assert not list(storage.directory.iterdir())


def test_repeated_delete_requeues_terminal_file_cleanup(engine, workspace, tmp_path, monkeypatch):
    storage = LocalStorage(tmp_path)
    principal, source = create_source(engine, workspace, storage)
    original_delete = storage.delete

    def unavailable(key):
        raise PermissionError("Simulated persistent filesystem failure")

    monkeypatch.setattr(storage, "delete", unavailable)
    delete_source(engine, principal, storage, source["id"])
    for attempt in range(1, 6):
        job = claim_job(engine, workspace.id)
        assert job is not None and job["attempts"] == attempt
        process_job(engine, job, storage)
        with engine.begin() as conn:
            conn.execute(
                update(knowledge_jobs)
                .where(knowledge_jobs.c.id == job["id"])
                .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    assert claim_job(engine, workspace.id) is None
    delete_source(engine, principal, storage, source["id"])
    monkeypatch.setattr(storage, "delete", original_delete)
    retried = claim_job(engine, workspace.id)
    assert retried is not None and retried["attempts"] == 1
    process_job(engine, retried, storage)
    assert not list(storage.directory.iterdir())
