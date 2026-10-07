"""Hybrid recall is authorized, current and bounded against a real database."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update

from memory_platform.api.knowledge_schemas import RecallRequest
from memory_platform.auth.principal import resolve_principal
from memory_platform.errors import AppError
from memory_platform.knowledge_tables import memory_embeddings
from memory_platform.providers import validate_vectors
from memory_platform.services.recall import recall
from memory_platform.services.sources import delete_source, upload_source
from memory_platform.storage import LocalStorage
from memory_platform.tables import memories
from memory_platform.worker import (
    claim_job,
    process_job,
    queue_missing_memory_embeddings,
    queue_missing_source_embeddings,
)
from tests.test_sources import encoded


class FakeProvider:
    model = "deterministic-multilingual"
    dimensions = 3

    def embed(self, workspace_id, texts):
        return [[1.0, 0.1 if "database" in text or "ဒေတာ" in text else 0.5, 0.1] for text in texts]

    def extract(self, workspace_id, text):
        return "Evidence summary"


def create_memory(client, workspace, content, **fields):
    result = client.post(
        "/v1/memories",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "type": "fact",
            "content": content,
            **fields,
        },
    )
    assert result.status_code == 201, result.text
    return UUID(result.json()["id"])


def test_keyword_http_recall_mixed_language(owner_client, workspace):
    mid = create_memory(owner_client, workspace, "Neon database supports မြန်မာဘာသာ memory")
    response = owner_client.post(
        "/v1/recall",
        json={
            "query": "မြန်မာဘာသာ",
            "semantic": False,
            "scope_ids": [str(workspace.personal_scope_id)],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["items"][0]["id"] == str(mid)
    assert "မြန်မာဘာသာ" in data["context"]
    assert data["items"][0]["citation"] == f"memory:{mid}@v1"
    assert not data["semantic_used"]
    assert data["estimated_tokens"] <= 3000
    assert (
        owner_client.post(
            "/v1/recall", json={"query": "nonexistent qqq", "semantic": False}
        ).json()["items"]
        == []
    )


def test_private_scope_and_effective_state_filters(
    owner_client, workspace, engine, make_agent_client, make_scope
):
    create_memory(owner_client, workspace, "private database")
    project = make_scope("project", "recall-project")
    client = make_agent_client([project], ["memory:read"])
    assert (
        client.post("/v1/recall", json={"query": "database", "semantic": False}).json()["items"]
        == []
    )
    assert (
        client.post(
            "/v1/recall",
            json={"query": "database", "scope_ids": [str(workspace.personal_scope_id)]},
        ).status_code
        == 404
    )
    expired = create_memory(owner_client, workspace, "expired uniquemarker")
    future = create_memory(owner_client, workspace, "future uniquemarker")
    with engine.begin() as conn:
        conn.execute(
            update(memories)
            .where(memories.c.id == expired)
            .values(valid_until=datetime.now(UTC) - timedelta(days=1))
        )
        conn.execute(
            update(memories)
            .where(memories.c.id == future)
            .values(valid_from=datetime.now(UTC) + timedelta(days=1))
        )
    assert (
        owner_client.post("/v1/recall", json={"query": "uniquemarker", "semantic": False}).json()[
            "items"
        ]
        == []
    )


def test_budget_counts_citations_and_preserves_unicode(owner_client, workspace):
    create_memory(owner_client, workspace, "မြန်မာစာ " * 700)
    data = owner_client.post(
        "/v1/recall", json={"query": "မြန်မာစာ", "semantic": False, "token_budget": 80}
    ).json()
    assert data["items"] and data["truncated"]
    assert data["items"][0]["truncated"]
    assert data["estimated_tokens"] <= 80
    assert "\ufffd" not in data["context"]


def test_vectors_exclude_updated_deleted_memory(owner_client, workspace, engine, tmp_path):
    mid = create_memory(owner_client, workspace, "database vector marker")
    provider = FakeProvider()
    storage = LocalStorage(tmp_path)
    queue_missing_memory_embeddings(engine, provider, limit=10000)
    job = claim_job(engine, workspace.id)
    assert job is not None
    process_job(engine, job, storage, provider)
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
        assert (
            conn.execute(
                select(memory_embeddings.c.id).where(memory_embeddings.c.memory_id == mid)
            ).first()
            is not None
        )
    request = RecallRequest(query="semanticdifferent", scope_ids=[workspace.personal_scope_id])
    assert recall(engine, principal, request, provider).items[0].id == mid
    changed = owner_client.patch(
        f"/v1/memories/{mid}", json={"expected_version": 1, "content": "changed unmatching"}
    )
    assert changed.status_code == 200
    assert recall(engine, principal, request, provider).items == []
    owner_client.delete(f"/v1/memories/{mid}")
    assert recall(engine, principal, request, provider).items == []


def test_current_source_version_only(engine, workspace, tmp_path):
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
    storage = LocalStorage(tmp_path)
    first = upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Evidence",
        filename="e.txt",
        encoded=encoded("obsoletekeyword database"),
    )
    process_job(engine, claim_job(engine, workspace.id), storage)
    assert recall(engine, principal, RecallRequest(query="obsoletekeyword", semantic=False)).items
    upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Evidence",
        filename="e.txt",
        encoded=encoded("currentkeyword"),
        source_id=first["id"],
        expected_version=1,
    )
    pending = recall(engine, principal, RecallRequest(query="obsoletekeyword", semantic=False))
    assert [item.version for item in pending.items] == [1]
    process_job(engine, claim_job(engine, workspace.id), storage)
    assert (
        recall(engine, principal, RecallRequest(query="obsoletekeyword", semantic=False)).items
        == []
    )
    result = recall(engine, principal, RecallRequest(query="currentkeyword", semantic=False))
    assert result.items[0].version == 2
    assert result.items[0].trust == "source_extracted"
    assert "@v2#chunk=0" in result.items[0].citation


@pytest.mark.parametrize(
    "vectors,count,dimensions",
    [
        ([[float("nan"), 1]], 1, 2),
        ([[0, 0]], 1, 2),
        ([[True, 1]], 1, 2),
        ([[1, 2]], 2, 2),
        ([[1, 2]], 1, 3),
    ],
)
def test_provider_shape_validation(vectors, count, dimensions):
    with pytest.raises(AppError):
        validate_vectors(vectors, count, dimensions)


class _ExtractingProvider(FakeProvider):
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


def test_recall_with_session_context(owner_client, workspace):
    """Session-aware recall prepends the authorized summary + selected events."""
    from uuid import uuid4

    create_memory(owner_client, workspace, "session database marker")
    created = owner_client.post(
        "/v1/sessions",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "client": "codex",
            "external_id": str(uuid4()),
        },
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    event = owner_client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "event_id": str(uuid4()),
            "sequence": 1,
            "kind": "message",
            "author": "user",
            "content": "Session selected requirement",
        },
    )
    assert event.status_code == 201, event.text
    summary = owner_client.patch(
        f"/v1/sessions/{sid}/state",
        json={"expected_version": 0, "summary": "Working on database migration"},
    )
    assert summary.status_code == 200, summary.text
    response = owner_client.post(
        "/v1/recall",
        json={
            "query": "database",
            "semantic": False,
            "session_id": sid,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["session"]["session_id"] == sid
    assert data["session"]["summary"] == "Working on database migration"
    assert len(data["session"]["recent_events"]) == 1
    assert "Working on database migration" in data["context"]
    assert "Session selected requirement" in data["context"]
    assert any(item["content"] == "session database marker" for item in data["items"])
    concealed = owner_client.post(
        "/v1/recall",
        json={"query": "database", "semantic": False, "session_id": str(uuid4())},
    )
    assert concealed.status_code == 404


def test_recall_hides_source_extracted_without_current_evidence(
    engine, workspace, owner_client, tmp_path, settings
):
    """Deleted or replaced sources must not leave unsupported memories retrievable."""
    import sqlalchemy as sa

    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
    storage = LocalStorage(tmp_path)
    marker = "recall-evidence-marker database"
    source = upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Evidence",
        filename="e.txt",
        encoded=encoded(marker),
    )
    process_job(engine, claim_job(engine, workspace.id), storage)
    queue_missing_source_embeddings(engine, _ExtractingProvider(), limit=10000)
    job = claim_job(engine, workspace.id)
    assert job is not None
    process_job(
        engine,
        job,
        storage,
        _ExtractingProvider(),
        hmac_key=settings.hmac_key_bytes,
        settings=settings,
    )
    with engine.begin() as conn:
        memory = (
            conn.execute(select(memories).where(memories.c.workspace_id == workspace.id))
            .mappings()
            .one()
        )
        assert memory["trust"] == "source_extracted"
        # Owner promotion via API would convert trust to owner_asserted; activate
        # directly to cover active source-extracted memories.
        conn.execute(
            sa.update(memories).where(memories.c.id == memory["id"]).values(state="active")
        )
    mid = memory["id"]
    found = owner_client.post(
        "/v1/recall", json={"query": "recall-evidence-marker", "semantic": False}
    )
    assert found.status_code == 200, found.text
    assert any(item["id"] == str(mid) for item in found.json()["items"])
    delete_source(engine, principal, storage, source["id"])
    gone = owner_client.post(
        "/v1/recall", json={"query": "recall-evidence-marker", "semantic": False}
    )
    assert gone.status_code == 200, gone.text
    assert all(item["id"] != str(mid) for item in gone.json()["items"])
    graph = owner_client.get("/v1/graph", params={"kind": "memory"})
    assert graph.status_code == 200, graph.text
    assert all(node["id"] != str(mid) for node in graph.json()["nodes"])
