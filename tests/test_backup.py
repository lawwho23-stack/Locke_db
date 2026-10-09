"""Encrypted recovery on disposable PostgreSQL, including exact package and original bytes."""

import base64
import hashlib
import stat
from contextlib import contextmanager
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.pool import NullPool

from memory_platform.backup import export_backup, restore_backup
from memory_platform.knowledge_tables import source_versions, sources
from memory_platform.schema import metadata
from memory_platform.skill_package import package_hash
from memory_platform.skill_tables import skill_files, skill_versions, skills
from memory_platform.storage import LocalStorage
from memory_platform.tables import forget_suppressions
from tests.conftest import make_alembic_config, seed_workspace

KEY = "ab" * 32


@contextmanager
def disposable_database(test_db_url, *, migrate=True):
    existing = create_engine(test_db_url, poolclass=NullPool)
    name = f"memtest_backup_{uuid4().hex[:8]}"
    admin = create_engine(existing.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    target = create_engine(existing.url.set(database=name), poolclass=NullPool)
    try:
        if migrate:
            command.upgrade(
                make_alembic_config(target.url.render_as_string(hide_password=False)), "head"
            )
        yield target
    finally:
        target.dispose()
        existing.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def backup_fixture(test_db_url, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_BACKUP_PASSPHRASE", "test-passphrase-that-is-long")
    with disposable_database(test_db_url) as original:
        workspace = seed_workspace(original)
        directory = tmp_path / "originals"
        storage = LocalStorage(directory)
        content = b"original exact bytes \xe1\x80\x99\r\n"
        source_id, version_id, skill_id = uuid4(), uuid4(), uuid4()
        package = [
            {
                "path": "SKILL.md",
                "content_base64": base64.b64encode(content).decode(),
                "executable": False,
            }
        ]
        with original.begin() as conn:
            conn.execute(
                insert(sources).values(
                    id=source_id,
                    workspace_id=workspace.id,
                    scope_id=workspace.personal_scope_id,
                    title="Document",
                    created_by=workspace.owner_actor_id,
                )
            )
            conn.execute(
                insert(source_versions).values(
                    id=version_id,
                    source_id=source_id,
                    workspace_id=workspace.id,
                    version=1,
                    filename="document.txt",
                    format="txt",
                    content_hash=hashlib.sha256(content).hexdigest(),
                    storage_key=str(version_id),
                    byte_size=len(content),
                )
            )
            conn.execute(
                insert(skills).values(
                    id=skill_id,
                    workspace_id=workspace.id,
                    scope_id=workspace.personal_scope_id,
                    command="/github_review",
                )
            )
            conn.execute(
                insert(skill_versions).values(
                    skill_id=skill_id,
                    version=1,
                    workspace_id=workspace.id,
                    approved_by=workspace.owner_actor_id,
                    package_hash=package_hash(package),
                )
            )
            conn.execute(
                insert(skill_files).values(
                    skill_id=skill_id,
                    version=1,
                    workspace_id=workspace.id,
                    path="SKILL.md",
                    content=content,
                    executable=False,
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
            conn.execute(skills.update().where(skills.c.id == skill_id).values(active_version=1))
            conn.execute(
                insert(forget_suppressions).values(
                    workspace_id=workspace.id,
                    scope_id=workspace.personal_scope_id,
                    content_hmac="forgotten-content-keyed-hash",
                )
            )
        storage.put(str(version_id), content)
        artifact = tmp_path / "recovery.memorydb"
        counts = export_backup(original, directory, artifact, KEY)
        yield original, artifact, counts, version_id, content


def test_full_roundtrip_and_encrypted_key(backup_fixture, test_db_url, tmp_path):
    original, artifact, counts, version_id, content = backup_fixture
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert content not in artifact.read_bytes() and KEY.encode() not in artifact.read_bytes()
    with disposable_database(test_db_url, migrate=False) as target:
        restored = restore_backup(target, tmp_path / "restored", artifact)
        assert restored["hmac_key_hex"] == KEY
        assert restored["tables"] == counts["tables"]
        assert LocalStorage(tmp_path / "restored").get(str(version_id)) == content
        with original.connect() as source_conn, target.connect() as target_conn:
            for table in metadata.tables.values():
                source_rows = [dict(row) for row in source_conn.execute(select(table)).mappings()]
                target_rows = [dict(row) for row in target_conn.execute(select(table)).mappings()]
                assert target_rows == source_rows
        # Restored approved versions remain immutable; restoration is not a new import.
        with pytest.raises(Exception, match="immutable"), target.begin() as conn:
            conn.execute(skill_files.delete())


@pytest.mark.parametrize("damage", ["tamper", "wrong_password"])
def test_authentication_failure_has_no_writes(
    backup_fixture, test_db_url, tmp_path, monkeypatch, damage
):
    _, artifact, _, _, _ = backup_fixture
    if damage == "tamper":
        data = bytearray(artifact.read_bytes())
        data[-1] ^= 1
        artifact.write_bytes(data)
    else:
        monkeypatch.setenv("MEMORY_BACKUP_PASSPHRASE", "wrong-passphrase-that-is-long")
    with disposable_database(test_db_url) as target:
        with pytest.raises(ValueError, match="authentication"):
            restore_backup(target, tmp_path / "restored", artifact)
        assert not (tmp_path / "restored").exists()
        with target.connect() as conn:
            assert conn.scalar(select(func.count()).select_from(skills)) == 0


def test_nonempty_target_and_existing_artifact_refused(backup_fixture, test_db_url, tmp_path):
    original, artifact, _, _, _ = backup_fixture
    with pytest.raises(FileExistsError):
        export_backup(original, tmp_path / "originals", artifact, KEY)
    with disposable_database(test_db_url) as target:
        seed_workspace(target)
        with pytest.raises(ValueError, match="empty"):
            restore_backup(target, tmp_path / "restored", artifact)


def test_symlink_artifact_refused(backup_fixture, test_db_url, tmp_path):
    _, artifact, _, _, _ = backup_fixture
    link = tmp_path / "link"
    link.symlink_to(artifact)
    with disposable_database(test_db_url) as target, pytest.raises(OSError):
        restore_backup(target, tmp_path / "restored", link)


def test_export_reads_originals_through_storage_adapter(backup_fixture, tmp_path):
    original, artifact, counts, _version_id, _content = backup_fixture
    directory = artifact.parent / "originals"
    # Read from an explicit adapter even when the supplied directory is unrelated.
    adapter = LocalStorage(directory)
    output = tmp_path / "adapter-recovery.memorydb"
    result = export_backup(original, tmp_path / "unused", output, KEY, storage=adapter)
    assert result["originals"] == counts["originals"]
    assert output.exists()
