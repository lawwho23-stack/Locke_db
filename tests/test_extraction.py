"""Source-grounded candidates never become owner assertions or approved skills."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from memory_platform.capture_tables import source_memory_evidence
from memory_platform.errors import AppError
from memory_platform.knowledge_tables import knowledge_jobs, source_chunks, source_versions
from memory_platform.services.evidence import validate_extraction
from memory_platform.services.sources import delete_source, upload_source
from memory_platform.storage import LocalStorage
from memory_platform.tables import memories
from memory_platform.worker import claim_job, process_job, queue_missing_source_embeddings
from tests.test_recall import FakeProvider
from tests.test_sources import encoded
from tests.test_worker import create_source


class ExtractingProvider(FakeProvider):
    extraction_enabled = True

    def extract(self, workspace_id, text):
        chunk = json.loads(text)["chunks"][0]
        return json.dumps(
            {
                "summary": "Grounded evidence",
                "candidates": [
                    {
                        "type": "fact",
                        "content": chunk["content"],
                        "quote": chunk["content"],
                        "chunk_id": chunk["chunk_id"],
                    }
                ],
            }
        )


def enrich(engine, workspace, storage, provider, settings):
    queue_missing_source_embeddings(engine, provider, limit=10000)
    job = claim_job(engine, workspace.id)
    assert job is not None
    process_job(engine, job, storage, provider, hmac_key=settings.hmac_key_bytes, settings=settings)
    return job


def test_candidates_draft_trust_and_current_evidence(
    engine, workspace, owner_client, tmp_path, settings
):
    storage = LocalStorage(tmp_path)
    principal, source = create_source(engine, workspace, storage)
    process_job(engine, claim_job(engine, workspace.id), storage)
    enrich(engine, workspace, storage, ExtractingProvider(), settings)
    with engine.begin() as conn:
        memory = (
            conn.execute(select(memories).where(memories.c.workspace_id == workspace.id))
            .mappings()
            .one()
        )
        assert memory["trust"] == "source_extracted" and memory["state"] == "draft"
        assert memory["created_by"] == workspace.owner_actor_id
    mid = memory["id"]
    detail = owner_client.get(f"/v1/memories/{mid}")
    assert detail.status_code == 200, detail.text
    evidence = detail.json()["evidence"]
    assert len(evidence) == 1 and evidence[0]["kind"] == "source"
    assert evidence[0]["source_id"] == str(source["id"])
    assert evidence[0]["chunk_id"] and evidence[0]["ordinal"] == 0
    delete_source(engine, principal, storage, source["id"])
    assert owner_client.get(f"/v1/memories/{mid}").status_code == 404
    listed = owner_client.get("/v1/memories", params={"state": "draft"}).json()
    assert all(row["id"] != str(mid) for row in listed["items"])
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(source_memory_evidence).where(source_memory_evidence.c.memory_id == mid)
            ).first()
            is None
        )


@pytest.mark.parametrize("change", ["locator", "quote", "procedure", "extra", "json"])
def test_invalid_extraction_never_writes_candidates(change):
    chunk_id = uuid4()
    chunks = [{"id": chunk_id, "ordinal": 0, "page": None, "content": "Original evidence"}]
    candidate = {
        "type": "fact",
        "content": "Original evidence",
        "quote": "Original evidence",
        "chunk_id": str(chunk_id),
    }
    if change == "locator":
        candidate["chunk_id"] = str(uuid4())
    elif change == "quote":
        candidate.update(content="Invented fact", quote="Invented fact")
    elif change == "procedure":
        candidate["type"] = "procedure"
    elif change == "extra":
        candidate["trust"] = "owner_asserted"
    output = (
        "not json"
        if change == "json"
        else json.dumps(
            {
                "summary": "Summary",
                "candidates": [candidate],
            }
        )
    )
    with pytest.raises(AppError):
        validate_extraction(output, chunks)


def test_invalid_provider_bounded_retries_no_rows(engine, workspace, settings, tmp_path):
    storage = LocalStorage(tmp_path)
    _, source = create_source(engine, workspace, storage)
    process_job(engine, claim_job(engine, workspace.id), storage)

    class InvalidProvider(ExtractingProvider):
        def extract(self, workspace_id, text):
            return "invalid json"

    provider = InvalidProvider()
    queue_missing_source_embeddings(engine, provider, limit=10000)
    for attempt in range(1, 6):
        job = claim_job(engine, workspace.id)
        assert job["attempts"] == attempt
        process_job(
            engine, job, storage, provider, hmac_key=settings.hmac_key_bytes, settings=settings
        )
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
                select(memories.c.id).where(memories.c.workspace_id == workspace.id)
            ).first()
            is None
        )
        assert (
            conn.execute(
                select(source_versions.c.status).where(source_versions.c.id == source["version_id"])
            ).scalar_one()
            == "ready"
        )


def test_forget_prevents_source_reextraction(engine, workspace, settings, owner_client, tmp_path):
    storage = LocalStorage(tmp_path)
    principal, first = create_source(engine, workspace, storage)
    process_job(engine, claim_job(engine, workspace.id), storage)
    enrich(engine, workspace, storage, ExtractingProvider(), settings)
    with engine.begin() as conn:
        mid = conn.execute(
            select(memories.c.id).where(memories.c.workspace_id == workspace.id)
        ).scalar_one()
    assert owner_client.delete(f"/v1/memories/{mid}").status_code == 200
    second = upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Again",
        filename="again.txt",
        encoded=encoded("document database evidence"),
    )
    process_job(engine, claim_job(engine, workspace.id), storage)
    enrich(engine, workspace, storage, ExtractingProvider(), settings)
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(memories.c.id).where(
                    memories.c.workspace_id == workspace.id, memories.c.state != "deleted"
                )
            ).first()
            is None
        )
    assert second["id"] != first["id"]


def test_delete_during_extraction_cannot_publish(engine, workspace, settings, tmp_path):
    storage = LocalStorage(tmp_path)
    principal, source = create_source(engine, workspace, storage)
    process_job(engine, claim_job(engine, workspace.id), storage)

    class DeletingProvider(ExtractingProvider):
        def extract(self, workspace_id, text):
            delete_source(engine, principal, storage, source["id"])
            return super().extract(workspace_id, text)

    job = enrich(engine, workspace, storage, DeletingProvider(), settings)
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(memories.c.id).where(memories.c.workspace_id == workspace.id)
            ).first()
            is None
        )
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


def test_replacement_publication_hides_old_source_memories(
    engine, workspace, owner_client, settings, tmp_path
):
    storage = LocalStorage(tmp_path)
    principal, source = create_source(engine, workspace, storage)
    process_job(engine, claim_job(engine, workspace.id), storage)
    enrich(engine, workspace, storage, ExtractingProvider(), settings)
    with engine.begin() as conn:
        mid = conn.execute(
            select(memories.c.id).where(memories.c.workspace_id == workspace.id)
        ).scalar_one()
    upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="New",
        filename="new.txt",
        encoded=encoded("Different current evidence"),
        source_id=source["id"],
        expected_version=1,
    )
    assert owner_client.get(f"/v1/memories/{mid}").status_code == 200
    process_job(engine, claim_job(engine, workspace.id), storage)
    assert owner_client.get(f"/v1/memories/{mid}").status_code == 404


def test_source_evidence_foreign_keys_reject_scope_or_locator_mismatch(
    engine, workspace, settings, tmp_path, make_scope
):
    from sqlalchemy.exc import IntegrityError

    storage = LocalStorage(tmp_path)
    _, source = create_source(engine, workspace, storage)
    process_job(engine, claim_job(engine, workspace.id), storage)
    enrich(engine, workspace, storage, ExtractingProvider(), settings)
    other_scope = make_scope("project", "elsewhere")
    with engine.begin() as conn:
        row = (
            conn.execute(
                select(source_memory_evidence).where(
                    source_memory_evidence.c.workspace_id == workspace.id
                )
            )
            .mappings()
            .one()
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            update(source_memory_evidence)
            .where(source_memory_evidence.c.id == row["id"])
            .values(scope_id=other_scope)
        )
    assert UUID(str(source["id"])) == row["source_id"]
