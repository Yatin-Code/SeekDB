from __future__ import annotations

import hashlib
import re

from seekdb.ids import is_fingerprint

MAX_TILE_BYTES = 256 * 1024
MAX_NVMT_BYTES = 16 * 1024 * 1024
MAX_DURATION_MS = 12 * 60 * 60 * 1000
TILE_INTERVAL_MS = 10_000
TILE_W, TILE_H = 320, 180
TILE_PROFILE = "t320x180q75g10v1"
NVMT_PROFILES = {"nvmt-v1-g5": 5_000, "nvmt-v1-g10": 10_000}
PROFILE_INTERVALS = {TILE_PROFILE: TILE_INTERVAL_MS, **NVMT_PROFILES}
_EPISODE_KEY = re.compile(r"[0-9a-f]{64}\Z")


class PoolError(Exception):
    def __init__(self, status: int, code: str, *, duration_ms: int | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.duration_ms = duration_ms


def tile_count(duration_ms: int) -> int:
    return (duration_ms + TILE_INTERVAL_MS - 1) // TILE_INTERVAL_MS


def _read32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little")


def _write32(data: bytearray, offset: int, value: int) -> None:
    data[offset:offset + 4] = int(value).to_bytes(4, "little", signed=False)


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read JPEG frame dimensions with bounded marker parsing; no image codec is loaded."""
    if not isinstance(data, bytes) or len(data) < 125 or data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        return None
    pos = 2
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while pos + 4 <= len(data) - 2:
        if data[pos] != 0xFF:
            return None
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            return None
        marker = data[pos]
        pos += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if pos + 2 > len(data):
            return None
        segment_len = int.from_bytes(data[pos:pos + 2], "big")
        if segment_len < 2 or pos + segment_len > len(data):
            return None
        if marker in sof_markers:
            if segment_len < 8:
                return None
            height = int.from_bytes(data[pos + 3:pos + 5], "big")
            width = int.from_bytes(data[pos + 5:pos + 7], "big")
            return width, height
        if marker == 0xDA:
            return None
        pos += segment_len
    return None


def jpeg_ok(data: bytes) -> bool:
    return jpeg_dimensions(data) == (TILE_W, TILE_H)


def decode_nvmt(fp: str, profile: str, duration_ms: int, data: bytes) -> tuple[int, int, dict[int, tuple[int, bytes]]]:
    grid = NVMT_PROFILES.get(profile)
    if not is_fingerprint(fp) or grid is None:
        raise PoolError(400, "bad_key")
    if not isinstance(duration_ms, int) or not 0 < duration_ms <= MAX_DURATION_MS:
        raise PoolError(400, "bad_duration")
    if not isinstance(data, bytes) or len(data) < 20:
        raise PoolError(400, "bad_package")
    if len(data) > MAX_NVMT_BYTES:
        raise PoolError(413, "package_too_large")
    if data[:4] != b"NVMT" or _read32(data, 4) != 1:
        raise PoolError(400, "bad_package")
    package_grid, n_slots, count = (_read32(data, 8), _read32(data, 12), _read32(data, 16))
    expected_slots = (duration_ms + grid - 1) // grid
    if package_grid != grid or n_slots != expected_slots or count == 0 or count > n_slots or n_slots > 8640:
        raise PoolError(400, "bad_package_metadata")
    pos: int = 20
    tiles: dict[int, tuple[int, bytes]] = {}
    for _ in range(count):
        if pos + 12 > len(data):
            raise PoolError(400, "bad_package")
        slot, pts_raw, size = (_read32(data, pos), _read32(data, pos + 4), _read32(data, pos + 8))
        pts_ms = pts_raw if pts_raw < 0x80000000 else pts_raw - 0x100000000
        pos += 12
        if (slot >= n_slots or slot in tiles or size < 125 or size > 200_000 or pos + size > len(data)
                or not 0 <= pts_ms <= duration_ms or abs(pts_ms - slot * grid) > 10_000):
            raise PoolError(400, "bad_package_tile")
        jpeg = data[pos:pos + size]
        if not jpeg_ok(jpeg):
            raise PoolError(400, "bad_jpeg")
        tiles[slot] = (pts_ms, jpeg)
        pos += size
    if pos != len(data):
        raise PoolError(400, "bad_package")
    return grid, n_slots, tiles


def encode_nvmt(grid: int, n_slots: int, tiles: dict[int, tuple[int, bytes]]) -> bytes:
    data = bytearray(20)
    data[:4] = b"NVMT"
    _write32(data, 4, 1)
    _write32(data, 8, grid)
    _write32(data, 12, n_slots)
    _write32(data, 16, len(tiles))
    for slot, (pts_ms, jpeg) in sorted(tiles.items()):
        record = bytearray(12)
        _write32(record, 0, slot)
        _write32(record, 4, pts_ms)
        _write32(record, 8, len(jpeg))
        data.extend(record)
        data.extend(jpeg)
    return bytes(data)


def validate_tile(fp: str, profile: str, slot: int, pts_ms: int, duration_ms: int,
                  n_tiles: int, data: bytes) -> str:
    if not is_fingerprint(fp) or profile != TILE_PROFILE:
        raise PoolError(400, "bad_key")
    if not isinstance(slot, int) or not 0 <= slot < 1_000_000:
        raise PoolError(400, "bad_tile_index")
    if not isinstance(duration_ms, int) or not 0 < duration_ms <= MAX_DURATION_MS:
        raise PoolError(400, "bad_duration")
    if n_tiles != tile_count(duration_ms) or slot >= n_tiles:
        raise PoolError(400, "bad_tile_count")
    if not isinstance(pts_ms, int) or not 0 <= pts_ms <= duration_ms or abs(pts_ms - slot * TILE_INTERVAL_MS) > 5_000:
        raise PoolError(400, "bad_timestamp")
    if len(data) > MAX_TILE_BYTES or not jpeg_ok(data):
        raise PoolError(400, "bad_jpeg")
    return hashlib.sha256(data).hexdigest()


def valid_episode_key(key: str) -> bool:
    return bool(_EPISODE_KEY.fullmatch(key))
