"""Request-driven processing is owner-only and confined to its workspace."""

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select, update

from memory_platform.knowledge_tables import knowledge_jobs
from memory_platform.storage import LocalStorage
from memory_platform.upload_tables import upload_receipts
from tests.conftest import seed_workspace


def queue_document(client, scope):
    response = client.post(
        "/v1/sources",
        json={
            "scope_id": str(scope),
            "title": "Queue",
            "filename": "queue.md",
            "content_base64": base64.b64encode(b"Queued private document").decode(),
        },
    )
    assert response.status_code == 202
    return response.json()


def test_processing_is_workspace_bounded(owner_client, workspace, engine, client_for_token):
    other = seed_workspace(engine)
    foreign = queue_document(client_for_token(other.owner_token), other.personal_scope_id)
    own = queue_document(owner_client, workspace.personal_scope_id)
    response = owner_client.post("/v1/jobs/process")
    assert response.status_code == 200
    assert response.json()["worked"] is True
    with engine.begin() as conn:
        statuses = dict(
            conn.execute(
                select(knowledge_jobs.c.target_id, knowledge_jobs.c.status).where(
                    knowledge_jobs.c.target_id.in_([foreign["version_id"], own["version_id"]])
                )
            ).all()
        )
    assert statuses[UUID(foreign["version_id"])] == "queued"
    assert statuses[UUID(own["version_id"])] == "completed"


def test_agent_cannot_process(owner_client, workspace, make_agent_client):
    agent = make_agent_client([workspace.personal_scope_id], ["source:ingest", "memory:read"])
    assert agent.post("/v1/jobs/process").status_code == 403


def test_daily_recovery_requires_secret(owner_client, app, settings, workspace):
    settings.cron_secret = SecretStr("test-cron-secret-" * 3)
    settings.cron_workspace_id = workspace.id
    assert owner_client.get("/internal/cron").status_code == 401
    response = owner_client.get(
        "/internal/cron",
        headers={"Authorization": "Bearer " + settings.cron_secret.get_secret_value()},
    )
    assert response.status_code == 200
    assert response.json()["worked"] is False


def test_expired_upload_original_is_cleaned(owner_client, workspace, engine, settings):
    response = owner_client.post(
        "/v1/uploads",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "title": "Expired",
            "filename": "x.txt",
            "byte_size": 1,
        },
    )
    ticket = response.json()["id"]
    storage = LocalStorage(settings.storage_dir)
    storage.put(ticket, b"x")
    with engine.begin() as conn:
        conn.execute(
            update(upload_receipts)
            .where(upload_receipts.c.id == ticket)
            .values(expires_at=datetime.now(UTC) - timedelta(hours=2))
        )
    assert owner_client.post("/v1/jobs/process").status_code == 200
    assert not (storage.directory / ticket).exists()


def test_enrichment_resumes_in_bounded_batches(engine, workspace, settings, tmp_path):
    from memory_platform.auth.principal import resolve_principal
    from memory_platform.knowledge_tables import source_chunks
    from memory_platform.services.sources import upload_source
    from memory_platform.worker import claim_job, process_job, queue_missing_source_embeddings
    from tests.test_recall import FakeProvider

    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
    storage = LocalStorage(tmp_path)
    source = upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Long notes",
        filename="long.md",
        encoded=base64.b64encode(("Many chunks of source text. " * 1800).encode()).decode(),
    )
    process_job(engine, claim_job(engine, workspace.id), storage)
    provider = FakeProvider()
    settings.hosted_enrichment_batch_size = 8
    queue_missing_source_embeddings(engine, provider)
    job = claim_job(engine, workspace.id)
    process_job(engine, job, storage, provider, settings=settings)
    with engine.begin() as conn:
        saved = (
            conn.execute(
                select(source_chunks.c.embedding).where(
                    source_chunks.c.source_version_id == source["version_id"]
                )
            )
            .scalars()
            .all()
        )
        row = (
            conn.execute(select(knowledge_jobs).where(knowledge_jobs.c.id == job["id"]))
            .mappings()
            .one()
        )
    assert sum(vector is not None for vector in saved) == 8
    assert row["status"] == "queued" and row["attempts"] == 0
    for _ in range(8):
        next_job = claim_job(engine, workspace.id)
        if next_job is None:
            break
        process_job(engine, next_job, storage, provider, settings=settings)
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(knowledge_jobs.c.status).where(knowledge_jobs.c.id == job["id"])
            ).scalar_one()
            == "completed"
        )


def test_restricted_owner_cannot_process_ungranted_scopes(
    owner_client, workspace, engine, make_scope
):
    from sqlalchemy import delete

    from memory_platform.tables import credential_grants

    hidden = make_scope("project", "restricted-processing")
    with engine.begin() as conn:
        conn.execute(
            delete(credential_grants).where(
                credential_grants.c.scope_id == hidden,
                credential_grants.c.credential_id == workspace.owner_credential_id,
            )
        )
    assert owner_client.post("/v1/jobs/process").status_code == 404
