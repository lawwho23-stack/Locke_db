"""Private local original-file storage. Keys are generated UUIDs, never user paths."""

import os
from pathlib import Path
from typing import Protocol
from uuid import UUID

from memory_platform.config import Settings
from memory_platform.errors import AppError, ErrorCode


class LocalStorage:
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory).absolute()
        if self.directory.is_symlink():
            raise ValueError("Storage root must not be a symlink.")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)

    def _path(self, key: str) -> Path:
        try:
            if str(UUID(key)) != key:
                raise ValueError
        except ValueError:
            raise AppError(ErrorCode.bad_request, "Invalid storage key.") from None
        return self.directory / key

    def put(self, key: str, content: bytes) -> None:
        path = self._path(key)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(content)

    def get(self, key: str) -> bytes:
        fd = os.open(self._path(key), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            return source.read(20 * 1024 * 1024 + 1)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class Storage(Protocol):
    def put(self, key: str, content: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


def blob_path(key: str) -> str:
    if str(UUID(key)) != key:
        raise ValueError("Invalid storage key.")
    return "sources/" + key


class BlobStorage:
    """Private originals, addressed only by application-generated UUIDs."""

    def __init__(self, token: str) -> None:
        self.token = token

    def put(self, key: str, content: bytes) -> None:
        from vercel.blob import BlobClient

        with BlobClient(token=self.token) as client:
            client.put(blob_path(key), content, access="private", overwrite=False)

    def get(self, key: str) -> bytes:
        from vercel.blob import BlobClient, BlobNotFoundError

        try:
            with BlobClient(token=self.token) as client:
                result = client.get(blob_path(key), access="private", use_cache=False)
        except BlobNotFoundError:
            raise FileNotFoundError("Source original not found.") from None
        else:
            if result is None:
                raise FileNotFoundError("Source original not found.")
            if result.status_code != 200 or len(result.content) > 20 * 1024 * 1024:
                raise OSError("Invalid source original.")
            return bytes(result.content)

    def delete(self, key: str) -> None:
        from vercel.blob import BlobClient

        with BlobClient(token=self.token) as client:
            client.delete(blob_path(key))


def configured_storage(settings: Settings) -> Storage:
    if settings.storage_provider == "vercel_blob":
        if not settings.blob_read_write_token:
            raise AppError(ErrorCode.dependency_unavailable, "Private storage is not configured.")
        return BlobStorage(settings.blob_read_write_token.get_secret_value())
    return LocalStorage(settings.storage_dir)
