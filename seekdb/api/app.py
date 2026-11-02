from __future__ import annotations

import hashlib
import hmac
import ipaddress
import asyncio
from dataclasses import replace
from collections import deque
from pathlib import Path
import threading
import time
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from seekdb import API_VERSION, __version__
from seekdb.auth import DeviceAuth, admin_token
from seekdb.channel import ChannelReadService
from seekdb.channel_index import ChannelIndex
from seekdb.config import ConfigError, Settings
from seekdb.contribution import TelegramLibrarian
from seekdb.index.sqlite import SQLiteIndex
from seekdb.pairing import InviteManager, PairingClosed, PairingFailed, PairingManager, RateLimited
from seekdb.pool import PoolService
from seekdb.protocol.nvmt import MAX_NVMT_BYTES, MAX_TILE_BYTES, PoolError, TILE_PROFILE
from seekdb.store.base import ObjectStore
from seekdb.store.fs import FileSystemObjectStore
from seekdb.store.telegram_bot import TelegramBotStore, TelegramObjectStore
from seekdb.telegram_mt import TelegramMTProtoStore
from seekdb.tls import ensure_certificate
from seekdb.remote import RemotePoolClient, RemotePoolSync

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class StartIn(BaseModel):
    invite: str | None = Field(default=None, max_length=80)


class ClaimIn(BaseModel):
    pair_id: str = Field(min_length=32, max_length=32)
    device_name: str = Field(min_length=1, max_length=64)
    client_proof: str = Field(min_length=64, max_length=64)


class InviteIn(BaseModel):
    label: str = Field(default="", max_length=64)
    role: Literal["reader", "contributor", "guest"] = "contributor"
    ttl_s: int = Field(default=86400, ge=600, le=30 * 86400)
    max_uses: int = Field(default=1, ge=1, le=100_000)
    device_days: float | None = Field(default=None, gt=0, le=3650)


class GCIn(BaseModel):
    dry_run: bool = True


class SlidingWindowLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.events: dict[str, deque[float]] = {}

    def allow(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        with self.lock:
            events = self.events.setdefault(key, deque())
            while events and now - events[0] >= 60:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            if len(self.events) > 4096:
                self.events = {name: queue for name, queue in self.events.items()
                               if queue and now - queue[-1] < 60}
            return True


def _error(status: int, code: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers={**SECURITY_HEADERS, **(headers or {})})


class BodyLimitMiddleware:
    """Reject oversized pool writes before FastAPI buffers the request body."""

    def __init__(self, app, package_limit: int):
        self.app = app
        self.package_limit = package_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        is_pool = path.startswith("/v1/pool/") and scope["method"] == "PUT"
        limit = self.package_limit if is_pool and path.endswith("/nvmt") else MAX_TILE_BYTES if is_pool else 64 * 1024
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if not raw_length.isdigit():
                    raise ValueError
                if int(raw_length) > limit:
                    response = _error(413, "too_large")
                    return await response(scope, receive, send)
            except ValueError:
                response = _error(400, "bad_request")
                return await response(scope, receive, send)
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            size += len(body)
            if size > limit:
                response = _error(413, "too_large")
                return await response(scope, receive, send)
            chunks.append(body)
            if not message.get("more_body", False):
                break
        content = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": content, "more_body": False}

        await self.app(scope, replay, send)


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer "):
        return None
    token = value[7:].strip()
    return token or None


def create_app(settings: Settings | None = None, *, index: SQLiteIndex | None = None,
               store: ObjectStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    data_dir = Path(settings.data_dir)
    index = index or SQLiteIndex(data_dir / "seekdb.db")
    cert_path, key_path, pin = ensure_certificate(data_dir)
    auth = DeviceAuth(index)
    invites = InviteManager(index, pin, auth.register)
    pairing = PairingManager(pin, auth.register, invites=invites)
    pairing.sharing_enabled = lambda: settings.effective_pool_mode != "local"
    channel_store = None
    librarian_store = None
    channel_index = None
    librarian = None
    if settings.effective_pool_mode == "channel":
        user_settings = replace(settings, telegram_bot_token="", librarian_enabled=False)
        channel_store = store or TelegramMTProtoStore(user_settings)
        channel_index = ChannelIndex(index, channel_store, channel_id=settings.telegram_channel_id,
                                     scan_limit=settings.pool_channel_scan_limit,
                                     refresh_s=settings.pool_index_refresh_s)
        if settings.librarian_enabled:
            librarian_settings = replace(settings, telegram_session="",
                                           telegram_session_file=data_dir / "telegram" / "librarian-bot.session")
            librarian_store = TelegramMTProtoStore(librarian_settings)
            librarian = TelegramLibrarian(librarian_store, channel_index)
            librarian.attach()
        pool = ChannelReadService(channel_index, settings.pool_read_key)
    else:
        if store is None:
            if settings.effective_pool_mode == "host" and settings.store == "telegram":
                bot = TelegramBotStore(settings.telegram_bot_token, settings.telegram_channel_id)
                store = TelegramObjectStore(bot, index)
            else:
                store = FileSystemObjectStore(data_dir)
        remote = (RemotePoolClient(settings.pool_url, settings.pool_read_key, settings.pool_write_key,
                                   settings.pool_timeout_s) if settings.pool_url else None)
        pool = PoolService(index, store, settings.pool_read_key, settings.pool_write_key,
                           settings.max_storage_bytes, remote, settings.pool_share)
        if remote and settings.pool_share:
            pool.remote_sync = RemotePoolSync(index, pool, remote)
    admin_secret = admin_token(data_dir, settings.admin_token)
    upload_limiter = SlidingWindowLimiter()
    ip_salt = hashlib.sha256(admin_secret.encode("utf-8")).digest()

    app = FastAPI(title="SeekDB", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimitMiddleware, package_limit=settings.max_package_bytes)
    app.state.settings = settings
    app.state.index = index
    app.state.store = store or channel_store
    app.state.channel_store = channel_store
    app.state.channel_index = channel_index
    app.state.librarian = librarian
    app.state.librarian_store = librarian_store
    app.state.pool = pool
    app.state.auth = auth
    app.state.invites = invites
    app.state.pairing = pairing
    app.state.spki_pin = pin
    app.state.cert_path = cert_path
    app.state.key_path = key_path
    app.state.admin_secret = admin_secret

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return _error(400, "bad_request")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return _error(404, "not_found")
        return _error(exc.status_code, "bad_request")

    @app.on_event("shutdown")
    async def close_index():
        if isinstance(pool, PoolService) and pool.remote_sync:
            pool.remote_sync.stop()
        if channel_index:
            channel_index.stop()
        if channel_store:
            await asyncio.to_thread(channel_store.close)
        if librarian_store:
            await asyncio.to_thread(librarian_store.close)
        index.close()

    @app.on_event("startup")
    async def start_channel():
        if isinstance(pool, PoolService) and pool.remote_sync:
            pool.remote_sync.start()
        if not channel_store or not channel_index:
            return
        await asyncio.to_thread(channel_store.start)
        if librarian_store:
            await asyncio.to_thread(librarian_store.start)
        state = channel_index.status()
        if not state["scanned_at"]:
            await asyncio.to_thread(channel_index.rebuild)
        else:
            await asyncio.to_thread(channel_index.refresh, True)
        channel_index.start()

    def device_id(request: Request) -> str | None:
        return auth.authenticate(request.headers.get("authorization"))

    def can_read(request: Request) -> bool:
        if settings.effective_pool_mode == "local":
            return False
        if settings.public_read or device_id(request):
            return True
        return pool.authorize(_bearer(request), write=False)

    def can_write(request: Request) -> bool:
        if settings.effective_pool_mode != "host":
            return False
        ident = device_id(request)
        if ident:
            rows = index.q("SELECT role,contribute FROM devices WHERE device_id=? AND revoked IS NULL", (ident,))
            if rows and (rows[0][0] in ("owner", "admin", "contributor") or
                         (rows[0][0] == "guest" and bool(rows[0][1]))):
                return True
        return pool.authorize(_bearer(request), write=True)

    def upload_rate_allowed(request: Request, ident: str | None) -> bool:
        peer = request.client.host if request.client else "unknown"
        ip_tag = hmac.new(ip_salt, peer.encode("utf-8", "replace"), hashlib.sha256).hexdigest()
        keys = ["ip:" + ip_tag]
        if ident:
            keys.append("device:" + ident)
        return all(upload_limiter.allow(key, settings.upload_requests_per_min) for key in keys)

    def quota_exceeded(ident: str | None, data_size: int, package: bool) -> bool:
        if not ident:
            return False
        usage = index.upload_usage(ident)
        if settings.device_daily_bytes and usage["bytes"] + data_size > settings.device_daily_bytes:
            return True
        return bool(package and settings.device_daily_packages and
                    usage["packages"] >= settings.device_daily_packages)

    def is_admin(request: Request) -> bool:
        peer = request.client.host if request.client else ""
        local = False
        try:
            local = ipaddress.ip_address(peer.split("%", 1)[0]).is_loopback
        except ValueError:
            pass
        supplied = _bearer(request)
        authorized = bool(supplied and hmac.compare_digest(supplied, admin_secret))
        return local or authorized

    @app.get("/v1/health")
    async def health():
        return JSONResponse({"service": "seekdb", "api": API_VERSION}, headers=SECURITY_HEADERS)

    @app.post("/v1/pair/start")
    async def pair_start(body: StartIn | None = None):
        try:
            result = pairing.start(body.invite if body else None)
        except RateLimited:
            return _error(429, "rate_limited", {"Retry-After": "60"})
        except PairingClosed:
            return _error(409, "pairing_closed")
        return JSONResponse(result, headers=SECURITY_HEADERS)

    @app.post("/v1/pair/claim")
    async def pair_claim(body: ClaimIn):
        try:
            result = pairing.claim(body.pair_id, body.device_name, body.client_proof)
        except RateLimited:
            return _error(429, "rate_limited", {"Retry-After": "60"})
        except PairingFailed:
            return _error(403, "pairing_failed")
        return JSONResponse(result, headers=SECURITY_HEADERS)

    @app.get("/v1/pair/status")
    async def pair_status(request: Request):
        ident = device_id(request)
        if not ident:
            return _error(401, "unauthorized")
        rows = index.q("SELECT name,role FROM devices WHERE device_id=? AND revoked IS NULL", (ident,))
        if not rows:
            return _error(401, "unauthorized")
        index.touch_device(ident)
        return JSONResponse({"device_id": ident, "device_name": rows[0][0], "role": rows[0][1]}, headers=SECURITY_HEADERS)

    @app.delete("/v1/pair", status_code=204)
    async def pair_revoke(request: Request):
        ident = device_id(request)
        if not ident:
            return _error(401, "unauthorized")
        auth.revoke(ident)
        return Response(status_code=204)

    @app.get("/v1/pool/episodes/{episode_key}")
    async def episode_candidates(request: Request, episode_key: str, duration_ms: int):
        if not can_read(request):
            return _error(401, "unauthorized")
        try:
            candidates = pool.episode_candidates(episode_key, duration_ms)
        except PoolError as exc:
            return _error(exc.status, exc.code)
        return JSONResponse({"candidates": candidates}, headers=SECURITY_HEADERS)

    @app.get("/v1/pool/{fp}/manifest")
    async def manifest(request: Request, fp: str, profile: str = ""):
        if not can_read(request):
            return _error(401, "unauthorized")
        try:
            result = pool.manifest(fp, profile)
        except PoolError as exc:
            return _error(exc.status, exc.code)
        if result is None:
            return _error(404, "not_found")
        return JSONResponse(result, headers=SECURITY_HEADERS)

    @app.get("/v1/pool/{fp}/tiles/{name}")
    async def get_tile(request: Request, fp: str, name: str, profile: str = ""):
        if not can_read(request):
            return _error(401, "unauthorized")
        if not name.endswith(".jpg") or not name[:-4].isdigit():
            return _error(404, "not_found")
        try:
            result = pool.tile(fp, profile, int(name[:-4]))
        except PoolError as exc:
            return _error(exc.status, exc.code)
        if result is None:
            return _error(404, "not_found")
        data, meta = result
        headers = {**SECURITY_HEADERS, "X-OpenSeek-Pts-Ms": str(meta["pts_ms"]),
                   "X-OpenSeek-Duration-Ms": str(meta["duration_ms"]),
                   "X-OpenSeek-N-Tiles": str(meta["n_tiles"]), "X-OpenSeek-Profile": meta["profile"],
                   "X-OpenSeek-Sha256": meta["sha256"]}
        return Response(data, media_type="image/jpeg", headers=headers)

    @app.put("/v1/pool/{fp}/tiles/{name}")
    async def put_tile(request: Request, fp: str, name: str):
        if settings.effective_pool_mode != "host":
            return _error(405, "method_not_allowed")
        if not can_write(request):
            return _error(403, "forbidden")
        ident = device_id(request)
        if not upload_rate_allowed(request, ident):
            return _error(429, "rate_limited", {"Retry-After": "60"})
        if not name.endswith(".jpg") or not name[:-4].isdigit() or request.headers.get("content-type", "").split(";", 1)[0].lower() != "image/jpeg":
            return _error(400, "bad_request")
        try:
            data = await request.body()
            if quota_exceeded(ident, len(data), False):
                return _error(429, "quota_exceeded", {"Retry-After": "86400"})
            result = pool.upload_tile(fp, request.query_params.get("profile", ""), int(name[:-4]),
                                      int(request.query_params.get("pts_ms", "")),
                                      int(request.query_params.get("duration_ms", "")),
                                      int(request.query_params.get("n_tiles", "")), data)
        except ValueError:
            return _error(400, "bad_request")
        except PoolError as exc:
            headers = {"X-OpenSeek-Duration-Ms": str(exc.duration_ms)} if exc.duration_ms is not None else None
            return _error(exc.status, exc.code, headers)
        if ident:
            index.record_upload(ident, len(data), tiles=1)
        index.record_audit(ident or "write-key", "tile_upload", fp,
                           "duplicate" if result["duplicate"] else "stored")
        return JSONResponse(result, status_code=200 if result["duplicate"] else 201, headers=SECURITY_HEADERS)

    @app.get("/v1/pool/{fp}/nvmt")
    async def get_package(request: Request, fp: str, profile: str = "", slots: str | None = None,
                         episode_key: str | None = None):
        if not can_read(request):
            return _error(401, "unauthorized")
        try:
            if slots is None:
                if settings.effective_pool_mode == "host":
                    result = pool.package(fp, profile, episode_key)
                else:
                    result = pool.package(fp, profile)
            else:
                values = slots.split(",")
                if not 1 <= len(values) <= 12 or any(not value.isdigit() for value in values):
                    return _error(400, "bad_slots")
                if settings.effective_pool_mode == "host":
                    result = pool.package_slots(fp, profile, [int(value) for value in values], episode_key)
                else:
                    result = pool.package_slots(fp, profile, [int(value) for value in values])
            if episode_key and settings.effective_pool_mode != "host":
                pool.index_episode_package(fp, profile, episode_key)
        except PoolError as exc:
            return _error(exc.status, exc.code)
        if result is None:
            return _error(404, "not_found")
        data, meta = result
        headers = {**SECURITY_HEADERS, "X-OpenSeek-Duration-Ms": str(meta["duration_ms"]),
                   "X-OpenSeek-N-Tiles": str(meta["n_tiles"]), "X-OpenSeek-Profile": meta["profile"],
                   "X-OpenSeek-Sha256": meta["sha256"]}
        return Response(data, media_type="application/vnd.openseek.nvmt", headers=headers)

    @app.put("/v1/pool/{fp}/nvmt")
    async def put_package(request: Request, fp: str):
        if settings.effective_pool_mode != "host":
            return _error(405, "method_not_allowed")
        if not can_write(request):
            return _error(403, "forbidden")
        ident = device_id(request)
        if not upload_rate_allowed(request, ident):
            return _error(429, "rate_limited", {"Retry-After": "60"})
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in ("application/vnd.openseek.nvmt", "application/octet-stream"):
            return _error(400, "bad_request")
        try:
            profile = request.query_params.get("profile", "")
            duration_ms = int(request.query_params.get("duration_ms", ""))
            data = await request.body()
            if quota_exceeded(ident, len(data), True):
                return _error(429, "quota_exceeded", {"Retry-After": "86400"})
            result = pool.upload_package(fp, profile, duration_ms, data,
                                         request.query_params.get("episode_key"))
        except ValueError:
            return _error(400, "bad_request")
        except PoolError as exc:
            headers = {"X-OpenSeek-Duration-Ms": str(exc.duration_ms)} if exc.duration_ms is not None else None
            return _error(exc.status, exc.code, headers)
        if ident:
            index.record_upload(ident, len(data), packages=1)
        index.record_audit(ident or "write-key", "package_upload", fp,
                           "duplicate" if result["duplicate"] else "stored")
        return JSONResponse(result, status_code=200 if result["duplicate"] else 201, headers=SECURITY_HEADERS)

    def require_admin(request: Request) -> bool:
        return is_admin(request)

    @app.get("/admin/status")
    async def admin_status(request: Request):
        if not require_admin(request):
            return _error(403, "forbidden")
        status = {**index.pool_status(), "mode": settings.effective_pool_mode,
                  "store": settings.store, "spki_pin": pin}
        if channel_index:
            status["channel"] = channel_index.status()
        if isinstance(pool, PoolService) and pool.remote_sync:
            status["upstream_sync"] = {"pending": pool.remote_sync.pending(),
                                       "last_error": pool.remote_sync.last_error}
        return JSONResponse(status, headers=SECURITY_HEADERS)

    @app.post("/admin/invites")
    async def create_invite(request: Request, body: InviteIn):
        if not require_admin(request):
            return _error(403, "forbidden")
        try:
            invite = invites.create(label=body.label, role=body.role, ttl_s=body.ttl_s,
                                    max_uses=body.max_uses, device_days=body.device_days,
                                    contribute=body.role in ("guest", "contributor"))
        except ValueError:
            return _error(400, "bad_request")
        # The invite secret is shown by the local CLI only; admin API responses
        # never carry it or the complete pairing code.
        public_invite = {key: invite[key] for key in ("invite_id", "tag", "expires", "expires_in_s", "role", "spki_pin", "contribute")}
        return JSONResponse(public_invite, status_code=201, headers=SECURITY_HEADERS)

    @app.get("/admin/invites")
    async def list_invites(request: Request):
        if not require_admin(request):
            return _error(403, "forbidden")
        return JSONResponse({"invites": invites.listing()}, headers=SECURITY_HEADERS)

    @app.delete("/admin/invites/{invite_id}")
    async def revoke_invite(request: Request, invite_id: str):
        if not require_admin(request):
            return _error(403, "forbidden")
        return JSONResponse({"revoked": invites.revoke(invite_id)}, headers=SECURITY_HEADERS)

    @app.get("/admin/devices")
    async def list_devices(request: Request):
        if not require_admin(request):
            return _error(403, "forbidden")
        return JSONResponse({"devices": index.devices()}, headers=SECURITY_HEADERS)

    @app.get("/admin/audit")
    async def admin_audit(request: Request, limit: int = 100):
        if not require_admin(request):
            return _error(403, "forbidden")
        return JSONResponse({"events": index.audit_rows(limit)}, headers=SECURITY_HEADERS)

    @app.delete("/admin/devices/{device_id}")
    async def revoke_device(request: Request, device_id: str):
        if not require_admin(request):
            return _error(403, "forbidden")
        return JSONResponse({"revoked": auth.revoke(device_id)}, headers=SECURITY_HEADERS)

    @app.post("/admin/gc")
    async def gc(request: Request, body: GCIn):
        if not require_admin(request):
            return _error(403, "forbidden")
        if settings.effective_pool_mode == "channel":
            return _error(405, "method_not_allowed")
        return JSONResponse(pool.gc(body.dry_run), headers=SECURITY_HEADERS)

    @app.post("/admin/rebuild")
    async def rebuild_index(request: Request):
        if not require_admin(request):
            return _error(403, "forbidden")
        if not channel_index:
            return _error(405, "method_not_allowed")
        try:
            result = await asyncio.to_thread(channel_index.rebuild)
        except PoolError as exc:
            return _error(exc.status, exc.code)
        return JSONResponse(result, headers=SECURITY_HEADERS)

    return app
