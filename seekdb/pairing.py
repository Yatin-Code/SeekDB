"""OSP1 pairing adapted from OpenSeek (AGPL-3.0-or-later).

  code_norm    = uppercase(code) with spaces/dashes removed
  K            = PBKDF2-HMAC-SHA256(code_norm, "OSP1" || pair_id || salt || spki_pin, iterations, 32)
  client_proof = hex(HMAC-SHA256(K, "client" || pair_id || device_name))
  server_proof = hex(HMAC-SHA256(K, "server" || pair_id || hex(SHA-256(token))))
All strings UTF-8; pair_id and salt are the 32-hex-char strings; spki_pin is the full
"sha256//<base64>" string; hex is lowercase.

Windows: opened only by a local admin action, at most one at a time, 10 minutes, single use,
burned after 5 wrong proofs; global rate limits on start and claim. K is derived once when the
window opens (the server knows every input), so claims cost one HMAC.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"   # 31 symbols (~39.6 bits for 8)
CODE_LEN = 8
ITERATIONS = 600_000
MIN_CLIENT_ITERATIONS = 200_000
WINDOW_S = 600.0
MAX_ATTEMPTS = 5
CLAIMS_PER_MIN = 10
STARTS_PER_MIN = 30
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def normalize_code(typed: str) -> str:
    return "".join(ch for ch in typed.upper() if ch not in " -")


def derive_key(code: str, pair_id: str, salt: str, spki_pin: str, iterations: int) -> bytes:
    pw = normalize_code(code).encode("utf-8")
    msg = ("OSP1" + pair_id + salt + spki_pin).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", pw, msg, iterations, 32)


def client_proof(k: bytes, pair_id: str, device_name: str) -> str:
    return hmac.new(k, ("client" + pair_id + device_name).encode("utf-8"), hashlib.sha256).hexdigest()


def server_proof(k: bytes, pair_id: str, token: str) -> str:
    th = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.new(k, ("server" + pair_id + th).encode("utf-8"), hashlib.sha256).hexdigest()


def new_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")  # 43 chars


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_device_name(name) -> bool:
    return (isinstance(name, str) and 1 <= len(name) <= 64
            and not any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 for c in name))


class PairingClosed(Exception):
    pass


class PairingFailed(Exception):
    pass


class RateLimited(Exception):
    pass


@dataclass
class _Window:
    code: str
    pair_id: str
    salt: str
    key: bytes
    opened: float
    attempts: int = 0
    used: bool = False


class _Limiter:
    def __init__(self, per_minute: int, clock: Callable[[], float]):
        self.n = per_minute
        self.clock = clock
        self.events: deque[float] = deque()

    def hit(self) -> bool:
        now = self.clock()
        while self.events and now - self.events[0] > 60:
            self.events.popleft()
        if len(self.events) >= self.n:
            return False
        self.events.append(now)
        return True


INVITE_CODE_LEN = 12                       # 31^12 ~ 2^59: an invite may live for days, so the code is stronger
INVITE_TAG_LEN = 4                         # public selector typed in front of the secret (31^4 distinct tags)
INVITE_MIN_TTL_S = 600
INVITE_MAX_TTL_S = 30 * 86400
INVITE_DEFAULT_TTL_S = 86400


def display_invite_code(code: str) -> str:
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


class InviteManager:
    """Owner-created, one-time, expiring, revocable invitations on the SAME OSP1 protocol as the pairing window.
    Only the derived key is stored (never the code); the wrong-proof counter and the single use are persisted, so a
    restart cannot reset them. A successful claim registers a device with the invite's role (guest by default)."""

    def __init__(self, db, spki_pin: str, register_device: Callable[..., str], clock: Callable[[], float] = time.time,
                 iterations: int | None = None):
        self.db, self.spki_pin, self.register_device, self.clock = db, spki_pin, register_device, clock
        self.iterations = iterations or ITERATIONS

    def create(self, label: str = "", ttl_s: float = INVITE_DEFAULT_TTL_S, role: str = "guest",
               device_days: float | None = None, contribute: bool = True, max_uses: int = 1) -> dict:
        if role not in ("reader", "contributor", "guest"):
            raise ValueError("invalid invite role")
        if not isinstance(max_uses, int) or not 1 <= max_uses <= 100_000:
            raise ValueError("max_uses must be between 1 and 100000")
        ttl = min(INVITE_MAX_TTL_S, max(INVITE_MIN_TTL_S, float(ttl_s)))
        code = new_code_n(INVITE_CODE_LEN)                  # the secret part (12 characters)
        pair_id, salt = secrets.token_hex(16), secrets.token_hex(16)
        k = derive_key(code, pair_id, salt, self.spki_pin, self.iterations)
        now = self.clock()
        tag = new_code_n(INVITE_TAG_LEN)                    # public: names the invitation when only a typed code is available
        while self.db.tag_in_use(tag, now):
            tag = new_code_n(INVITE_TAG_LEN)
        enabled = bool(contribute) and role in ("guest", "contributor")
        self.db.add_invite(pair_id, (label or "")[:64], role, salt, self.iterations, k.hex(), now, now + ttl,
                           max_uses, float(device_days) if device_days else None, tag, enabled)
        return {"invite_id": pair_id, "tag": tag, "secret": display_invite_code(code),
                "code": display_invite_code(tag + code),    # what a person types: tag + secret (16 characters)
                "expires": now + ttl, "expires_in_s": int(ttl), "role": role, "spki_pin": self.spki_pin,
                "contribute": enabled}

    def _live(self, pair_id: str) -> dict | None:
        inv = self.db.invite(pair_id)
        if (inv is None or inv["revoked"] is not None or inv["uses"] >= inv["max_uses"] or inv["attempts"] >= MAX_ATTEMPTS
                or self.clock() >= inv["expires"]):
            return None
        return inv

    def start(self, pair_id: str) -> dict:
        """`pair_id` is the invitation's id (from a join string) or its public 4-character tag (from a typed code)."""
        if isinstance(pair_id, str) and len(pair_id) <= INVITE_TAG_LEN + 1:
            tag = "".join(ch for ch in pair_id.upper() if ch not in " -")
            pair_id = self.db.invite_by_tag(tag, self.clock()) if len(tag) == INVITE_TAG_LEN else None
        if not isinstance(pair_id, str) or not _HEX32.fullmatch(pair_id):
            raise PairingClosed()
        inv = self._live(pair_id)
        if inv is None:
            raise PairingClosed()                  # unknown, expired, used, burned and revoked look the same
        return {"pair_id": pair_id, "salt": inv["salt"], "spki_pin": self.spki_pin, "iterations": inv["iterations"]}

    def knows(self, pair_id: str) -> bool:
        return isinstance(pair_id, str) and bool(_HEX32.fullmatch(pair_id)) and self.db.invite(pair_id) is not None

    def claim(self, pair_id: str, device_name: str, proof: str) -> dict:
        inv = self._live(pair_id)
        if inv is None:
            raise PairingFailed()
        key = bytes.fromhex(inv["k_hex"])
        if not hmac.compare_digest(client_proof(key, pair_id, device_name), proof):
            self.db.bump_invite_attempts(pair_id)   # 5 wrong proofs burn the invite
            raise PairingFailed()
        if not self.db.use_invite(pair_id, self.clock(), MAX_ATTEMPTS):
            raise PairingFailed()                   # lost a race, expired meanwhile, or already used: single use holds
        expires = (self.clock() + inv["device_days"] * 86400) if inv["device_days"] else None
        token = new_token()
        device_id = self.register_device(device_name, token_hash(token), inv["role"], expires, pair_id)
        if inv["role"] in ("guest", "contributor") and inv.get("contribute"):
            self.db.set_contribute(device_id, True)    # a trusted invitation is a Contributor by default (still needs the host's switch)
        return {"device_id": device_id, "token": token, "server_proof": server_proof(key, pair_id, token),
                "role": inv["role"]}

    def revoke(self, pair_id: str) -> bool:
        return self.db.revoke_invite(pair_id, self.clock())

    def listing(self) -> list[dict]:
        now = self.clock()
        out = []
        for i in self.db.invites():
            state = ("revoked" if i["revoked"] is not None else "used" if i["uses"] >= i["max_uses"] else
                     "burned" if i["attempts"] >= MAX_ATTEMPTS else "expired" if now >= i["expires"] else "open")
            out.append({"invite_id": i["pair_id"], "label": i["label"], "role": i["role"], "state": state,
                        "expires_in_s": max(0, int(i["expires"] - now)), "uses": i["uses"], "max_uses": i["max_uses"],
                        "attempts_left": max(0, MAX_ATTEMPTS - i["attempts"])})
        return out


def new_code_n(n: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


class PairingManager:
    def __init__(self, spki_pin: str, register_device: Callable[[str, str], str],
                 clock: Callable[[], float] = time.monotonic, iterations: int = ITERATIONS,
                 invites: "InviteManager | None" = None):
        """register_device(device_name, token_hash) -> device_id (persists the hashed token)."""
        self.spki_pin = spki_pin
        self.register_device = register_device
        self.clock = clock
        self.iterations = iterations
        self._w: _Window | None = None
        self._lock = threading.Lock()
        self._starts = _Limiter(STARTS_PER_MIN, clock)
        self._claims = _Limiter(CLAIMS_PER_MIN, clock)
        self.invites = invites
        self.sharing_enabled: Callable[[], bool] = lambda: False        # set by Core: new guests only while sharing is on

    # ---------------------------------------------------------------- admin
    def open_window(self) -> tuple[str, float]:
        code = new_code()
        pair_id, salt = secrets.token_hex(16), secrets.token_hex(16)
        k = derive_key(code, pair_id, salt, self.spki_pin, self.iterations)
        with self._lock:
            self._w = _Window(code, pair_id, salt, k, self.clock())
        return display_code(code), WINDOW_S

    def close_window(self) -> None:
        with self._lock:
            self._w = None

    def _live(self) -> _Window | None:
        w = self._w
        if w is None or w.used or w.attempts >= MAX_ATTEMPTS or self.clock() - w.opened > WINDOW_S:
            return None
        return w

    def is_open(self) -> bool:
        with self._lock:
            return self._live() is not None

    def admin_status(self) -> dict:
        with self._lock:
            w = self._live()
            if w is None:
                burned = self._w is not None and self._w.attempts >= MAX_ATTEMPTS
                return {"open": False, "burned": burned}
            return {"open": True, "code": display_code(w.code),
                    "expires_in_s": int(WINDOW_S - (self.clock() - w.opened)),
                    "attempts_left": MAX_ATTEMPTS - w.attempts}

    # ---------------------------------------------------------------- protocol
    def start(self, invite: str | None = None) -> dict:
        if invite is not None:
            with self._lock:
                if not self._starts.hit():
                    raise RateLimited()
            if self.invites is None or not self.sharing_enabled():
                raise PairingClosed()
            return self.invites.start(invite)
        with self._lock:
            if not self._starts.hit():
                raise RateLimited()
            w = self._live()
            if w is None:
                raise PairingClosed()
            return {"pair_id": w.pair_id, "salt": w.salt, "spki_pin": self.spki_pin,
                    "iterations": self.iterations}

    def claim(self, pair_id: str, device_name: str, proof: str) -> dict:
        if not isinstance(pair_id, str) or not _HEX32.fullmatch(pair_id):
            raise PairingFailed()
        if not isinstance(proof, str) or not _HEX64.fullmatch(proof):
            raise PairingFailed()
        if not valid_device_name(device_name):
            raise PairingFailed()
        if self.invites is not None and self.invites.knows(pair_id):
            with self._lock:
                if not self._claims.hit():
                    raise RateLimited()
            if not self.sharing_enabled():
                raise PairingFailed()
            return self.invites.claim(pair_id, device_name, proof)
        with self._lock:
            if not self._claims.hit():
                raise RateLimited()
            w = self._live()
            if w is None or not hmac.compare_digest(w.pair_id, pair_id):
                raise PairingFailed()
            expected = client_proof(w.key, w.pair_id, device_name)
            if not hmac.compare_digest(expected, proof):
                w.attempts += 1           # 5 wrong proofs burn the window
                raise PairingFailed()
            w.used = True                 # single use: replays fail
            key, pid = w.key, w.pair_id
        token = new_token()
        device_id = self.register_device(device_name, token_hash(token))
        return {"device_id": device_id, "token": token, "server_proof": server_proof(key, pid, token), "role": "owner"}
