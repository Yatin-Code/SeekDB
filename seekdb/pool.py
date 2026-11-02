from __future__ import annotations

import hashlib
import secrets
import threading
import time

from seekdb.ids import is_fingerprint
from seekdb.index.base import Index
from seekdb.protocol.nvmt import (MAX_DURATION_MS, MAX_NVMT_BYTES, NVMT_PROFILES, TILE_INTERVAL_MS,
                                  TILE_PROFILE, PoolError, decode_nvmt, encode_nvmt, tile_count,
                                  valid_episode_key, validate_tile)
from seekdb.store.base import ObjectStore


class PoolService:
    """Nuvio v1 pool behavior over an Index and an ObjectStore."""

    def __init__(self, index: Index, store: ObjectStore, read_key: str = "", write_key: str = "",
                 max_storage_bytes: int = 0, remote=None, pool_share: bool = False):
        self.index = index
        self.store = store
        self.read_key = read_key
        self.write_key = write_key
        self.max_storage_bytes = max_storage_bytes
        self.remote = remote
        self.pool_share = pool_share
        self.remote_sync = None
        self._lock = threading.RLock()

    def _queue_remote(self, kind: str, fp: str, profile: str, slot: int = -1) -> None:
        if self.pool_share and self.remote is not None:
            if self.remote_sync:
                self.remote_sync.enqueue(kind, fp, profile, slot)
            else:
                self.index.enqueue_remote(kind, fp, profile, slot)

    def authorize(self, given: str | None, write: bool = False) -> bool:
        expected = self.write_key if write else self.read_key
        return bool(given and expected and secrets.compare_digest(given, expected))

    def upload_package(self, fp: str, profile: str, duration_ms: int, data: bytes,
                       episode_key: str | None = None) -> dict:
        grid, n_slots, incoming = decode_nvmt(fp, profile, duration_ms, data)
        if episode_key is not None and not valid_episode_key(episode_key):
            raise PoolError(400, "bad_episode_key")
        with self._lock:
            previous = self.index.get_package(fp, profile)
            merged = dict(incoming)
            if previous:
                if previous["duration_ms"] != duration_ms or previous["n_slots"] != n_slots:
                    raise PoolError(409, "media_metadata_conflict", duration_ms=previous["duration_ms"])
                try:
                    old_data = self._read_package(previous)
                    _old_grid, _old_slots, old_tiles = decode_nvmt(fp, profile, duration_ms, old_data)
                except PoolError as exc:
                    if exc.code != "stored_package_corrupt":
                        raise
                    self.index.delete_package(fp, profile, previous["object_key"])
                    previous = None
                if previous:
                    merged = dict(old_tiles)
                    for slot, tile in incoming.items():
                        merged.setdefault(slot, tile)
            package = encode_nvmt(grid, n_slots, merged)
            if len(package) > MAX_NVMT_BYTES:
                raise PoolError(413, "package_too_large")
            digest = hashlib.sha256(package).hexdigest()
            if previous and digest == previous["sha256"]:
                if episode_key:
                    self.index.add_episode(episode_key, fp, profile, duration_ms)
                self._queue_remote("package", fp, profile)
                return {"stored": False, "duplicate": True, "tiles": len(merged), "sha256": digest}
            object_key = hashlib.sha256(package).hexdigest() + ".nvmt"
            self._check_storage(len(package), object_key)
            try:
                object_key = self.store.put(package, "nvmt")
            except OSError as exc:
                raise PoolError(503, "storage_unavailable") from exc
            meta = {"fp": fp, "profile": profile, "duration_ms": duration_ms, "n_slots": n_slots,
                    "n_tiles": len(merged), "size": len(package), "sha256": digest, "object_key": object_key}
            rows = self._tile_rows(package, merged)
            # Store first, commit the metadata second. A crash between these steps leaves an orphan,
            # which the GC can safely remove; readers never see an index row without its object.
            self.index.put_package(meta, rows, episode_key)
            self._queue_remote("package", fp, profile)
            return {"stored": True, "duplicate": False, "tiles": len(merged), "sha256": digest}

    def _tile_rows(self, package: bytes, tiles: dict[int, tuple[int, bytes]]) -> list[dict]:
        pos = 20
        rows = []
        for _slot, (pts_ms, jpeg) in sorted(tiles.items()):
            slot = int.from_bytes(package[pos:pos + 4], "little")
            size = int.from_bytes(package[pos + 8:pos + 12], "little")
            data_offset = pos + 12
            rows.append({"slot": slot, "pts_ms": pts_ms, "offset": data_offset, "length": size,
                         "sha256": hashlib.sha256(jpeg).hexdigest()})
            pos = data_offset + size
        return rows

    def _read_package(self, meta: dict) -> bytes:
        try:
            data = self.store.get(meta["object_key"])
        except PoolError as exc:
            if exc.code in ("telegram_unavailable", "telegram_timeout"):
                raise
            raise PoolError(502, "stored_package_corrupt") from exc
        except (OSError, ValueError) as exc:
            raise PoolError(502, "stored_package_corrupt") from exc
        if len(data) != meta["size"] or hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise PoolError(502, "stored_package_corrupt")
        try:
            _grid, n_slots, tiles = decode_nvmt(meta["fp"], meta["profile"], meta["duration_ms"], data)
        except PoolError as exc:
            raise PoolError(502, "stored_package_corrupt") from exc
        if n_slots != meta["n_slots"] or len(tiles) != meta["n_tiles"]:
            raise PoolError(502, "stored_package_corrupt")
        return data

    def package(self, fp: str, profile: str, episode_key: str | None = None) -> tuple[bytes, dict] | None:
        if not is_fingerprint(fp) or profile not in NVMT_PROFILES:
            raise PoolError(400, "bad_key")
        meta = self.index.get_package(fp, profile)
        if meta is None:
            return self.remote.get_package(fp, profile, episode_key) if self.remote else None
        if episode_key:
            if not valid_episode_key(episode_key):
                raise PoolError(400, "bad_episode_key")
            self.index.add_episode(episode_key, fp, profile, meta["duration_ms"])
            self._queue_remote("package", fp, profile)
        try:
            data = self._read_package(meta)
        except PoolError as exc:
            if exc.code == "stored_package_corrupt":
                self.index.delete_package(fp, profile, meta["object_key"])
            raise
        return data, {"duration_ms": meta["duration_ms"], "n_slots": meta["n_slots"],
                      "n_tiles": meta["n_tiles"], "sha256": meta["sha256"], "profile": profile}

    def package_slots(self, fp: str, profile: str, slots: list[int],
                      episode_key: str | None = None) -> tuple[bytes, dict] | None:
        if not slots or len(slots) > 12 or any(not isinstance(slot, int) or slot < 0 for slot in slots):
            raise PoolError(400, "bad_slots")
        got = self.package(fp, profile, episode_key)
        if got is None:
            return self._sparse_from_legacy_tiles(fp, profile, slots)
        data, meta = got
        grid, n_slots, tiles = decode_nvmt(fp, profile, meta["duration_ms"], data)
        selected = self._select_slots(slots, tiles)
        if not selected:
            return None
        partial = encode_nvmt(grid, n_slots, selected)
        return partial, {**meta, "n_tiles": len(selected), "sha256": hashlib.sha256(partial).hexdigest()}

    @staticmethod
    def _select_slots(wanted: list[int], tiles: dict[int, tuple[int, bytes]]) -> dict[int, tuple[int, bytes]]:
        selected: dict[int, tuple[int, bytes]] = {}
        for slot in dict.fromkeys(wanted):
            if slot in tiles:
                selected[slot] = tiles[slot]
                continue
            if not tiles:
                continue
            nearest = min(tiles, key=lambda candidate: abs(candidate - slot))
            if abs(nearest - slot) <= 1:
                selected.setdefault(nearest, tiles[nearest])
        return selected

    def _sparse_from_legacy_tiles(self, fp: str, profile: str, slots: list[int]) -> tuple[bytes, dict] | None:
        if not is_fingerprint(fp) or profile not in NVMT_PROFILES:
            raise PoolError(400, "bad_key")
        rows = self.index.get_tile_records(fp, TILE_PROFILE)
        if not rows:
            return None
        duration, n_tiles = rows[0]["duration_ms"], rows[0]["n_tiles"]
        if any(row["duration_ms"] != duration or row["n_tiles"] != n_tiles for row in rows):
            raise PoolError(502, "stored_tile_corrupt")
        grid = NVMT_PROFILES[profile]
        n_slots = (duration + grid - 1) // grid
        tile_step = TILE_INTERVAL_MS // grid
        by_slot = {row["slot"] * tile_step: row for row in rows}
        selected: dict[int, tuple[int, bytes]] = {}
        for wanted in dict.fromkeys(slots):
            if wanted in by_slot:
                row = by_slot[wanted]
                target = wanted
            elif by_slot:
                near = min(by_slot, key=lambda candidate: abs(candidate - wanted))
                if abs(near - wanted) > 1:
                    continue
                row, target = by_slot[near], near
            else:
                continue
            try:
                data = self._read_tile(row)
            except PoolError as exc:
                if exc.code == "stored_tile_corrupt":
                    self.index.delete_tile_record(fp, TILE_PROFILE, row["slot"], row["object_key"])
                raise
            selected[target] = (row["pts_ms"], data)
        if not selected:
            return None
        body = encode_nvmt(grid, n_slots, selected)
        return body, {"duration_ms": duration, "n_slots": n_slots, "n_tiles": len(selected),
                      "sha256": hashlib.sha256(body).hexdigest(), "profile": profile}

    def episode_candidates(self, episode_key: str, duration_ms: int, limit: int = 8) -> list[dict]:
        if not valid_episode_key(episode_key) or not isinstance(duration_ms, int) or not 0 < duration_ms <= MAX_DURATION_MS:
            raise PoolError(400, "bad_episode_key")
        local = self.index.episode_candidates(episode_key, duration_ms, limit)
        if not self.remote:
            return local
        remote = self.remote.episode_candidates(episode_key, duration_ms)
        merged = {(item["fp"], item["profile"]): item for item in local}
        for item in remote:
            merged.setdefault((item["fp"], item["profile"]), item)
        return sorted(merged.values(), key=lambda item: (item["duration_delta_ms"], item["fp"]))[:limit]

    def index_episode_package(self, fp: str, profile: str, episode_key: str) -> bool:
        if not is_fingerprint(fp) or profile not in NVMT_PROFILES or not valid_episode_key(episode_key):
            raise PoolError(400, "bad_episode_key")
        meta = self.index.get_package(fp, profile)
        if not meta:
            return self.remote.index_episode(fp, profile, episode_key) if self.remote else False
        added = self.index.add_episode(episode_key, fp, profile, meta["duration_ms"])
        if added:
            self._queue_remote("package", fp, profile)
        return added

    def upload_tile(self, fp: str, profile: str, slot: int, pts_ms: int, duration_ms: int,
                    n_tiles: int, data: bytes) -> dict:
        digest = validate_tile(fp, profile, slot, pts_ms, duration_ms, n_tiles, data)
        with self._lock:
            previous = self.index.get_tile_record(fp, profile, slot)
            if previous:
                if previous["sha256"] != digest:
                    raise PoolError(409, "tile_conflict")
                self._queue_remote("tile", fp, profile, slot)
                return {"stored": False, "duplicate": True, "sha256": digest}
            media = self.index.get_tile_media(fp, profile)
            if media and (media[0] != duration_ms or media[1] != n_tiles):
                raise PoolError(409, "media_metadata_conflict", duration_ms=media[0])
            key = hashlib.sha256(data).hexdigest() + ".jpg"
            self._check_storage(len(data), key)
            try:
                key = self.store.put(data, "jpg")
            except OSError as exc:
                raise PoolError(503, "storage_unavailable") from exc
            try:
                saved = self.index.put_tile_record({"fp": fp, "profile": profile, "slot": slot, "pts_ms": pts_ms,
                                                    "duration_ms": duration_ms, "n_tiles": n_tiles, "size": len(data),
                                                    "sha256": digest, "object_key": key})
            except ValueError as exc:
                if str(exc) == "media_metadata_conflict":
                    media = self.index.get_tile_media(fp, profile)
                    raise PoolError(409, "media_metadata_conflict", duration_ms=media[0] if media else None) from exc
                raise
            if not saved:
                winner = self.index.get_tile_record(fp, profile, slot)
                if winner and winner["sha256"] == digest:
                    return {"stored": False, "duplicate": True, "sha256": digest}
                raise PoolError(409, "tile_conflict")
            self._queue_remote("tile", fp, profile, slot)
        return {"stored": True, "duplicate": False, "sha256": digest}

    def _read_tile(self, meta: dict) -> bytes:
        try:
            data = self.store.get(meta["object_key"])
        except PoolError as exc:
            if exc.code in ("telegram_unavailable", "telegram_timeout"):
                raise
            raise PoolError(502, "stored_tile_corrupt") from exc
        except (OSError, ValueError) as exc:
            raise PoolError(502, "stored_tile_corrupt") from exc
        if len(data) != meta["size"] or hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise PoolError(502, "stored_tile_corrupt")
        return data

    def tile(self, fp: str, profile: str, slot: int) -> tuple[bytes, dict] | None:
        if not is_fingerprint(fp) or profile != TILE_PROFILE or not isinstance(slot, int) or slot < 0:
            raise PoolError(400, "bad_key")
        meta = self.index.get_tile_record(fp, profile, slot)
        if not meta:
            return self.remote.get_tile(fp, profile, slot) if self.remote else None
        try:
            data = self._read_tile(meta)
        except PoolError as exc:
            if exc.code == "stored_tile_corrupt":
                self.index.delete_tile_record(fp, profile, slot, meta["object_key"])
            raise
        return data, {"pts_ms": meta["pts_ms"], "duration_ms": meta["duration_ms"],
                      "n_tiles": meta["n_tiles"], "sha256": meta["sha256"], "profile": profile}

    def manifest(self, fp: str, profile: str) -> dict | None:
        if not is_fingerprint(fp):
            raise PoolError(400, "bad_key")
        if profile in NVMT_PROFILES:
            meta = self.index.get_package(fp, profile)
            if meta is None:
                return self.remote.manifest(fp, profile) if self.remote else None
            rows = self.index.get_package_tiles(fp, profile)
            return {"fp": fp, "profile": profile, "duration_ms": meta["duration_ms"],
                    "n_slots": meta["n_slots"], "n_tiles": meta["n_tiles"],
                    "tiles": [{"k": row["slot"], "pts_ms": row["pts_ms"], "size": row["length"],
                               "sha256": row["sha256"]} for row in rows]}
        if profile != TILE_PROFILE:
            raise PoolError(400, "bad_key")
        rows = self.index.get_tile_records(fp, profile)
        if not rows:
            return self.remote.manifest(fp, profile) if self.remote else None
        first = rows[0]
        return {"fp": fp, "profile": profile, "duration_ms": first["duration_ms"],
                "n_tiles": first["n_tiles"], "tiles": [
                    {"k": row["slot"], "pts_ms": row["pts_ms"], "size": row["size"], "sha256": row["sha256"]}
                    for row in rows]}

    def gc(self, dry_run: bool = True) -> dict:
        referenced = self.index.referenced_object_keys()
        orphaned = sorted(self.store.keys() - referenced)
        bytes_found = 0
        for key in orphaned:
            try:
                bytes_found += self.store.size(key)
            except OSError:
                continue
        deleted: list[str] = []
        failed: list[str] = []
        if not dry_run:
            for key in orphaned:
                try:
                    self.store.delete(key)
                    deleted.append(key)
                except (OSError, PoolError, ValueError):
                    failed.append(key)
        return {"dry_run": dry_run, "objects": len(orphaned), "bytes": bytes_found,
                "deleted": [] if dry_run else deleted, "failed": [] if dry_run else failed}

    def _check_storage(self, adding_bytes: int, object_key: str) -> None:
        if self.max_storage_bytes <= 0 or self.store.exists(object_key):
            return
        if self.store.usage() + adding_bytes > self.max_storage_bytes:
            raise PoolError(507, "storage_full")
