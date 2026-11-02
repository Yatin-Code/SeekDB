from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from seekdb.cache import secure_mkdir

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'reader', created REAL NOT NULL, last_seen REAL NOT NULL DEFAULT 0,
  revoked REAL, expires REAL, invite_id TEXT, contribute INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invites (
  pair_id TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'guest',
  salt TEXT NOT NULL, iterations INTEGER NOT NULL, k_hex TEXT NOT NULL, created REAL NOT NULL,
  expires REAL NOT NULL, max_uses INTEGER NOT NULL DEFAULT 1, uses INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0, revoked REAL, device_days REAL, tag TEXT,
  contribute INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS invites_tag ON invites(tag, expires);
CREATE TABLE IF NOT EXISTS packages (
  fp TEXT NOT NULL, profile TEXT NOT NULL, duration_ms INTEGER NOT NULL,
  n_slots INTEGER NOT NULL, n_tiles INTEGER NOT NULL, size INTEGER NOT NULL,
  sha256 TEXT NOT NULL, object_key TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY(fp, profile)
);
CREATE TABLE IF NOT EXISTS package_tiles (
  fp TEXT NOT NULL, profile TEXT NOT NULL, slot INTEGER NOT NULL, pts_ms INTEGER NOT NULL,
  offset INTEGER NOT NULL, length INTEGER NOT NULL, sha256 TEXT NOT NULL,
  PRIMARY KEY(fp, profile, slot)
);
CREATE TABLE IF NOT EXISTS episodes (
  episode_key TEXT NOT NULL, fp TEXT NOT NULL, profile TEXT NOT NULL,
  duration_ms INTEGER NOT NULL, updated REAL NOT NULL,
  PRIMARY KEY(episode_key, fp, profile)
);
CREATE INDEX IF NOT EXISTS episode_lookup ON episodes(episode_key, duration_ms);
CREATE TABLE IF NOT EXISTS pool_tiles (
  fp TEXT NOT NULL, profile TEXT NOT NULL, slot INTEGER NOT NULL, pts_ms INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL, n_tiles INTEGER NOT NULL, size INTEGER NOT NULL,
  sha256 TEXT NOT NULL, object_key TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY(fp, profile, slot)
);
CREATE TABLE IF NOT EXISTS pool_channel_tiles (
  fp TEXT NOT NULL, profile TEXT NOT NULL, k INTEGER NOT NULL, pts_ms INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL, n_tiles INTEGER NOT NULL, size INTEGER NOT NULL,
  sha256 TEXT NOT NULL, message_id INTEGER NOT NULL, created REAL NOT NULL,
  PRIMARY KEY(fp, profile, k)
);
CREATE INDEX IF NOT EXISTS channel_tiles_fp ON pool_channel_tiles(fp,profile,k);
CREATE TABLE IF NOT EXISTS pool_channel_state (
  channel_id TEXT PRIMARY KEY, last_message_id INTEGER NOT NULL DEFAULT 0,
  scanned_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pool_channel_outbox (
  fp TEXT NOT NULL, k INTEGER NOT NULL, pts_ms INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
  n_tiles INTEGER NOT NULL, created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  retry_after REAL NOT NULL DEFAULT 0, message_id INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(fp,k)
);
CREATE TABLE IF NOT EXISTS telegram_objects (
  object_key TEXT PRIMARY KEY, file_id TEXT NOT NULL, message_id INTEGER NOT NULL,
  size INTEGER NOT NULL, sha256 TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_episodes (
  episode_key TEXT NOT NULL, fp TEXT NOT NULL, profile TEXT NOT NULL,
  duration_ms INTEGER NOT NULL, updated REAL NOT NULL,
  PRIMARY KEY(episode_key,fp,profile)
);
CREATE INDEX IF NOT EXISTS channel_episode_lookup ON channel_episodes(episode_key,duration_ms);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, key TEXT NOT NULL DEFAULT '', result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS upload_daily (
  device_id TEXT NOT NULL, day INTEGER NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
  packages INTEGER NOT NULL DEFAULT 0, tiles INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(device_id,day)
);
CREATE TABLE IF NOT EXISTS pool_remote_outbox (
  kind TEXT NOT NULL, fp TEXT NOT NULL, profile TEXT NOT NULL, slot INTEGER NOT NULL DEFAULT -1,
  created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, retry_after REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(kind,fp,profile,slot)
);
"""


class SQLiteIndex:
    """Single-node metadata index. Methods return plain values, not live cursors."""

    def __init__(self, path: Path):
        self.path = Path(path)
        secure_mkdir(self.path.parent)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        if self.path.exists():
            self.path.chmod(0o600)

    def __enter__(self) -> "SQLiteIndex":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, args)
            return int(cur.rowcount)

    def tx(self, statements: list[tuple[str, tuple]]) -> None:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for sql, args in statements:
                    self.conn.execute(sql, args)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def get_package(self, fp: str, profile: str) -> dict | None:
        rows = self.q("SELECT duration_ms,n_slots,n_tiles,size,sha256,object_key,created FROM packages WHERE fp=? AND profile=?",
                      (fp, profile))
        if not rows:
            return None
        duration, slots, count, size, digest, key, created = rows[0]
        return {"fp": fp, "profile": profile, "duration_ms": duration, "n_slots": slots,
                "n_tiles": count, "size": size, "sha256": digest, "object_key": key, "created": created}

    def get_package_tiles(self, fp: str, profile: str) -> list[dict]:
        return [{"slot": slot, "pts_ms": pts, "offset": offset, "length": length, "sha256": digest}
                for slot, pts, offset, length, digest in self.q(
                    "SELECT slot,pts_ms,offset,length,sha256 FROM package_tiles WHERE fp=? AND profile=? ORDER BY slot",
                    (fp, profile))]

    def package_episode_key(self, fp: str, profile: str) -> str | None:
        rows = self.q("SELECT episode_key FROM episodes WHERE fp=? AND profile=? ORDER BY updated DESC LIMIT 1",
                      (fp, profile))
        return rows[0][0] if rows else None

    def get_package_tile(self, fp: str, profile: str, slot: int) -> dict | None:
        rows = self.q("SELECT slot,pts_ms,offset,length,sha256 FROM package_tiles "
                      "WHERE fp=? AND profile=? AND slot=?", (fp, profile, slot))
        if not rows:
            return None
        slot, pts, offset, length, digest = rows[0]
        return {"slot": slot, "pts_ms": pts, "offset": offset, "length": length, "sha256": digest}

    def put_package(self, meta: dict, tile_rows: list[dict], episode_key: str | None = None) -> None:
        now = time.time()
        fp, profile = meta["fp"], meta["profile"]
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT INTO packages(fp,profile,duration_ms,n_slots,n_tiles,size,sha256,object_key,created) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(fp,profile) DO UPDATE SET "
                    "duration_ms=excluded.duration_ms,n_slots=excluded.n_slots,n_tiles=excluded.n_tiles,"
                    "size=excluded.size,sha256=excluded.sha256,object_key=excluded.object_key,created=excluded.created",
                    (fp, profile, meta["duration_ms"], meta["n_slots"], meta["n_tiles"], meta["size"],
                     meta["sha256"], meta["object_key"], now),
                )
                self.conn.execute("DELETE FROM package_tiles WHERE fp=? AND profile=?", (fp, profile))
                self.conn.executemany(
                    "INSERT INTO package_tiles(fp,profile,slot,pts_ms,offset,length,sha256) VALUES(?,?,?,?,?,?,?)",
                    [(fp, profile, row["slot"], row["pts_ms"], row["offset"], row["length"], row["sha256"])
                     for row in tile_rows],
                )
                if episode_key:
                    self.conn.execute(
                        "INSERT INTO episodes(episode_key,fp,profile,duration_ms,updated) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(episode_key,fp,profile) DO UPDATE SET duration_ms=excluded.duration_ms,updated=excluded.updated",
                        (episode_key, fp, profile, meta["duration_ms"], now),
                    )
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def delete_package(self, fp: str, profile: str, object_key: str | None = None) -> bool:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if object_key is None:
                    cur = self.conn.execute("DELETE FROM packages WHERE fp=? AND profile=?", (fp, profile))
                else:
                    cur = self.conn.execute("DELETE FROM packages WHERE fp=? AND profile=? AND object_key=?",
                                            (fp, profile, object_key))
                if cur.rowcount:
                    self.conn.execute("DELETE FROM package_tiles WHERE fp=? AND profile=?", (fp, profile))
                    self.conn.execute("DELETE FROM episodes WHERE fp=? AND profile=?", (fp, profile))
                self.conn.commit()
                return bool(cur.rowcount)
            except BaseException:
                self.conn.rollback()
                raise

    def add_episode(self, episode_key: str, fp: str, profile: str, duration_ms: int) -> bool:
        if self.get_package(fp, profile) is None:
            return False
        self.x("INSERT INTO episodes(episode_key,fp,profile,duration_ms,updated) VALUES(?,?,?,?,?) "
               "ON CONFLICT(episode_key,fp,profile) DO UPDATE SET duration_ms=excluded.duration_ms,updated=excluded.updated",
               (episode_key, fp, profile, duration_ms, time.time()))
        return True

    def episode_candidates(self, episode_key: str, duration_ms: int, limit: int = 8) -> list[dict]:
        tolerance = max(90_000, int(duration_ms * 0.15))
        return [{"fp": fp, "profile": profile, "duration_ms": duration,
                 "duration_delta_ms": abs(duration - duration_ms), "n_slots": slots, "n_tiles": count}
                for fp, profile, duration, slots, count in self.q(
                    "SELECT e.fp,e.profile,e.duration_ms,p.n_slots,p.n_tiles FROM episodes e "
                    "JOIN packages p ON p.fp=e.fp AND p.profile=e.profile WHERE e.episode_key=? "
                    "AND ABS(e.duration_ms-?)<=? ORDER BY ABS(e.duration_ms-?) ASC,e.updated DESC LIMIT ?",
                    (episode_key, duration_ms, tolerance, duration_ms, max(1, min(limit, 8))))]

    def referenced_object_keys(self) -> set[str]:
        return {row[0] for row in self.q("SELECT object_key FROM packages UNION SELECT object_key FROM pool_tiles")}

    def put_telegram_object(self, object_key: str, file_id: str, message_id: int, size: int, digest: str) -> None:
        self.x("INSERT INTO telegram_objects(object_key,file_id,message_id,size,sha256,created) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(object_key) DO NOTHING", (object_key, file_id, message_id, size, digest, time.time()))

    def get_telegram_object(self, object_key: str) -> tuple | None:
        rows = self.q("SELECT file_id,message_id,size,sha256 FROM telegram_objects WHERE object_key=?", (object_key,))
        return rows[0] if rows else None

    def delete_telegram_object(self, object_key: str) -> None:
        self.x("DELETE FROM telegram_objects WHERE object_key=?", (object_key,))

    def telegram_object_keys(self) -> set[str]:
        return {row[0] for row in self.q("SELECT object_key FROM telegram_objects")}

    def telegram_object_usage(self) -> int:
        return int(self.q("SELECT COALESCE(SUM(size),0) FROM telegram_objects")[0][0])

    def get_tile_record(self, fp: str, profile: str, slot: int) -> dict | None:
        rows = self.q("SELECT pts_ms,duration_ms,n_tiles,size,sha256,object_key FROM pool_tiles "
                      "WHERE fp=? AND profile=? AND slot=?", (fp, profile, slot))
        if not rows:
            return None
        pts, duration, count, size, digest, key = rows[0]
        return {"fp": fp, "profile": profile, "slot": slot, "pts_ms": pts, "duration_ms": duration,
                "n_tiles": count, "size": size, "sha256": digest, "object_key": key}

    def get_tile_records(self, fp: str, profile: str) -> list[dict]:
        return [{"slot": slot, "pts_ms": pts, "duration_ms": duration, "n_tiles": count,
                 "size": size, "sha256": digest, "object_key": key}
                for slot, pts, duration, count, size, digest, key in self.q(
                    "SELECT slot,pts_ms,duration_ms,n_tiles,size,sha256,object_key FROM pool_tiles "
                    "WHERE fp=? AND profile=? ORDER BY slot", (fp, profile))]

    def get_tile_media(self, fp: str, profile: str) -> tuple[int, int] | None:
        rows = self.q("SELECT duration_ms,n_tiles FROM pool_tiles WHERE fp=? AND profile=? LIMIT 1", (fp, profile))
        return (int(rows[0][0]), int(rows[0][1])) if rows else None

    def put_tile_record(self, meta: dict) -> bool:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self.conn.execute(
                    "SELECT sha256,duration_ms,n_tiles FROM pool_tiles WHERE fp=? AND profile=? AND slot=?",
                    (meta["fp"], meta["profile"], meta["slot"]),
                ).fetchone()
                if existing:
                    self.conn.rollback()
                    return False
                media = self.conn.execute(
                    "SELECT duration_ms,n_tiles FROM pool_tiles WHERE fp=? AND profile=? LIMIT 1",
                    (meta["fp"], meta["profile"]),
                ).fetchone()
                if media and (media[0] != meta["duration_ms"] or media[1] != meta["n_tiles"]):
                    self.conn.rollback()
                    raise ValueError("media_metadata_conflict")
                self.conn.execute(
                    "INSERT INTO pool_tiles(fp,profile,slot,pts_ms,duration_ms,n_tiles,size,sha256,object_key,created) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (meta["fp"], meta["profile"], meta["slot"], meta["pts_ms"], meta["duration_ms"],
                     meta["n_tiles"], meta["size"], meta["sha256"], meta["object_key"], time.time()),
                )
                self.conn.commit()
                return True
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.rollback()
                raise

    def delete_tile_record(self, fp: str, profile: str, slot: int, object_key: str | None = None) -> bool:
        if object_key is None:
            return bool(self.x("DELETE FROM pool_tiles WHERE fp=? AND profile=? AND slot=?", (fp, profile, slot)))
        return bool(self.x("DELETE FROM pool_tiles WHERE fp=? AND profile=? AND slot=? AND object_key=?",
                           (fp, profile, slot, object_key)))

    def upload_usage(self, device_id: str, day: int | None = None) -> dict:
        day = int(time.time() // 86400) if day is None else int(day)
        rows = self.q("SELECT bytes,packages,tiles FROM upload_daily WHERE device_id=? AND day=?", (device_id, day))
        return {"bytes": int(rows[0][0]), "packages": int(rows[0][1]), "tiles": int(rows[0][2])} if rows else {
            "bytes": 0, "packages": 0, "tiles": 0}

    def record_upload(self, device_id: str, byte_count: int, packages: int = 0, tiles: int = 0) -> None:
        day = int(time.time() // 86400)
        self.x("INSERT INTO upload_daily(device_id,day,bytes,packages,tiles) VALUES(?,?,?,?,?) "
               "ON CONFLICT(device_id,day) DO UPDATE SET bytes=bytes+excluded.bytes,"
               "packages=packages+excluded.packages,tiles=tiles+excluded.tiles",
               (device_id, day, byte_count, packages, tiles))

    def record_audit(self, actor: str, action: str, key: str = "", result: str = "ok") -> None:
        self.x("INSERT INTO audit(at,actor,action,key,result) VALUES(?,?,?,?,?)",
               (time.time(), actor[:64], action[:64], key[:16], result[:64]))

    def audit_rows(self, limit: int = 100) -> list[dict]:
        return [{"id": row[0], "at": row[1], "actor": row[2], "action": row[3], "key": row[4], "result": row[5]}
                for row in self.q("SELECT id,at,actor,action,key,result FROM audit ORDER BY id DESC LIMIT ?",
                                  (max(1, min(limit, 500)),))]

    def enqueue_remote(self, kind: str, fp: str, profile: str, slot: int = -1) -> None:
        self.x("INSERT INTO pool_remote_outbox(kind,fp,profile,slot,created) VALUES(?,?,?,?,?) "
               "ON CONFLICT(kind,fp,profile,slot) DO NOTHING", (kind, fp, profile, slot, time.time()))

    def remote_outbox_next(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        rows = self.q("SELECT kind,fp,profile,slot,attempts,retry_after FROM pool_remote_outbox "
                      "WHERE retry_after<=? ORDER BY created LIMIT 1", (now,))
        if not rows:
            return None
        kind, fp, profile, slot, attempts, retry_after = rows[0]
        return {"kind": kind, "fp": fp, "profile": profile, "slot": slot,
                "attempts": attempts, "retry_after": retry_after}

    def remote_outbox_retry(self, row: dict, attempts: int, retry_after: float) -> None:
        self.x("UPDATE pool_remote_outbox SET attempts=?,retry_after=? WHERE kind=? AND fp=? AND profile=? AND slot=?",
               (attempts, retry_after, row["kind"], row["fp"], row["profile"], row["slot"]))

    def remote_outbox_finish(self, row: dict) -> None:
        self.x("DELETE FROM pool_remote_outbox WHERE kind=? AND fp=? AND profile=? AND slot=?",
               (row["kind"], row["fp"], row["profile"], row["slot"]))

    def remote_outbox_pending(self) -> int:
        return int(self.q("SELECT COUNT(*) FROM pool_remote_outbox")[0][0])

    def pool_status(self) -> dict:
        packages = self.q("SELECT COUNT(*),COALESCE(SUM(size),0) FROM packages")[0]
        tiles = self.q("SELECT COUNT(*) FROM pool_tiles")[0][0]
        devices = self.q("SELECT COUNT(*) FROM devices WHERE revoked IS NULL")[0][0]
        channel_tiles = self.q("SELECT COUNT(*) FROM pool_channel_tiles")[0][0]
        return {"packages": int(packages[0]), "package_bytes": int(packages[1]),
                "tiles": int(tiles), "channel_tiles": int(channel_tiles), "devices": int(devices)}

    def auth_rows(self) -> list[tuple[str, str, str, float | None]]:
        return self.q("SELECT token_hash,device_id,role,expires FROM devices WHERE revoked IS NULL")

    def add_device(self, device_id: str, name: str, token_hash: str, role: str = "reader",
                   expires: float | None = None, invite_id: str | None = None) -> None:
        self.x("INSERT INTO devices(device_id,name,token_hash,role,created,last_seen,expires,invite_id) "
               "VALUES(?,?,?,?,?,?,?,?)", (device_id, name, token_hash, role, time.time(), time.time(), expires, invite_id))

    def devices(self) -> list[dict]:
        return [{"device_id": row[0], "name": row[1], "role": row[2], "created": row[3],
                 "last_seen": row[4], "revoked": row[5], "expires": row[6]}
                for row in self.q("SELECT device_id,name,role,created,last_seen,revoked,expires FROM devices ORDER BY created DESC")]

    def remove_device(self, device_id: str) -> bool:
        return bool(self.x("UPDATE devices SET revoked=? WHERE device_id=? AND revoked IS NULL", (time.time(), device_id)))

    def touch_device(self, device_id: str) -> None:
        self.x("UPDATE devices SET last_seen=? WHERE device_id=? AND revoked IS NULL", (time.time(), device_id))

    def set_contribute(self, device_id: str, on: bool) -> bool:
        return bool(self.x("UPDATE devices SET contribute=? WHERE device_id=? AND revoked IS NULL", (int(on), device_id)))

    def can_contribute(self, device_id: str) -> bool:
        rows = self.q("SELECT contribute FROM devices WHERE device_id=? AND revoked IS NULL", (device_id,))
        return bool(rows and rows[0][0])

    def get_setting(self, key: str) -> str | None:
        rows = self.q("SELECT v FROM settings WHERE k=?", (key,))
        return rows[0][0] if rows else None

    def set_setting(self, key: str, value: str) -> None:
        self.x("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

    # InviteManager's persistence interface (OSP1 invite material is hashed/derived before this point).
    def add_invite(self, pair_id: str, label: str, role: str, salt: str, iterations: int, k_hex: str,
                   created: float, expires: float, max_uses: int, device_days: float | None,
                   tag: str, contribute: bool) -> None:
        self.x("INSERT INTO invites(pair_id,label,role,salt,iterations,k_hex,created,expires,max_uses,device_days,tag,contribute) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (pair_id, label, role, salt, iterations, k_hex, created,
                                                    expires, max_uses, device_days, tag, int(contribute)))

    def tag_in_use(self, tag: str, now: float) -> bool:
        return bool(self.q("SELECT 1 FROM invites WHERE tag=? AND expires>? AND revoked IS NULL AND uses<max_uses LIMIT 1",
                           (tag, now)))

    def invite_by_tag(self, tag: str, now: float) -> str | None:
        rows = self.q("SELECT pair_id FROM invites WHERE tag=? AND expires>? AND revoked IS NULL AND uses<max_uses "
                      "AND attempts<5 ORDER BY created DESC LIMIT 1", (tag, now))
        return rows[0][0] if rows else None

    def invite(self, pair_id: str) -> dict | None:
        rows = self.q("SELECT pair_id,label,role,salt,iterations,k_hex,created,expires,max_uses,uses,attempts,revoked,device_days,tag,contribute "
                      "FROM invites WHERE pair_id=?", (pair_id,))
        if not rows:
            return None
        names = ("pair_id", "label", "role", "salt", "iterations", "k_hex", "created", "expires", "max_uses",
                 "uses", "attempts", "revoked", "device_days", "tag", "contribute")
        result = dict(zip(names, rows[0]))
        result["contribute"] = bool(result["contribute"])
        return result

    def invites(self) -> list[dict]:
        rows = self.q("SELECT pair_id,label,role,salt,iterations,k_hex,created,expires,max_uses,uses,attempts,revoked,device_days,tag,contribute FROM invites ORDER BY created DESC")
        names = ("pair_id", "label", "role", "salt", "iterations", "k_hex", "created", "expires", "max_uses",
                 "uses", "attempts", "revoked", "device_days", "tag", "contribute")
        return [dict(zip(names, row)) for row in rows]

    def bump_invite_attempts(self, pair_id: str) -> None:
        self.x("UPDATE invites SET attempts=attempts+1 WHERE pair_id=?", (pair_id,))

    def use_invite(self, pair_id: str, now: float, max_attempts: int) -> bool:
        return bool(self.x("UPDATE invites SET uses=uses+1 WHERE pair_id=? AND revoked IS NULL AND uses<max_uses "
                           "AND attempts<? AND expires>?", (pair_id, max_attempts, now)))

    def revoke_invite(self, pair_id: str, now: float) -> bool:
        return bool(self.x("UPDATE invites SET revoked=? WHERE pair_id=? AND revoked IS NULL", (now, pair_id)))

    def close(self) -> None:
        with self._lock:
            self.conn.close()


# Existing, tested auth/pairing modules type against this name. The implementation lives
# under index/sqlite.py so other Index implementations can replace it later.
DB = SQLiteIndex
