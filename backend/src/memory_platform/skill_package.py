"""Bounded, exact-byte packages; importing and installing never executes code."""

import base64
import binascii
import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memory_platform.api.skill_schemas import SkillFile

MAX_FILE_BYTES = 1024 * 1024
MAX_PACKAGE_BYTES = 6 * 1024 * 1024


@dataclass(frozen=True)
class PackageFile:
    path: str
    data: bytes
    executable: bool
    sha256: str


def _safe_path(path: str) -> None:
    parts = path.split("/")
    if (
        len(path.encode()) > 1024
        or "\\" in path
        or any(ord(char) < 32 for char in path)
        or any(part in {"", ".", ".."} for part in parts)
        or ":" in path
    ):
        raise ValueError("Unsafe package path.")
    for part in parts:
        lower = part.casefold()
        if (
            lower.startswith(".env")
            or lower in {".git", ".ssh", ".aws", "node_modules", "__pycache__"}
            or lower.endswith((".pem", ".key", ".p12", ".pfx"))
            or lower in {"credentials.json", "secrets.json", "id_rsa", "id_ed25519"}
        ):
            raise ValueError("Secret or generated file path is not permitted.")


def validate_package(files: Sequence[SkillFile | Mapping[str, Any]]) -> list[PackageFile]:
    """Validate fully in memory before a caller can persist any package bytes."""
    if not 1 <= len(files) <= 128:
        raise ValueError("Package must contain 1 to 128 files.")
    result: list[PackageFile] = []
    seen: set[str] = set()
    total = 0
    metadata_size = 0
    for item in files:
        value = item if isinstance(item, SkillFile) else SkillFile.model_validate(item)
        _safe_path(value.path)
        key = unicodedata.normalize("NFC", value.path).casefold()
        if key in seen:
            raise ValueError("Package paths collide.")
        seen.add(key)
        if len(value.content_base64) > 1398104:
            raise ValueError("File exceeds 1 MiB.")
        try:
            data = base64.b64decode(value.content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid file encoding.") from exc
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("File exceeds 1 MiB.")
        total += len(data)
        metadata_size += len(value.path.encode()) + 128
        if total > MAX_PACKAGE_BYTES or metadata_size > 65536:
            raise ValueError("Package exceeds its size limit.")
        result.append(
            PackageFile(value.path, data, value.executable, hashlib.sha256(data).hexdigest())
        )
    if not any(item.path == "SKILL.md" for item in result):
        raise ValueError("Root SKILL.md is required.")
    for packaged in result:
        parts = packaged.path.split("/")
        if any(
            unicodedata.normalize("NFC", "/".join(parts[:index])).casefold() in seen
            for index in range(1, len(parts))
        ):
            raise ValueError("A file conflicts with a directory path.")
    return sorted(result, key=lambda item: item.path)


def package_hash(files: Sequence[SkillFile | Mapping[str, Any]]) -> str:
    """Bind approval to a deterministic manifest of paths, exact hashes, sizes, and modes."""
    manifest = [
        {
            "path": item.path,
            "sha256": item.sha256,
            "size": len(item.data),
            "executable": item.executable,
        }
        for item in validate_package(files)
    ]
    encoded = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def pack_directory(directory: Path) -> list[SkillFile]:
    """Read only a selected directory; reject links and special files before reading."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Select a real package directory.")
    files: list[SkillFile] = []

    def visit(descriptor: int, prefix: str) -> None:
        for name in sorted(os.listdir(descriptor)):
            relative = f"{prefix}/{name}" if prefix else name
            _safe_path(relative)
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("Symlinks are not permitted.")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise ValueError("Only bounded regular files are permitted.")
            leaf = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(leaf, "rb") as stream:
                current = os.fstat(stream.fileno())
                if not stat.S_ISREG(current.st_mode):
                    raise ValueError("Only regular files are permitted.")
                data = stream.read(MAX_FILE_BYTES + 1)
            files.append(
                SkillFile(
                    path=relative,
                    content_base64=base64.b64encode(data).decode(),
                    executable=bool(current.st_mode & 0o111),
                )
            )
            if len(files) > 128 or sum(len(item.content_base64) for item in files) > 8388608:
                raise ValueError("Package is too large.")

    root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(root, "")
    except OSError as exc:
        raise ValueError("Unsafe or unreadable package path.") from exc
    finally:
        os.close(root)
    validate_package(files)
    return sorted(files, key=lambda item: item.path)
