from __future__ import annotations

import hashlib
import re
from pathlib import Path

from seekdb.cache import atomic_write, secure_mkdir
from seekdb.store.base import ObjectStore

_KEY = re.compile(r"([0-9a-f]{64})\.(nvmt|jpg)\Z")
_SUFFIX = re.compile(r"(?:nvmt|jpg)\Z")


class FileSystemObjectStore(ObjectStore):
    """Content-addressed filesystem store with atomic, owner-only writes."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "objects"
        secure_mkdir(self.root)

    def _path(self, object_key: str) -> Path:
        match = _KEY.fullmatch(object_key)
        if not match:
            raise ValueError("invalid object key")
        digest = match.group(1)
        return self.root / digest[:2] / object_key

    def put(self, data: bytes, suffix: str = "nvmt") -> str:
        if not isinstance(data, bytes) or not data or not _SUFFIX.fullmatch(suffix):
            raise ValueError("invalid object")
        digest = hashlib.sha256(data).hexdigest()
        key = f"{digest}.{suffix}"
        path = self._path(key)
        if path.exists():
            try:
                current = path.read_bytes()
                if len(current) == len(data) and hashlib.sha256(current).hexdigest() == digest:
                    return key
            except OSError:
                pass
            # The key is derived from the already validated bytes, so replacing
            # a damaged object at this path repairs it without changing refs.
            atomic_write(path, data)
            return key
        secure_mkdir(path.parent)
        atomic_write(path, data)
        return key

    def get(self, object_key: str) -> bytes:
        return self._path(object_key).read_bytes()

    def delete(self, object_key: str) -> None:
        try:
            self._path(object_key).unlink()
        except FileNotFoundError:
            pass

    def keys(self) -> set[str]:
        found: set[str] = set()
        for path in self.root.glob("[0-9a-f][0-9a-f]/*"):
            if path.is_file() and _KEY.fullmatch(path.name):
                found.add(path.name)
        return found

    def exists(self, object_key: str) -> bool:
        return self._path(object_key).is_file()

    def usage(self) -> int:
        total = 0
        for path in self.root.glob("[0-9a-f][0-9a-f]/*"):
            if path.is_file() and _KEY.fullmatch(path.name):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def size(self, object_key: str) -> int:
        return self._path(object_key).stat().st_size
