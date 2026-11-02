from __future__ import annotations

import re
import secrets

_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_DEVICE_ID = re.compile(r"[0-9a-f]{32}\Z")


def is_fingerprint(value: object) -> bool:
    return isinstance(value, str) and bool(_FINGERPRINT.fullmatch(value))


def new_device_id() -> str:
    return secrets.token_hex(16)
