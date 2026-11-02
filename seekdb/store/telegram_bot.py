"""Telegram Bot API object storage adapted from OpenSeek (AGPL-3.0-or-later)."""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import re
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from seekdb.protocol.nvmt import MAX_DURATION_MS, MAX_NVMT_BYTES, MAX_TILE_BYTES, TILE_INTERVAL_MS, PoolError, tile_count

log = logging.getLogger("seekdb.telegram_bot")
_TILE_FILENAME = re.compile(r"^openseek-tile-v1-(\d+)-(\d+)-(\d+)\.jpg$")


def _tile_filename(pts_ms: int, duration_ms: int, n_tiles: int) -> str:
    """Carry metadata beside the stable caption so channel-only readers can rebuild it."""
    return f"openseek-tile-v1-{pts_ms}-{duration_ms}-{n_tiles}.jpg"


def _tile_metadata_from_filename(filename: str, k: int) -> tuple[int, int, int] | None:
    match = _TILE_FILENAME.fullmatch(filename or "")
    if not match:
        return None
    pts_ms, duration_ms, n_tiles = map(int, match.groups())
    if (not 0 < duration_ms <= MAX_DURATION_MS or n_tiles != tile_count(duration_ms) or
            not 0 <= k < n_tiles or not 0 <= pts_ms <= duration_ms or
            abs(pts_ms - k * TILE_INTERVAL_MS) > 5000):
        return None
    return pts_ms, duration_ms, n_tiles
class _DualStackHTTPSConnection(http.client.HTTPSConnection):
    """Prefer IPv4 but fall back across resolved addresses before timing out a Bot API request.

    Some hosts publish both A and AAAA records while the client network has no working IPv6 route.
    The standard urllib path can spend the full request timeout on that first address. Connect to
    available addresses in family-preference order, with a short per-address connect timeout.
    HTTPS proxies keep the stdlib tunnel behavior.
    """
    def connect(self) -> None:
        if self._tunnel_host:
            return super().connect()
        host = self.host.strip("[]")
        try:
            addresses = socket.getaddrinfo(host, self.port, type=socket.SOCK_STREAM)
        except OSError:
            return super().connect()
        addresses.sort(key=lambda item: 0 if item[0] == socket.AF_INET else 1)
        last_error: OSError | None = None
        for family, socktype, proto, _canonname, sockaddr in addresses:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                if self.source_address:
                    sock.bind(self.source_address)
                if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(min(float(self.timeout), 2.5))
                sock.connect(sockaddr)
                sock.settimeout(self.timeout)
                self.sock = self._context.wrap_socket(sock, server_hostname=host)
                return
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
        if last_error is not None:
            raise last_error
        return super().connect()


class _DualStackHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_DualStackHTTPSConnection, req, context=self._context)


def _urlopen(req, timeout: float = 8.0):
    """urllib opener with bounded IPv4/IPv6 fallback for Telegram and remote pool hosts."""
    return urllib.request.build_opener(_DualStackHTTPSHandler()).open(req, timeout=timeout)
class TelegramBotStore:
    """Store exact JPEG bytes as Telegram documents so Telegram won't recompress them."""

    def __init__(self, token: str, channel_id: str, timeout_s: float = 8.0,
                 api_base: str = "https://api.telegram.org", opener=None):
        if not token or not channel_id:
            raise ValueError("Telegram bot token and channel ID are required")
        self._token = token
        self.channel_id = channel_id
        self.timeout_s = timeout_s
        self.api_base = api_base.rstrip("/")
        self.opener = opener or _urlopen

    def _api_url(self, method: str) -> str:
        return f"{self.api_base}/bot{self._token}/{method}"

    def _json(self, method: str, params: dict) -> dict:
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(self._api_url(method), data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.opener(req, timeout=self.timeout_s) as response:
                result = json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            if method != "getFile":
                raise PoolError(503, "telegram_unavailable") from exc
            try:
                result = json.loads(exc.read(1024 * 1024))
            except (OSError, ValueError) as parse_exc:
                raise PoolError(503, "telegram_unavailable") from parse_exc
            finally:
                exc.close()
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise PoolError(503, "telegram_unavailable") from exc
        if (isinstance(result, dict) and result.get("ok") is not True and method == "getFile"
                and result.get("error_code") == 400):
            raise PoolError(404, "telegram_file_not_found")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise PoolError(502, "telegram_error")
        return result["result"]

    def file_exists(self, file_id: str) -> bool:
        """Check Telegram metadata without downloading the document bytes."""
        try:
            self._json("getFile", {"file_id": file_id})
            return True
        except PoolError as exc:
            if exc.code == "telegram_file_not_found":
                return False
            raise

    def put(self, data: bytes, caption: str, metadata: dict | None = None) -> tuple[str, int]:
        filename = "tile.jpg"
        if metadata:
            filename = _tile_filename(int(metadata["pts_ms"]), int(metadata["duration_ms"]),
                                      int(metadata["n_tiles"]))
        return self.put_document(data, caption, filename, "image/jpeg")

    def put_document(self, data: bytes, caption: str, filename: str,
                     content_type: str = "application/octet-stream") -> tuple[str, int]:
        boundary = "openseek-" + secrets.token_hex(16)
        pieces: list[bytes] = []

        def field(name: str, value: str) -> None:
            pieces.extend([f"--{boundary}\r\n".encode(),
                           f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                           value.encode(), b"\r\n"])

        field("chat_id", self.channel_id)
        field("caption", caption)
        pieces.extend([f"--{boundary}\r\n".encode(),
                       f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode(),
                       f"Content-Type: {content_type}\r\n\r\n".encode(), data, b"\r\n",
                       f"--{boundary}--\r\n".encode()])
        req = urllib.request.Request(self._api_url("sendDocument"), data=b"".join(pieces), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with self.opener(req, timeout=self.timeout_s) as response:
                result = json.loads(response.read(1024 * 1024))
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise PoolError(503, "telegram_unavailable") from exc
        message = result.get("result") if isinstance(result, dict) and result.get("ok") is True else None
        doc = message.get("document") if isinstance(message, dict) else None
        file_id = doc.get("file_id") if isinstance(doc, dict) else None
        message_id = message.get("message_id") if isinstance(message, dict) else None
        if not isinstance(file_id, str) or not file_id or not isinstance(message_id, int):
            raise PoolError(502, "telegram_error")
        return file_id, message_id

    def get(self, file_id: str, max_bytes: int = MAX_TILE_BYTES) -> bytes:
        result = self._json("getFile", {"file_id": file_id})
        path = result.get("file_path") if isinstance(result, dict) else None
        if not isinstance(path, str) or not path or ".." in Path(path).parts:
            raise PoolError(502, "telegram_error")
        url = f"{self.api_base}/file/bot{self._token}/{urllib.parse.quote(path, safe='/')}"
        req = urllib.request.Request(url, method="GET")
        try:
            with self.opener(req, timeout=self.timeout_s) as response:
                data = response.read(max_bytes + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise PoolError(503, "telegram_unavailable") from exc
        if len(data) > max_bytes:
            raise PoolError(502, "telegram_file_too_large")
        return data

    def get_document(self, file_id: str) -> bytes:
        return self.get(file_id, MAX_NVMT_BYTES)

    def delete_message(self, message_id: int) -> None:
        self._json("deleteMessage", {"chat_id": self.channel_id, "message_id": message_id})


class TelegramObjectStore:
    """ObjectStore adapter that persists Bot API file IDs beside the local index."""

    def __init__(self, bot: TelegramBotStore, index):
        self.bot = bot
        self.index = index

    def _key(self, data: bytes, suffix: str) -> str:
        if suffix not in ("nvmt", "jpg"):
            raise ValueError("unsupported Telegram object type")
        return hashlib.sha256(data).hexdigest() + "." + suffix

    def put(self, data: bytes, suffix: str = "nvmt") -> str:
        key = self._key(data, suffix)
        if self.exists(key):
            return key
        file_id, message_id = self.bot.put_document(data, f"seekdb:object:v1:{key}",
                                                    key, "image/jpeg" if suffix == "jpg" else "application/octet-stream")
        self.index.put_telegram_object(key, file_id, message_id, len(data), key.split(".", 1)[0])
        return key

    def get(self, object_key: str) -> bytes:
        row = self.index.get_telegram_object(object_key)
        if row is None:
            raise FileNotFoundError("Telegram object is not indexed")
        file_id, _message_id, size, digest = row
        try:
            data = self.bot.get_document(file_id) if object_key.endswith(".nvmt") else self.bot.get(file_id, MAX_TILE_BYTES)
        except PoolError as exc:
            if exc.code == "telegram_file_not_found":
                self.index.delete_telegram_object(object_key)
            raise
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            self.index.delete_telegram_object(object_key)
            raise PoolError(502, "stored_object_corrupt")
        return data

    def delete(self, object_key: str) -> None:
        row = self.index.get_telegram_object(object_key)
        if row is None:
            return
        self.bot.delete_message(int(row[1]))
        self.index.delete_telegram_object(object_key)

    def keys(self) -> set[str]:
        return self.index.telegram_object_keys()

    def exists(self, object_key: str) -> bool:
        return self.index.get_telegram_object(object_key) is not None

    def usage(self) -> int:
        return self.index.telegram_object_usage()

    def size(self, object_key: str) -> int:
        row = self.index.get_telegram_object(object_key)
        if row is None:
            raise FileNotFoundError(object_key)
        return int(row[2])
