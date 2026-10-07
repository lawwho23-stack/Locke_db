"""Real PostgreSQL source lifecycle and bounded document parsing."""

import base64
import io
from uuid import uuid4

import pytest
from docx import Document
from pypdf import PdfWriter
from sqlalchemy import select

from memory_platform.api.knowledge_schemas import RecallRequest
from memory_platform.auth.principal import resolve_principal
from memory_platform.errors import AppError, ErrorCode
from memory_platform.knowledge_tables import knowledge_jobs, source_chunks, source_versions, sources
from memory_platform.services.recall import recall
from memory_platform.services.sources import (
    decode_upload,
    delete_source,
    parse_document,
    upload_source,
)
from memory_platform.storage import LocalStorage
from memory_platform.worker import claim_job, process_job


def encoded(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_source_version_lifecycle(engine, workspace, tmp_path):
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
    storage = LocalStorage(tmp_path / "originals")
    first = upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Myanmar notes",
        filename="notes.md",
        encoded=encoded("Memory project supports မြန်မာဘာသာ retrieval."),
    )
    job = claim_job(engine, workspace.id)
    assert job is not None
    process_job(engine, job, storage)
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(source_versions.c.status).where(source_versions.c.id == first["version_id"])
            ).scalar_one()
            == "ready"
        )
        assert (
            "မြန်မာဘာသာ"
            in conn.execute(
                select(source_chunks.c.content).where(
                    source_chunks.c.source_version_id == first["version_id"]
                )
            ).scalar_one()
        )
    second = upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Myanmar notes",
        filename="notes.txt",
        encoded=encoded("Current source says durable queue"),
        source_id=first["id"],
        expected_version=1,
    )
    assert second["version"] == 2
    with pytest.raises(AppError) as error:
        upload_source(
            engine,
            principal,
            storage,
            scope_id=workspace.personal_scope_id,
            title="ignored",
            filename="notes.txt",
            encoded=encoded("stale revision"),
            source_id=first["id"],
            expected_version=1,
        )
    assert error.value.code == ErrorCode.version_conflict
    assert len(list(storage.directory.iterdir())) == 2
    job = claim_job(engine, workspace.id)
    assert job is not None
    process_job(engine, job, storage)
    delete_source(engine, principal, storage, first["id"])
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(sources.c.deleted_at).where(sources.c.id == first["id"])
            ).scalar_one()
            is not None
        )
        assert (
            conn.execute(
                select(source_chunks.c.id).where(source_chunks.c.workspace_id == workspace.id)
            ).first()
            is None
        )
        assert set(
            conn.execute(
                select(knowledge_jobs.c.status).where(
                    knowledge_jobs.c.workspace_id == workspace.id,
                    knowledge_jobs.c.kind != "source_cleanup",
                )
            ).scalars()
        ) == {"cancelled"}
    assert not list(storage.directory.iterdir())


def test_replacement_keeps_ready_evidence_until_success(engine, workspace, tmp_path):
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
    storage = LocalStorage(tmp_path)
    first = upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Document",
        filename="a.txt",
        encoded=encoded("stable evidence"),
    )
    process_job(engine, claim_job(engine, workspace.id), storage)
    second = upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Document",
        filename="a.txt",
        encoded=encoded("new evidence"),
        source_id=first["id"],
        expected_version=1,
    )
    request = RecallRequest(query="evidence", semantic=False)
    assert [item.version for item in recall(engine, principal, request).items] == [1]
    # A corrupt original fails publication without hiding the last ready version.
    (storage.directory / str(second["version_id"])).write_bytes(b"corrupt original")
    job = claim_job(engine, workspace.id)
    process_job(engine, job, storage)
    assert [item.version for item in recall(engine, principal, request).items] == [1]
    third = upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Document",
        filename="a.txt",
        encoded=encoded("new evidence"),
        source_id=first["id"],
        expected_version=2,
    )
    process_job(engine, claim_job(engine, workspace.id), storage)
    result = recall(engine, principal, request)
    assert [item.version for item in result.items] == [third["version"]]


def test_ungranted_upload_has_no_side_effects(engine, workspace, make_scope, tmp_path):
    from memory_platform.auth.principal import Principal

    principal = Principal(
        actor_id=workspace.owner_actor_id,
        actor_kind="owner",
        workspace_id=workspace.id,
        credential_id=workspace.owner_credential_id,
        is_admin=True,
        grants={},
    )
    storage = LocalStorage(tmp_path)
    with pytest.raises(AppError) as error:
        upload_source(
            engine,
            principal,
            storage,
            scope_id=workspace.personal_scope_id,
            title="Hidden",
            filename="a.txt",
            encoded=encoded("Private content"),
        )
    assert error.value.code == ErrorCode.not_found
    with engine.begin() as conn:
        assert (
            conn.execute(select(sources.c.id).where(sources.c.workspace_id == workspace.id)).first()
            is None
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "name,data,code",
    [
        ("../a.txt", "YQ==", ErrorCode.validation_error),
        ("a.exe", "YQ==", ErrorCode.validation_error),
        ("a.txt", "not base64", ErrorCode.validation_error),
        ("a.txt", "", ErrorCode.payload_too_large),
    ],
)
def test_invalid_upload(name, data, code):
    with pytest.raises(AppError) as error:
        decode_upload(name, data)
    assert error.value.code == code


def test_supported_parser_formats_and_page_limit():
    assert parse_document("txt", "မြန်မာစာ".encode())[0][1] == "မြန်မာစာ"
    document = Document()
    document.add_paragraph("Evidence survives DOCX parsing")
    output = io.BytesIO()
    document.save(output)
    assert "Evidence survives" in parse_document("docx", output.getvalue())[0][1]
    writer = PdfWriter()
    for _ in range(301):
        writer.add_blank_page(width=72, height=72)
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(AppError) as error:
        parse_document("pdf", output.getvalue())
    assert error.value.code == ErrorCode.payload_too_large
    with pytest.raises(AppError):
        parse_document("txt", b"\xff")


def test_private_storage_rejects_traversal_and_symlink(tmp_path):
    storage = LocalStorage(tmp_path / "private")
    key = str(uuid4())
    storage.put(key, b"original")
    assert storage.get(key) == b"original"
    assert (storage.directory / key).stat().st_mode & 0o777 == 0o600
    with pytest.raises(AppError):
        storage.get("../secrets")
    bad_key = str(uuid4())
    (storage.directory / bad_key).symlink_to(storage.directory / key)
    with pytest.raises(OSError):
        storage.get(bad_key)
    linked = tmp_path / "link"
    linked.symlink_to(storage.directory, target_is_directory=True)
    with pytest.raises(ValueError):
        LocalStorage(linked)
