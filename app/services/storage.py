from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


class StorageError(Exception):
    pass


@dataclass(frozen=True)
class StoredFile:
    content_hash: str
    size_bytes: int
    storage_reference: str


class LocalDocumentStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _path_for_hash(self, content_hash: str) -> Path:
        return self.root / content_hash[:2] / content_hash[2:4] / content_hash

    def store(self, content: bytes, *, expected_hash: str | None = None) -> StoredFile:
        if not content:
            raise StorageError("attachment is empty")
        content_hash = hashlib.sha256(content).hexdigest()
        if expected_hash and not content_hash == expected_hash.lower():
            raise StorageError("attachment content hash does not match expected hash")
        destination = self._path_for_hash(content_hash)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            fd, temporary_name = tempfile.mkstemp(prefix=f".{content_hash}.", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as temporary_file:
                    temporary_file.write(content)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
                os.replace(temporary_name, destination)
            except Exception:
                Path(temporary_name).unlink(missing_ok=True)
                raise
        return StoredFile(content_hash, len(content), self.reference_for_hash(content_hash))

    def reference_for_hash(self, content_hash: str) -> str:
        return f"local://documents/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"

    def resolve(self, storage_reference: str) -> Path:
        prefix = "local://documents/"
        if not storage_reference.startswith(prefix):
            raise StorageError("unsupported storage reference")
        relative = storage_reference[len(prefix):]
        path = (self.root / relative).resolve()
        if self.root.resolve() not in path.parents:
            raise StorageError("storage reference escapes storage root")
        if not path.is_file():
            raise StorageError("stored document is not available")
        return path

    def read(self, storage_reference: str) -> bytes:
        return self.resolve(storage_reference).read_bytes()

