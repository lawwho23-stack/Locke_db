"""Portable authenticated whole-database backups, restricted to empty local restores.

The encryption envelope has fixed KDF parameters; untrusted input cannot request
unbounded key derivation. Passphrases are read only from the environment, never CLI
arguments. The HMAC key belongs inside the encryption because suppression matching
must survive restoration. Callers must keep the returned key out of logs.
"""

import base64
import hashlib
import json
import math
import os
import stat
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from pgvector.sqlalchemy import Vector
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.postgresql import ARRAY

from memory_platform.config import BACKEND_ROOT
from memory_platform.schema import metadata
from memory_platform.skill_package import package_hash
from memory_platform.storage import LocalStorage, Storage

MAGIC = b"MEMORYDB-BACKUP-1\x00"
MAX_BACKUP_BYTES = 256 * 1024 * 1024
MAX_ROWS = 1_000_000
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _config(engine: Engine) -> Config:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.attributes["url"] = engine.url.render_as_string(hide_password=False)
    return cfg


def _head(engine: Engine) -> str:
    head = ScriptDirectory.from_config(_config(engine)).get_current_head()
    if head is None:
        raise ValueError("Migration head unavailable.")
    return head


def _passphrase() -> bytes:
    value = os.environ.get("MEMORY_BACKUP_PASSPHRASE", "").encode()
    if not 16 <= len(value) <= 1024:
        raise ValueError("Set MEMORY_BACKUP_PASSPHRASE to 16 to 1024 UTF-8 bytes.")
    return value


def _key(salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(_passphrase())


def _json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode()


def _encode(value: Any) -> Any:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    if isinstance(value, UUID | Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_encode(item) for item in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def _decode(value: Any, kind: Any) -> Any:
    if value is None:
        return None
    if isinstance(kind, sa.Uuid):
        return UUID(value)
    if isinstance(kind, sa.LargeBinary):
        return base64.b64decode(value, validate=True)
    if isinstance(kind, sa.DateTime):
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("Backup dates require a timezone.")
        return result
    if isinstance(kind, sa.Date):
        return date.fromisoformat(value)
    if isinstance(kind, sa.Numeric):
        result_decimal = Decimal(value)
        if not result_decimal.is_finite():
            raise ValueError("Invalid numeric value.")
        return result_decimal
    if isinstance(kind, ARRAY):
        if not isinstance(value, list):
            raise ValueError("Invalid array value.")
        return [_decode(item, kind.item_type) for item in value]
    if isinstance(kind, Vector):
        if not isinstance(value, list) or not 1 <= len(value) <= 4096:
            raise ValueError("Invalid embedding shape.")
        if any(not isinstance(item, int | float) or not math.isfinite(item) for item in value):
            raise ValueError("Invalid embedding value.")
    elif isinstance(kind, sa.Boolean):
        if not isinstance(value, bool):
            raise ValueError("Invalid boolean value.")
    elif isinstance(kind, sa.Integer):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("Invalid integer value.")
    elif isinstance(kind, sa.Float):
        if not isinstance(value, int | float) or not math.isfinite(value):
            raise ValueError("Invalid float value.")
    elif isinstance(kind, sa.Text) and not isinstance(value, str):
        raise ValueError("Invalid text value.")
    return value


def _safe_directory(path: Path, *, create: bool = False) -> Path:
    path = path.absolute()
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise ValueError("Symlink directories are not permitted.")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError("Select an existing real directory.")
    return path


def _read_file(path: Path, maximum: int) -> bytes:
    _safe_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("Backup contains an oversized or special file.")
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Backup size limit exceeded.")
        return data


def _write_artifact(output: Path, content: bytes) -> None:
    parent = _safe_directory(output.parent)
    # Link publishes the complete private temporary file atomically without clobbering.
    fd, name = tempfile.mkstemp(prefix=".memory-backup-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, output, follow_symlinks=False)
    finally:
        Path(name).unlink(missing_ok=True)


def export_backup(
    engine: Engine,
    storage_directory: Path,
    output: Path,
    hmac_key_hex: str,
    *,
    storage: Storage | None = None,
) -> dict[str, Any]:
    """Encrypt a repeatable-read snapshot of every application table and originals."""
    if len(hmac_key_hex) != 64 or len(bytes.fromhex(hmac_key_hex)) != 32:
        raise ValueError("Invalid HMAC key.")
    directory = _safe_directory(storage_directory) if storage is None else None
    tables: dict[str, Any] = {}
    with (
        engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
        conn.begin(),
    ):
        conn.execute(sa.text("SET TRANSACTION READ ONLY"))
        revision = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
        if revision != _head(engine):
            raise ValueError("Backup requires the current migration head.")
        for name, table in metadata.tables.items():
            rows = [_encode(dict(row)) for row in conn.execute(sa.select(table)).mappings()]
            tables[name] = {"rows": rows, "sha256": hashlib.sha256(_json(rows)).hexdigest()}
        if sum(len(item["rows"]) for item in tables.values()) > MAX_ROWS:
            raise ValueError("Backup row limit exceeded.")
        # Only referenced originals are needed. Deleted originals may already be cleaned.
        deleted = {row["id"] for row in tables["sources"]["rows"] if row["deleted_at"]}
        files: dict[str, Any] = {}
        for row in tables["source_versions"]["rows"]:
            key = row["storage_key"]
            if str(UUID(key)) != key:
                raise ValueError("Unsafe storage key.")
            try:
                content = (
                    storage.get(key)
                    if storage is not None
                    else _read_file(directory / key, 20 * 1024 * 1024)
                )
            except FileNotFoundError:
                if row["source_id"] not in deleted:
                    raise ValueError("A live source original is missing.") from None
                continue
            digest = hashlib.sha256(content).hexdigest()
            if digest != row["content_hash"] or len(content) != row["byte_size"]:
                raise ValueError("Source original does not match its database manifest.")
            files[key] = {
                "sha256": digest,
                "size": len(content),
                "content": base64.b64encode(content).decode(),
            }
    payload = _json(
        {
            "format": 1,
            "revision": revision,
            "tables": tables,
            "files": files,
            "hmac_key_hex": hmac_key_hex,
        }
    )
    if len(payload) > MAX_BACKUP_BYTES:
        raise ValueError("Backup size limit exceeded.")
    salt, nonce = os.urandom(16), os.urandom(12)
    ciphertext = AESGCM(_key(salt)).encrypt(nonce, payload, MAGIC)
    _write_artifact(output, MAGIC + salt + nonce + ciphertext)
    return {
        "tables": {name: len(item["rows"]) for name, item in tables.items()},
        "originals": len(files),
        "encrypted_bytes": len(ciphertext),
    }


def _validate(payload: Any, revision: str) -> tuple[dict[str, Any], dict[str, bytes], str]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"format", "revision", "tables", "files", "hmac_key_hex"}
        or payload["format"] != 1
        or payload["revision"] != revision
    ):
        raise ValueError("Unsupported backup format or migration revision.")
    key = payload["hmac_key_hex"]
    if not isinstance(key, str) or len(key) != 64 or len(bytes.fromhex(key)) != 32:
        raise ValueError("Invalid suppression key.")
    snapshots = payload["tables"]
    if not isinstance(snapshots, dict) or set(snapshots) != set(metadata.tables):
        raise ValueError("Backup table manifest differs from this application.")
    rows_by_table: dict[str, Any] = {}
    for name, table in metadata.tables.items():
        snapshot = snapshots[name]
        if not isinstance(snapshot, dict) or set(snapshot) != {"rows", "sha256"}:
            raise ValueError("Invalid table manifest.")
        if not isinstance(snapshot["rows"], list) or (
            hashlib.sha256(_json(snapshot["rows"])).hexdigest() != snapshot["sha256"]
        ):
            raise ValueError("Table manifest hash mismatch.")
        decoded = []
        for row in snapshot["rows"]:
            if not isinstance(row, dict) or set(row) != set(table.c.keys()):
                raise ValueError("Backup row columns differ from the schema.")
            item = {column.name: _decode(row[column.name], column.type) for column in table.c}
            if any(item[column.name] is None for column in table.c if not column.nullable):
                raise ValueError("Null value in required column.")
            decoded.append(item)
        primary_keys = [tuple(row[col.name] for col in table.primary_key) for row in decoded]
        if len(primary_keys) != len(set(primary_keys)):
            raise ValueError("Duplicate backup primary key.")
        rows_by_table[name] = decoded
    if sum(len(rows) for rows in rows_by_table.values()) > MAX_ROWS:
        raise ValueError("Backup row limit exceeded.")
    # Validate all FK references before any filesystem or database writes.
    for name, table in metadata.tables.items():
        for constraint in table.foreign_key_constraints:
            columns = [element.parent.name for element in constraint.elements]
            remote = [element.column.name for element in constraint.elements]
            remote_name = next(iter(constraint.elements)).column.table.name
            values = {tuple(row[col] for col in remote) for row in rows_by_table[remote_name]}
            for row in rows_by_table[name]:
                reference = tuple(row[col] for col in columns)
                if all(item is not None for item in reference) and reference not in values:
                    raise ValueError("Invalid backup foreign key reference.")
    owners = {
        (row["id"], row["workspace_id"])
        for row in rows_by_table["actors"]
        if row["kind"] == "owner"
    }
    for version in rows_by_table["skill_versions"]:
        if (version["approved_by"], version["workspace_id"]) not in owners:
            raise ValueError("Skill approval does not belong to an owner.")
        packaged = [
            {
                "path": row["path"],
                "content_base64": base64.b64encode(row["content"]).decode(),
                "executable": row["executable"],
            }
            for row in rows_by_table["skill_files"]
            if row["skill_id"] == version["skill_id"] and row["version"] == version["version"]
        ]
        if package_hash(packaged) != version["package_hash"]:
            raise ValueError("Approved package hash mismatch.")
    for row in rows_by_table["skill_files"]:
        if hashlib.sha256(row["content"]).hexdigest() != row["sha256"]:
            raise ValueError("Approved file hash mismatch.")
    originals: dict[str, bytes] = {}
    file_manifest = payload["files"]
    if not isinstance(file_manifest, dict):
        raise ValueError("Invalid original file manifest.")
    references = {row["storage_key"]: row for row in rows_by_table["source_versions"]}
    for storage_key, info in file_manifest.items():
        if str(UUID(storage_key)) != storage_key or storage_key not in references:
            raise ValueError("Unexpected or unsafe original file.")
        if not isinstance(info, dict) or set(info) != {"content", "sha256", "size"}:
            raise ValueError("Invalid original file manifest.")
        content = base64.b64decode(info["content"], validate=True)
        row = references[storage_key]
        if (
            len(content) != info["size"]
            or len(content) != row["byte_size"]
            or len(content) > 20 * 1024 * 1024
            or hashlib.sha256(content).hexdigest() != info["sha256"]
            or info["sha256"] != row["content_hash"]
        ):
            raise ValueError("Original file manifest mismatch.")
        originals[storage_key] = content
    deleted = {row["id"] for row in rows_by_table["sources"] if row["deleted_at"]}
    for storage_key, row in references.items():
        if str(UUID(storage_key)) != storage_key:
            raise ValueError("Unsafe storage key.")
        if storage_key not in originals and row["source_id"] not in deleted:
            raise ValueError("Backup lacks a live original.")
    return rows_by_table, originals, key


def _require_empty(conn: Connection) -> None:
    names = set(sa.inspect(conn).get_table_names())
    if names - set(metadata.tables) - {"alembic_version"}:
        raise ValueError("Restore target contains unrelated tables.")
    for name in names & set(metadata.tables):
        if conn.execute(sa.select(metadata.tables[name]).limit(1)).first() is not None:
            raise ValueError("Restore target must be empty; existing data is never overwritten.")


def restore_backup(engine: Engine, storage_directory: Path, artifact: Path) -> dict[str, Any]:
    """Validate/decrypt completely, then restore only to an empty LOCAL database.

    Stop API/worker before restoring. Transaction rollback and file cleanup cover
    failures; a power loss can leave private files to remove before retrying. Schema
    migration remains at head on failure. Never restore to an active production DB.
    """
    if engine.url.host not in LOCAL_HOSTS:
        raise ValueError("Restore is limited to an empty local database.")
    encrypted = _read_file(artifact, MAX_BACKUP_BYTES + 1024)
    if not encrypted.startswith(MAGIC) or len(encrypted) < len(MAGIC) + 44:
        raise ValueError("Invalid encrypted backup envelope.")
    offset = len(MAGIC)
    salt, nonce = encrypted[offset : offset + 16], encrypted[offset + 16 : offset + 28]
    try:
        plaintext = AESGCM(_key(salt)).decrypt(nonce, encrypted[offset + 28 :], MAGIC)
        payload = json.loads(plaintext)
    except (InvalidTag, UnicodeError, json.JSONDecodeError):
        raise ValueError(
            "Backup authentication failed; incorrect passphrase or damaged file."
        ) from None
    rows, originals, hmac_key_hex = _validate(payload, _head(engine))
    directory = storage_directory.absolute()
    _safe_directory(directory.parent)
    if directory.exists():
        _safe_directory(directory)
        if any(directory.iterdir()):
            raise ValueError("Restore original storage must be empty.")
    with engine.begin() as conn:
        _require_empty(conn)
    command.upgrade(_config(engine), "head")
    storage = LocalStorage(directory)
    created: list[str] = []
    try:
        with engine.begin() as conn:
            # Lock out ordinary writers throughout population. Do not restore live clients.
            table_names = ", ".join(f'"{name}"' for name in sorted(metadata.tables))
            conn.execute(sa.text(f"LOCK TABLE {table_names} IN ACCESS EXCLUSIVE MODE"))
            _require_empty(conn)
            for table in metadata.sorted_tables:
                values = rows[table.name]
                if table.name == "skills":
                    values = [{**row, "active_version": None} for row in values]
                for start in range(0, len(values), 1000):
                    conn.execute(sa.insert(table), values[start : start + 1000])
            for row in rows["skills"]:
                if row["active_version"] is not None:
                    conn.execute(
                        sa.update(metadata.tables["skills"])
                        .where(metadata.tables["skills"].c.id == row["id"])
                        .values(active_version=row["active_version"])
                    )
            for storage_key, content in originals.items():
                storage.put(storage_key, content)
                created.append(storage_key)
    except BaseException:
        for storage_key in created:
            storage.delete(storage_key)
        raise
    return {
        "tables": {name: len(values) for name, values in rows.items()},
        "originals": len(originals),
        "hmac_key_hex": hmac_key_hex,
    }
