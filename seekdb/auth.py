"""Device tokens and capability helpers, adapted from OpenSeek (AGPL-3.0-or-later)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import threading
import time
from pathlib import Path

from .db import DB
from .ids import new_device_id

CAP_MAX_TTL_S = 6 * 3600
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class DeviceAuth:
    """Bearer token -> device_id. Only SHA-256(token) is stored; comparisons are constant-time
    over every registered hash."""

    def __init__(self, db: DB):
        self.db = db
        self._lock = threading.Lock()
        self._hashes: list[tuple[str, str]] = []
        self._roles: dict[str, tuple[str, float | None]] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            rows = self.db.auth_rows()
            self._hashes = [(h, d) for h, d, _r, _e in rows]
            self._roles = {d: (r, e) for _h, d, r, e in rows}

    def register(self, device_name: str, token_hash: str, role: str = "owner", expires: float | None = None,
                 invite_id: str | None = None) -> str:
        device_id = new_device_id()
        self.db.add_device(device_id, device_name, token_hash, role, expires, invite_id)
        self.reload()
        return device_id

    def role_of(self, device_id: str | None) -> str | None:
        """'owner' | 'guest' for a device that exists and has not expired, else None."""
        if device_id is None:
            return None
        with self._lock:
            r = self._roles.get(device_id)
        if r is None or (r[1] is not None and time.time() > r[1]):
            return None
        return r[0]

    def revoke(self, device_id: str) -> bool:
        ok = self.db.remove_device(device_id)
        self.reload()
        return ok

    def authenticate(self, authorization: str | None) -> str | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization[7:].strip()
        if not _TOKEN.fullmatch(token):
            return None
        h = hashlib.sha256(token.encode()).hexdigest()
        found = None
        with self._lock:
            for stored, device_id in self._hashes:
                if hmac.compare_digest(stored, h):
                    found = device_id
        return found if self.role_of(found) is not None else None          # an expired guest is no device at all


class CapSigner:
    """cap = base64url(HMAC-SHA256(server_secret, device_id || preview_id || exp))[0:32]."""

    def __init__(self, secret: bytes, clock=time.time):
        if len(secret) < 32:
            raise ValueError("secret too short")
        self.secret = secret
        self.clock = clock

    def cap(self, device_id: str, preview_id: str, exp: int) -> str:
        mac = hmac.new(self.secret, f"{device_id}{preview_id}{int(exp)}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()[:32]

    def current_exp(self) -> int:
        """Hour-bucketed expiry: URLs stay byte-identical for an hour (clients keep textures keyed
        by URL) while always satisfying exp <= now + 6 h."""
        now = int(self.clock())
        return (now // 3600) * 3600 + CAP_MAX_TTL_S

    def verify(self, cap: str, device_id: str, preview_id: str, exp: int) -> bool:
        now = self.clock()
        if not (now < exp <= now + CAP_MAX_TTL_S):
            return False
        return hmac.compare_digest(self.cap(device_id, preview_id, exp), cap)


def admin_token(data_dir: Path, configured: str = "") -> str:
    """The per-install secret the loopback admin API requires: OPENSEEK_ADMIN_TOKEN, else a random value kept in
    <data>/admin.token (created owner-only). Without it, any local process could pair itself as the owner."""
    if configured:
        if len(configured) < 16:
            raise ValueError("OPENSEEK_ADMIN_TOKEN must be at least 16 characters")
        return configured
    return load_or_create_secret(Path(data_dir) / "admin.token").hex()


def load_or_create_secret(path: Path) -> bytes:
    path = Path(path)
    if path.exists():
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    data = os.urandom(32)
    from .cache import atomic_write
    atomic_write(path, data)
    return data
