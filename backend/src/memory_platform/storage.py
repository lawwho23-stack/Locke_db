"""Private local original-file storage. Keys are generated UUIDs, never user paths."""

import os
from pathlib import Path
from uuid import UUID

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
