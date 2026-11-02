from __future__ import annotations

import hashlib
import http.client
import json
import logging
import threading
import time
import urllib.parse
import ssl

from seekdb.protocol.nvmt import MAX_NVMT_BYTES, MAX_TILE_BYTES, PoolError, decode_nvmt, validate_tile

log = logging.getLogger("seekdb.remote")


class RemotePoolClient:
    """Small, no-redirect HTTP client for a configured SeekDB/OpenSeek host."""

    def __init__(self, base_url: str, read_key: str, write_key: str = "", timeout_s: float = 2.0):
        self.base = urllib.parse.urlsplit(base_url)
        self.prefix = self.base.path.rstrip("/")
        self.read_key = read_key
        self.write_key = write_key
        self.timeout_s = timeout_s

    def _request(self, method: str, path: str, *, key: str = "", data: bytes | None = None,
                 content_type: str = "application/octet-stream", max_bytes: int = MAX_NVMT_BYTES):
        if self.base.scheme == "https":
            conn = http.client.HTTPSConnection(self.base.hostname, self.base.port or 443,
                                               timeout=self.timeout_s, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(self.base.hostname, self.base.port or 80, timeout=self.timeout_s)
        full_path = self.prefix + path
        headers = {"Accept": "application/vnd.openseek.nvmt, application/json, image/jpeg"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if data is not None:
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(len(data))
        try:
            conn.request(method, full_path, body=data, headers=headers)
            response = conn.getresponse()
            status = response.status
            response_headers = {name.lower(): value for name, value in response.getheaders()}
            body = response.read(max_bytes + 1)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise PoolError(503, "remote_unavailable") from exc
        finally:
            conn.close()
        if len(body) > max_bytes:
            raise PoolError(502, "remote_response_too_large")
        if 300 <= status < 400:
            raise PoolError(502, "remote_redirect_refused")
        return status, response_headers, body

    def get_package(self, fp: str, profile: str, episode_key: str | None = None) -> tuple[bytes, dict] | None:
        values = {"profile": profile}
        if episode_key:
            values["episode_key"] = episode_key
        query = urllib.parse.urlencode(values)
        status, headers, body = self._request("GET", f"/v1/pool/{fp}/nvmt?{query}", key=self.read_key)
        if status == 404:
            return None
        if status != 200:
            raise PoolError(503 if status >= 500 else 502, "remote_read_failed")
        try:
            duration = int(headers["x-openseek-duration-ms"])
            grid, n_slots, tiles = decode_nvmt(fp, profile, duration, body)
            digest = hashlib.sha256(body).hexdigest()
            if headers.get("x-openseek-sha256") != digest:
                raise ValueError("digest")
        except (KeyError, ValueError, PoolError) as exc:
            raise PoolError(502, "remote_response_invalid") from exc
        return body, {"duration_ms": duration, "n_slots": n_slots, "n_tiles": len(tiles),
                      "sha256": digest, "profile": profile}

    def index_episode(self, fp: str, profile: str, episode_key: str) -> bool:
        got = self.get_package(fp, profile, episode_key)
        return got is not None

    def put_package(self, fp: str, profile: str, duration_ms: int, data: bytes,
                    episode_key: str | None = None) -> None:
        values = {"profile": profile, "duration_ms": duration_ms}
        if episode_key:
            values["episode_key"] = episode_key
        query = urllib.parse.urlencode(values)
        status, _headers, _body = self._request(
            "PUT", f"/v1/pool/{fp}/nvmt?{query}", key=self.write_key, data=data,
            content_type="application/vnd.openseek.nvmt")
        if status not in (200, 201):
            raise PoolError(503, "remote_write_failed")

    def get_tile(self, fp: str, profile: str, slot: int):
        query = urllib.parse.urlencode({"profile": profile})
        status, headers, data = self._request("GET", f"/v1/pool/{fp}/tiles/{slot}.jpg?{query}",
                                              key=self.read_key, max_bytes=MAX_TILE_BYTES)
        if status == 404:
            return None
        if status != 200:
            raise PoolError(503 if status >= 500 else 502, "remote_read_failed")
        digest = hashlib.sha256(data).hexdigest()
        if headers.get("x-openseek-sha256") != digest:
            raise PoolError(502, "remote_response_invalid")
        try:
            pts = int(headers["x-openseek-pts-ms"])
            duration = int(headers["x-openseek-duration-ms"])
            n_tiles = int(headers["x-openseek-n-tiles"])
        except (KeyError, ValueError) as exc:
            raise PoolError(502, "remote_response_invalid") from exc
        try:
            validate_tile(fp, profile, slot, pts, duration, n_tiles, data)
        except PoolError as exc:
            raise PoolError(502, "remote_response_invalid") from exc
        return data, {"pts_ms": pts, "duration_ms": duration, "n_tiles": n_tiles,
                      "sha256": digest, "profile": profile}

    def put_tile(self, fp: str, profile: str, slot: int, pts_ms: int, duration_ms: int,
                 n_tiles: int, data: bytes) -> None:
        query = urllib.parse.urlencode({"profile": profile, "pts_ms": pts_ms,
                                        "duration_ms": duration_ms, "n_tiles": n_tiles})
        status, _headers, _body = self._request("PUT", f"/v1/pool/{fp}/tiles/{slot}.jpg?{query}",
                                                key=self.write_key, data=data, content_type="image/jpeg",
                                                max_bytes=MAX_TILE_BYTES)
        if status not in (200, 201):
            raise PoolError(503, "remote_write_failed")

    def manifest(self, fp: str, profile: str) -> dict | None:
        query = urllib.parse.urlencode({"profile": profile})
        status, _headers, body = self._request("GET", f"/v1/pool/{fp}/manifest?{query}",
                                               key=self.read_key, max_bytes=1024 * 1024)
        if status == 404:
            return None
        if status != 200:
            raise PoolError(503 if status >= 500 else 502, "remote_read_failed")
        try:
            result = json.loads(body)
        except ValueError as exc:
            raise PoolError(502, "remote_response_invalid") from exc
        if not isinstance(result, dict):
            raise PoolError(502, "remote_response_invalid")
        return result

    def episode_candidates(self, episode_key: str, duration_ms: int) -> list[dict]:
        query = urllib.parse.urlencode({"duration_ms": duration_ms})
        status, _headers, body = self._request("GET", f"/v1/pool/episodes/{episode_key}?{query}",
                                               key=self.read_key, max_bytes=128 * 1024)
        if status == 404:
            return []
        if status != 200:
            raise PoolError(503 if status >= 500 else 502, "remote_read_failed")
        try:
            result = json.loads(body)
            candidates = result["candidates"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PoolError(502, "remote_response_invalid") from exc
        if not isinstance(candidates, list):
            raise PoolError(502, "remote_response_invalid")
        return candidates


class RemotePoolSync:
    """Durable retry worker for packages and compatibility tiles shared upstream."""

    def __init__(self, index, pool, remote: RemotePoolClient):
        self.index, self.pool, self.remote = index, pool, remote
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="seekdb-pool-sync", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(3, self.remote.timeout_s + 1))
        self._thread = None

    def enqueue(self, kind: str, fp: str, profile: str, slot: int = -1) -> None:
        self.index.enqueue_remote(kind, fp, profile, slot)
        self._wake.set()

    def pending(self) -> int:
        return self.index.remote_outbox_pending()

    def _run(self) -> None:
        while not self._stop.is_set():
            row = self.index.remote_outbox_next()
            if row is None:
                self._wake.wait(1)
                self._wake.clear()
                continue
            try:
                self._publish(row)
                self.index.remote_outbox_finish(row)
                self.last_error = ""
            except Exception as exc:
                self.last_error = getattr(exc, "code", type(exc).__name__)
                if self.last_error in ("stored_package_corrupt", "stored_tile_corrupt"):
                    if row["kind"] == "package":
                        meta = self.index.get_package(row["fp"], row["profile"])
                        if meta:
                            self.index.delete_package(row["fp"], row["profile"], meta["object_key"])
                    else:
                        meta = self.index.get_tile_record(row["fp"], row["profile"], row["slot"])
                        if meta:
                            self.index.delete_tile_record(row["fp"], row["profile"], row["slot"], meta["object_key"])
                    self.index.remote_outbox_finish(row)
                    continue
                attempts = int(row["attempts"]) + 1
                delay = min(300, 2 ** min(attempts, 8))
                self.index.remote_outbox_retry(row, attempts, time.time() + delay)
                log.info("upstream pool write deferred fp=%s retry_s=%d (%s)",
                         row["fp"][:8], delay, type(exc).__name__)

    def _publish(self, row: dict) -> None:
        if row["kind"] == "package":
            meta = self.index.get_package(row["fp"], row["profile"])
            if meta is None:
                return
            data = self.pool._read_package(meta)
            episode_key = self.index.package_episode_key(row["fp"], row["profile"])
            self.remote.put_package(row["fp"], row["profile"], meta["duration_ms"], data, episode_key)
            return
        meta = self.index.get_tile_record(row["fp"], row["profile"], row["slot"])
        if meta is None:
            return
        data = self.pool._read_tile(meta)
        self.remote.put_tile(row["fp"], row["profile"], row["slot"], meta["pts_ms"],
                             meta["duration_ms"], meta["n_tiles"], data)
