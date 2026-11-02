"""Small filesystem helpers shared by storage and TLS setup."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from seekdb.protocol.nvmt import TILE_PROFILE, jpeg_ok

PROFILE = TILE_PROFILE


def secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path = Path(path)
    secure_mkdir(path.parent)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name == "posix":
            os.chmod(path, mode)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
