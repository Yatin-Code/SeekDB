"""Rebuildable channel index adapted from OpenSeek (AGPL-3.0-or-later)."""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from pathlib import Path

from .cache import PROFILE, atomic_write, secure_mkdir
from .db import DB
from .ids import is_fingerprint
from .protocol.nvmt import (MAX_DURATION_MS, MAX_NVMT_BYTES, MAX_TILE_BYTES, NVMT_PROFILES, PoolError,
                            TILE_INTERVAL_MS, decode_nvmt, encode_nvmt, tile_count, valid_episode_key, validate_tile)
from .store.telegram_bot import _tile_metadata_from_filename

log = logging.getLogger("seekdb.channel_index")
_CAPTION = re.compile(r"^openseek:v1:([0-9a-f]{64}):([^:]+):(\d+):([0-9a-f]{64})$")


class ChannelIndex:
    def __init__(self, db: DB, store, *, channel_id: str | None = None,
                 scan_limit: int = 2000, refresh_s: int = 300, clock=time.time):
        self.db, self.store = db, store
        self.cache_root = Path(db.path).parent / "channel-cache"
        secure_mkdir(self.cache_root)
        self.channel_id = str(channel_id or getattr(store, "channel_id", ""))
        self.scan_limit = max(1, int(scan_limit))
        self.refresh_s = max(10, int(refresh_s))
        self.clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    def refresh(self, force: bool = False) -> dict:
        with self._lock:
            state = self._state()
            if not force and state and self.clock() - state[1] < self.refresh_s:
                return {"scanned": 0, "indexed": 0, "last_message_id": state[0], "skipped": True}
            try:
                result = self._refresh_once(state[0] if state else 0)
                self.last_error = ""
                return result
            except Exception as exc:
                self.last_error = getattr(exc, "code", type(exc).__name__)
                raise

    def _refresh_once(self, last_message_id: int) -> dict:
        messages = self.store.scan(last_message_id, self.scan_limit)
        messages = sorted(messages, key=lambda message: int(message.message_id))
        newest = max((int(message.message_id) for message in messages), default=last_message_id)
        now = self.clock()
        indexed = 0
        candidates = []
        seen = set()
        for message in messages:
            tile = self._parse_message(message)
            if tile is None:
                continue
            fp, profile, k, pts_ms, duration_ms, n_tiles, size, digest, message_id = tile
            key = (fp, profile, k)
            if key in seen:
                continue
            existing = self.db.q(
                "SELECT sha256,duration_ms FROM pool_channel_tiles WHERE fp=? AND profile=? AND k=?", key)
            if existing:
                seen.add(key)
                if existing[0][0] == digest and existing[0][1] == 0 and duration_ms > 0:
                    candidates.append(tile)
                continue
            try:
                data = self.store.get_tile(message_id)
                valid_duration = duration_ms or MAX_DURATION_MS
                valid_count = n_tiles or tile_count(valid_duration)
                if len(data) != size or validate_tile(
                        fp, profile, k, pts_ms, valid_duration, valid_count, data) != digest:
                    continue
            except Exception as exc:
                log.debug("ignoring invalid Telegram tile message %d (%s)", message_id, type(exc).__name__)
                continue
            seen.add(key)
            candidates.append(tile)
        with self.db._lock:
            self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                for tile in candidates:
                    fp, profile, k, pts_ms, duration_ms, n_tiles, size, digest, message_id = tile
                    existing = self.db.conn.execute(
                        "SELECT sha256,duration_ms,n_tiles FROM pool_channel_tiles "
                        "WHERE fp=? AND profile=? AND k=?", (fp, profile, k)).fetchone()
                    if existing is None:
                        self.db.conn.execute(
                            "INSERT OR IGNORE INTO pool_channel_tiles "
                            "(fp,profile,k,pts_ms,duration_ms,n_tiles,size,sha256,message_id,created) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (fp, profile, k, pts_ms, duration_ms, n_tiles, size, digest, message_id, now))
                        indexed += 1
                    elif (existing[0] == digest and existing[1] == 0 and duration_ms > 0):
                        # Older host uploads only carried the stable caption. A
                        # matching later post may fill in its filename metadata.
                        self.db.conn.execute(
                            "UPDATE pool_channel_tiles SET pts_ms=?,duration_ms=?,n_tiles=?,size=? "
                            "WHERE fp=? AND profile=? AND k=? AND sha256=? AND duration_ms=0",
                            (pts_ms, duration_ms, n_tiles, size, fp, profile, k, digest))
                self.db.conn.execute(
                    "INSERT INTO pool_channel_state(channel_id,last_message_id,scanned_at) VALUES(?,?,?) "
                    "ON CONFLICT(channel_id) DO UPDATE SET last_message_id=excluded.last_message_id, "
                    "scanned_at=excluded.scanned_at",
                    (self.channel_id, newest, now))
                self.db.conn.execute("COMMIT")
            except BaseException:
                self.db.conn.execute("ROLLBACK")
                raise
        return {"scanned": len(messages), "indexed": indexed, "last_message_id": newest, "skipped": False}

    def rebuild(self) -> dict:
        with self._lock:
            with self.db._lock:
                self.db.conn.execute("BEGIN IMMEDIATE")
                try:
                    self.db.conn.execute("DELETE FROM pool_channel_tiles")
                    self.db.conn.execute("DELETE FROM pool_channel_state WHERE channel_id=?", (self.channel_id,))
                    self.db.conn.execute("COMMIT")
                except BaseException:
                    self.db.conn.execute("ROLLBACK")
                    raise
            total_scanned = total_indexed = 0
            last_message_id = 0
            while True:
                try:
                    result = self._refresh_once(last_message_id)
                    self.last_error = ""
                except Exception as exc:
                    self.last_error = getattr(exc, "code", type(exc).__name__)
                    raise
                total_scanned += result["scanned"]
                total_indexed += result["indexed"]
                last_message_id = result["last_message_id"]
                if result["scanned"] < self.scan_limit or last_message_id == 0:
                    break
            return {"scanned": total_scanned, "indexed": total_indexed,
                    "last_message_id": last_message_id, "skipped": False}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="openseek-channel-index", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.refresh_s):
            try:
                self.refresh(force=True)
            except Exception as exc:
                log.warning("Telegram channel refresh failed (%s)", type(exc).__name__)

    def _state(self) -> tuple[int, float] | None:
        rows = self.db.q("SELECT last_message_id,scanned_at FROM pool_channel_state WHERE channel_id=?",
                         (self.channel_id,))
        return (int(rows[0][0]), float(rows[0][1])) if rows else None

    @staticmethod
    def _parse_message(message) -> tuple | None:
        match = _CAPTION.fullmatch(getattr(message, "caption", "") or "")
        if not match:
            return None
        fp, profile, raw_k, digest = match.groups()
        try:
            k = int(raw_k)
            size = int(message.document_size)
            message_id = int(message.message_id)
        except (TypeError, ValueError, AttributeError):
            return None
        if (not is_fingerprint(fp) or profile != PROFILE or not 0 <= k < 1_000_000 or
                size < 125 or size > MAX_TILE_BYTES or message_id <= 0):
            return None
        metadata = _tile_metadata_from_filename(getattr(message, "file_name", ""), k)
        if metadata is None:
            pts_ms, duration_ms, n_tiles = k * TILE_INTERVAL_MS, 0, 0
        else:
            pts_ms, duration_ms, n_tiles = metadata
        return (fp, profile, k, pts_ms, duration_ms, n_tiles, size, digest, message_id)

    def lookup(self, fp: str, profile: str, k: int) -> dict | None:
        if not is_fingerprint(fp) or profile != PROFILE or not isinstance(k, int) or not 0 <= k < 1_000_000:
            raise PoolError(400, "bad_key")
        rows = self.db.q("SELECT message_id,pts_ms,duration_ms,n_tiles,size,sha256 FROM pool_channel_tiles "
                         "WHERE fp=? AND profile=? AND k=?", (fp, profile, k))
        if not rows:
            return None
        message_id, pts_ms, duration_ms, n_tiles, size, digest = rows[0]
        return {"message_id": int(message_id), "pts_ms": int(pts_ms), "duration_ms": int(duration_ms),
                "n_tiles": int(n_tiles), "size": int(size), "sha256": digest}

    def forget(self, fp: str, profile: str, k: int, message_id: int | None = None) -> bool:
        """Drop a stale or corrupt Telegram reference without affecting a newer winner."""
        if not is_fingerprint(fp) or profile != PROFILE or not isinstance(k, int):
            return False
        if message_id is None:
            return self.db.x("DELETE FROM pool_channel_tiles WHERE fp=? AND profile=? AND k=?",
                             (fp, profile, k)) > 0
        return self.db.x("DELETE FROM pool_channel_tiles WHERE fp=? AND profile=? AND k=? AND message_id=?",
                         (fp, profile, k, int(message_id))) > 0

    def record_message(self, message) -> bool:
        """Index a just-reposted librarian tile while preserving the first digest."""
        tile = self._parse_message(message)
        if tile is None:
            return False
        fp, profile, k, pts_ms, duration_ms, n_tiles, size, digest, message_id = tile
        with self.db._lock:
            self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self.db.conn.execute(
                    "SELECT sha256,duration_ms FROM pool_channel_tiles WHERE fp=? AND profile=? AND k=?",
                    (fp, profile, k)).fetchone()
                if existing is None:
                    self.db.conn.execute(
                        "INSERT INTO pool_channel_tiles "
                        "(fp,profile,k,pts_ms,duration_ms,n_tiles,size,sha256,message_id,created) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (fp, profile, k, pts_ms, duration_ms, n_tiles, size, digest, message_id, self.clock()))
                    accepted = True
                else:
                    accepted = existing[0] == digest
                    if accepted and existing[1] == 0 and duration_ms > 0:
                        self.db.conn.execute(
                            "UPDATE pool_channel_tiles SET pts_ms=?,duration_ms=?,n_tiles=?,size=? "
                            "WHERE fp=? AND profile=? AND k=? AND sha256=? AND duration_ms=0",
                            (pts_ms, duration_ms, n_tiles, size, fp, profile, k, digest))
                self.db.conn.execute("COMMIT")
                return accepted
            except BaseException:
                self.db.conn.execute("ROLLBACK")
                raise

    def manifest(self, fp: str, profile: str) -> dict | None:
        if not is_fingerprint(fp):
            raise PoolError(400, "bad_key")
        if profile in NVMT_PROFILES:
            got = self.package(fp, profile)
            if got is None:
                return None
            data, meta = got
            _grid, _slots, tiles = decode_nvmt(fp, profile, meta["duration_ms"], data)
            return {"fp": fp, "profile": profile, "duration_ms": meta["duration_ms"],
                    "n_slots": meta["n_slots"], "n_tiles": meta["n_tiles"],
                    "tiles": [{"k": slot, "pts_ms": pts, "size": len(jpeg),
                               "sha256": hashlib.sha256(jpeg).hexdigest()}
                              for slot, (pts, jpeg) in sorted(tiles.items())]}
        if profile != PROFILE:
            raise PoolError(400, "bad_key")
        rows = self.db.q("SELECT k,pts_ms,duration_ms,n_tiles,size,sha256 FROM pool_channel_tiles "
                         "WHERE fp=? AND profile=? ORDER BY k", (fp, profile))
        if not rows:
            return None
        known = {(int(row[2]), int(row[3])) for row in rows if int(row[2]) > 0 and int(row[3]) > 0}
        duration_ms, n_tiles = next(iter(known)) if len(known) == 1 else (0, 0)
        return {"fp": fp, "profile": profile, "duration_ms": duration_ms, "n_tiles": n_tiles,
                "tiles": [{"k": int(row[0]), "pts_ms": int(row[1]), "size": int(row[4]), "sha256": row[5]}
                          for row in rows]}

    def episode_candidates(self, episode_key: str, duration_ms: int, limit: int = 8) -> list[dict]:
        if not valid_episode_key(episode_key or "") or not isinstance(duration_ms, int) or duration_ms <= 0:
            raise PoolError(400, "bad_episode_key")
        tolerance = max(90_000, int(duration_ms * 0.15))
        rows = self.db.q(
            "SELECT e.fp,e.profile,e.duration_ms,COUNT(t.k) FROM channel_episodes e "
            "JOIN pool_channel_tiles t ON t.fp=e.fp AND t.profile=? "
            "WHERE e.episode_key=? AND ABS(e.duration_ms-?)<=? "
            "GROUP BY e.fp,e.profile,e.duration_ms ORDER BY ABS(e.duration_ms-?) ASC,e.updated DESC LIMIT ?",
            (PROFILE, episode_key, duration_ms, tolerance, duration_ms, max(1, min(limit, 8))))
        out = []
        for fp, profile, duration, count in rows:
            grid = NVMT_PROFILES.get(profile)
            if grid:
                slots = (int(duration) + grid - 1) // grid
                out.append({"fp": fp, "profile": profile, "duration_ms": int(duration),
                            "duration_delta_ms": abs(int(duration) - duration_ms),
                            "n_slots": slots, "n_tiles": int(count)})
        return out

    def index_episode_package(self, fp: str, profile: str, episode_key: str) -> bool:
        if not is_fingerprint(fp) or profile not in NVMT_PROFILES or not valid_episode_key(episode_key):
            raise PoolError(400, "bad_episode_key")
        rows = self.db.q("SELECT duration_ms FROM pool_channel_tiles WHERE fp=? AND profile=? LIMIT 1",
                         (fp, PROFILE))
        if not rows:
            return False
        duration = int(rows[0][0]) or MAX_DURATION_MS
        self.db.x("INSERT INTO channel_episodes(episode_key,fp,profile,duration_ms,updated) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(episode_key,fp,profile) DO UPDATE SET duration_ms=excluded.duration_ms,updated=excluded.updated",
                  (episode_key, fp, profile, duration, self.clock()))
        return True

    def tile(self, fp: str, profile: str, k: int) -> tuple[bytes, dict] | None:
        row = self.lookup(fp, profile, k)
        if row is None:
            return None
        try:
            data = self._tile_bytes(row["message_id"], row["size"], row["sha256"])
        except PoolError as exc:
            self.forget(fp, profile, k, row["message_id"])
            if exc.status == 404:
                return None
            raise
        duration = row["duration_ms"] or MAX_DURATION_MS
        n_tiles = row["n_tiles"] or tile_count(duration)
        if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            self.forget(fp, profile, k, row["message_id"])
            raise PoolError(502, "stored_tile_corrupt")
        try:
            validate_tile(fp, profile, k, row["pts_ms"], duration, n_tiles, data)
        except PoolError as exc:
            self.forget(fp, profile, k, row["message_id"])
            raise PoolError(502, "stored_tile_corrupt") from exc
        return data, {"pts_ms": row["pts_ms"], "duration_ms": duration, "n_tiles": n_tiles,
                      "sha256": row["sha256"], "profile": profile}

    def _tile_bytes(self, message_id: int, size: int, digest: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PoolError(502, "stored_tile_corrupt")
        path = self.cache_root / f"{digest}.jpg"
        try:
            cached = path.read_bytes()
            if len(cached) == size and hashlib.sha256(cached).hexdigest() == digest:
                return cached
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        data = self.store.get_tile(int(message_id))
        if len(data) != int(size) or hashlib.sha256(data).hexdigest() != digest:
            raise PoolError(502, "stored_tile_corrupt")
        atomic_write(path, data)
        return data

    def package(self, fp: str, profile: str) -> tuple[bytes, dict] | None:
        grid = NVMT_PROFILES.get(profile)
        if not is_fingerprint(fp) or grid is None:
            raise PoolError(400, "bad_key")
        rows = self.db.q("SELECT k,pts_ms,duration_ms,n_tiles,size,sha256,message_id FROM pool_channel_tiles "
                         "WHERE fp=? AND profile=? ORDER BY k", (fp, PROFILE))
        if not rows:
            return None
        duration = int(rows[0][2]) or MAX_DURATION_MS
        n_tiles = int(rows[0][3]) or tile_count(duration)
        if any((int(row[2]) or duration) != duration or (int(row[3]) or n_tiles) != n_tiles for row in rows):
            raise PoolError(502, "stored_tile_corrupt")
        n_slots = (duration + grid - 1) // grid
        step = TILE_INTERVAL_MS // grid
        selected: dict[int, tuple[int, bytes]] = {}
        for row in rows:
            k, pts_ms, _duration, _count, size, digest, message_id = row
            try:
                data = self._tile_bytes(int(message_id), int(size), digest)
            except PoolError as exc:
                if exc.status == 404:
                    self.forget(fp, PROFILE, int(k), int(message_id))
                    continue
                raise
            if len(data) != int(size) or hashlib.sha256(data).hexdigest() != digest:
                self.forget(fp, PROFILE, int(k), int(message_id))
                continue
            try:
                validate_tile(fp, PROFILE, int(k), int(pts_ms), duration, n_tiles, data)
            except PoolError:
                self.forget(fp, PROFILE, int(k), int(message_id))
                continue
            selected[int(k) * step] = (int(pts_ms), data)
        if not selected:
            return None
        data = encode_nvmt(grid, n_slots, selected)
        if len(data) > MAX_NVMT_BYTES:
            raise PoolError(502, "stored_package_too_large")
        return data, {"duration_ms": duration, "n_slots": n_slots, "n_tiles": len(selected),
                      "sha256": hashlib.sha256(data).hexdigest(), "profile": profile}

    def package_slots(self, fp: str, profile: str, slots: list[int]) -> tuple[bytes, dict] | None:
        if not slots or len(slots) > 12 or any(not isinstance(slot, int) or slot < 0 for slot in slots):
            raise PoolError(400, "bad_slots")
        grid = NVMT_PROFILES.get(profile)
        if not is_fingerprint(fp) or grid is None:
            raise PoolError(400, "bad_key")
        rows = self.db.q("SELECT k,pts_ms,duration_ms,n_tiles,size,sha256,message_id "
                         "FROM pool_channel_tiles WHERE fp=? AND profile=? ORDER BY k", (fp, PROFILE))
        if not rows:
            return None
        duration_ms = int(rows[0][2]) or MAX_DURATION_MS
        n_tiles = int(rows[0][3]) or tile_count(duration_ms)
        if (any((int(row[2]) or duration_ms) != duration_ms or (int(row[3]) or n_tiles) != n_tiles for row in rows) or
                n_tiles != (duration_ms + 9_999) // 10_000 or TILE_INTERVAL_MS % grid):
            raise PoolError(502, "stored_tile_corrupt")
        tile_step = TILE_INTERVAL_MS // grid
        by_slot = {int(row[0]) * tile_step: row for row in rows}
        selected_rows = {}
        for wanted in dict.fromkeys(slots):
            if wanted in by_slot:
                selected_rows[wanted] = by_slot[wanted]
            else:
                nearest = min(by_slot, key=lambda candidate: abs(candidate - wanted))
                if abs(nearest - wanted) <= 1:
                    selected_rows.setdefault(nearest, by_slot[nearest])
        if not selected_rows:
            return None
        selected = {}
        for slot, row in selected_rows.items():
            k, pts_ms, _duration, _n, size, digest, message_id = row
            try:
                data = self._tile_bytes(int(message_id), int(size), digest)
            except PoolError as exc:
                if exc.status == 404:
                    self.forget(fp, PROFILE, int(k), int(message_id))
                    continue
                raise
            if len(data) != int(size) or hashlib.sha256(data).hexdigest() != digest:
                raise PoolError(502, "stored_tile_corrupt")
            try:
                validate_tile(fp, PROFILE, int(k), int(pts_ms), duration_ms, n_tiles, data)
            except PoolError as exc:
                raise PoolError(502, "stored_tile_corrupt") from exc
            selected[slot] = (int(pts_ms), data)
        n_slots = (duration_ms + grid - 1) // grid
        data = encode_nvmt(grid, n_slots, selected)
        return data, {"duration_ms": duration_ms, "n_slots": n_slots, "n_tiles": len(selected),
                      "sha256": hashlib.sha256(data).hexdigest(), "profile": profile}

    def status(self) -> dict:
        state = self._state()
        count = self.db.q("SELECT COUNT(*) FROM pool_channel_tiles")
        return {"channel_id": self.channel_id, "last_message_id": state[0] if state else 0,
                "scanned_at": state[1] if state else 0, "tiles": int(count[0][0]) if count else 0,
                "last_error": self.last_error}
