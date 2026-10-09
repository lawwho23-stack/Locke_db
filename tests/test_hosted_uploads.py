"""Hosted uploads bind the original to the caller and survive retry safely."""

from uuid import uuid4

from sqlalchemy import select

from memory_platform.knowledge_tables import source_versions
from memory_platform.storage import LocalStorage


def authorize(owner_client, workspace, **extra):
    return owner_client.post(
        "/v1/uploads",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "title": "Hosted document",
            "filename": "notes.md",
            "byte_size": 18,
            **extra,
        },
    )


def test_upload_receipt_finalize_is_retryable(owner_client, workspace, settings):
    response = authorize(owner_client, workspace)
    assert response.status_code == 201
    ticket = response.json()
    storage = LocalStorage(settings.storage_dir)
    storage.put(ticket["id"], b"private text notes")
    finalized = owner_client.post(f"/v1/uploads/{ticket['id']}/finalize")
    assert finalized.status_code == 202
    retried = owner_client.post(f"/v1/uploads/{ticket['id']}/finalize")
    assert retried.status_code == 202
    assert retried.json() == finalized.json()
    assert owner_client.get(f"/v1/sources/{finalized.json()['id']}").status_code == 200


def test_receipt_rejects_wrong_caller(owner_client, workspace, make_agent_client):
    response = authorize(owner_client, workspace)
    assert response.status_code == 201
    agent = make_agent_client([workspace.personal_scope_id], ["source:ingest"])
    assert agent.get(f"/v1/uploads/{response.json()['id']}").status_code == 404
    assert agent.post(f"/v1/uploads/{response.json()['id']}/finalize").status_code == 404


def test_reject_oversized_or_hidden_scope(owner_client, workspace):
    assert authorize(owner_client, workspace, byte_size=20971521).status_code == 422
    assert authorize(owner_client, workspace, scope_id=str(uuid4())).status_code == 404


def test_finalization_rechecks_size_and_replacement_version(
    owner_client, workspace, settings, engine
):
    first = authorize(owner_client, workspace).json()
    storage = LocalStorage(settings.storage_dir)
    storage.put(first["id"], b"wrong size")
    assert owner_client.post(f"/v1/uploads/{first['id']}/finalize").status_code == 422
    storage.delete(first["id"])
    storage.put(first["id"], b"private text notes")
    source = owner_client.post(f"/v1/uploads/{first['id']}/finalize").json()
    replacement = authorize(
        owner_client, workspace, source_id=source["id"], expected_version=1
    ).json()
    storage.put(replacement["id"], b"private text notes")
    assert owner_client.post(f"/v1/uploads/{replacement['id']}/finalize").status_code == 202
    assert (
        authorize(owner_client, workspace, source_id=source["id"], expected_version=1).status_code
        == 409
    )
    with engine.begin() as conn:
        keys = (
            conn.execute(
                select(source_versions.c.storage_key).where(
                    source_versions.c.source_id == source["id"]
                )
            )
            .scalars()
            .all()
        )
    assert set(keys) == {first["id"], replacement["id"]}


def test_source_job_status_uses_version_target(owner_client, workspace, settings):
    ticket = authorize(owner_client, workspace).json()
    LocalStorage(settings.storage_dir).put(ticket["id"], b"private text notes")
    finalized = owner_client.post(f"/v1/uploads/{ticket['id']}/finalize").json()
    response = owner_client.get(f"/v1/jobs/{finalized['job_id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_expired_receipt_cannot_finalize(owner_client, workspace, engine):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from memory_platform.upload_tables import upload_receipts

    ticket = authorize(owner_client, workspace).json()
    with engine.begin() as conn:
        conn.execute(
            update(upload_receipts)
            .where(upload_receipts.c.id == ticket["id"])
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert owner_client.post(f"/v1/uploads/{ticket['id']}/finalize").status_code == 409


def test_finalize_does_not_accept_client_urls(owner_client, workspace):
    assert authorize(owner_client, workspace, url="https://evil.test/private").status_code == 422


def test_large_document_finalize(owner_client, workspace, settings):
    content = b"x" * (20 * 1024 * 1024 - 1)
    response = authorize(owner_client, workspace, byte_size=len(content))
    assert response.status_code == 201
    ticket = response.json()
    LocalStorage(settings.storage_dir).put(ticket["id"], content)
    result = owner_client.post(f"/v1/uploads/{ticket['id']}/finalize")
    assert result.status_code == 202
    assert owner_client.get(f"/v1/sources/{result.json()['id']}").json()["versions"][0][
        "byte_size"
    ] == len(content)


def test_database_rejects_replacement_without_expected_version(
    owner_client, workspace, settings, engine
):
    import pytest
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from memory_platform.upload_tables import upload_receipts

    ticket = authorize(owner_client, workspace).json()
    LocalStorage(settings.storage_dir).put(ticket["id"], b"private text notes")
    source = owner_client.post(f"/v1/uploads/{ticket['id']}/finalize").json()
    replacement = authorize(
        owner_client, workspace, source_id=source["id"], expected_version=1
    ).json()
    with pytest.raises(IntegrityError, match="upload_replacement"), engine.begin() as conn:
        conn.execute(
            update(upload_receipts)
            .where(upload_receipts.c.id == replacement["id"])
            .values(expected_version=None)
        )
