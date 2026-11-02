"""Telethon channel store adapted from OpenSeek (AGPL-3.0-or-later)."""
from __future__ import annotations

import asyncio
import getpass
import hashlib
import io
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .cache import PROFILE, jpeg_ok
from .config import Settings
from .ids import is_fingerprint
from .protocol.nvmt import MAX_TILE_BYTES, PoolError
from .store.telegram_bot import _tile_filename, _tile_metadata_from_filename

log = logging.getLogger("seekdb.telegram_mt")
_CAPTION = re.compile(r"^openseek:v1:([0-9a-f]{64}):([^:]+):(\d+):([0-9a-f]{64})$")


@dataclass(frozen=True)
class ChannelMessage:
    message_id: int
    caption: str
    document_size: int
    file_name: str = ""


class TelegramMTProtoStore:
    """MTProto channel store whose blocking API never runs on the decode threads."""

    def __init__(self, settings: Settings, client_factory=None, request_timeout_s: float = 60.0):
        self.settings = settings
        self.channel_id = settings.telegram_channel_id
        self._client_factory = client_factory
        self._request_timeout_s = max(5.0, request_timeout_s)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._client = None
        self._entity = None
        self._connected = False
        self._message_handler = None
        self._handler_attached = False
        self._session_file: Path | None = None
        self._lock = threading.RLock()

    def _start_loop(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._ready.clear()
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop_main, name="openseek-telegram-mtproto", daemon=True)
            self._thread.start()
        if not self._ready.wait(10):
            raise PoolError(503, "telegram_loop_unavailable")

    def _loop_main(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def _submit(self, coro):
        self._start_loop()
        assert self._loop is not None
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self._request_timeout_s)
        except PoolError:
            raise
        except TimeoutError as exc:
            raise PoolError(503, "telegram_timeout") from exc
        except Exception as exc:
            raise _pool_error(exc) from exc

    def _session(self):
        if self._client_factory is not None:
            configured = self.settings.telegram_session_file or self.settings.telegram_session
            if configured:
                return str(configured)
            return str(self._default_session_path())

        from telethon import TelegramClient
        from telethon.sessions import StringSession

        value = self.settings.telegram_session
        if value and _looks_like_path(value):
            self._session_file = Path(value).expanduser()
            self._secure_session_path(self._session_file)
            return self._new_client(TelegramClient, str(self._session_file))
        if self.settings.telegram_session:
            return self._new_client(TelegramClient, StringSession(value))
        path = Path(self.settings.telegram_session_file).expanduser() if self.settings.telegram_session_file else self._default_session_path()
        self._session_file = path
        self._secure_session_path(path)
        return self._new_client(TelegramClient, str(path))

    def _new_client(self, factory, session):
        old_umask = os.umask(0o077) if os.name == "posix" else None
        try:
            return factory(session, self.settings.telegram_api_id, self.settings.telegram_api_hash,
                           flood_sleep_threshold=0)
        finally:
            if old_umask is not None:
                os.umask(old_umask)

    def _default_session_path(self) -> Path:
        return Path(self.settings.data_dir) / "telegram" / "seekdb.session"

    @staticmethod
    def _secure_session_path(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        if os.name == "posix":
            os.chmod(path.parent, 0o700)
            if path.exists():
                os.chmod(path, 0o600)

    async def _ensure_connected(self, allow_unauthorized: bool = False) -> None:
        if self._connected:
            return
        try:
            if self._client is None:
                session = self._session()
                if self._client_factory:
                    self._client = self._client_factory(session, self.settings.telegram_api_id,
                                                        self.settings.telegram_api_hash)
                else:
                    self._client = session
            if self._message_handler is not None and not self._handler_attached:
                from telethon import events

                self._client.add_event_handler(self._dispatch_message, events.NewMessage(incoming=True))
                self._handler_attached = True
            await self._client.connect()
            if self.settings.telegram_bot_token:
                await self._client.sign_in(bot_token=self.settings.telegram_bot_token)
            elif not await self._client.is_user_authorized() and not allow_unauthorized:
                raise PoolError(401, "telegram_login_required")
            self._connected = True
            if self._session_file:
                self._secure_session_path(self._session_file)
                if self._session_file.exists() and os.name == "posix":
                    os.chmod(self._session_file, 0o600)
        except PoolError:
            raise
        except Exception as exc:
            raise _pool_error(exc) from exc

    async def _verify_entity(self, check_post: bool) -> None:
        try:
            self._entity = await self._client.get_entity(self.channel_id)
            async for _message in self._client.iter_messages(self._entity, limit=1):
                break
            if check_post:
                permissions = await self._client.get_permissions(self._entity, "me")
                is_broadcast = bool(getattr(self._entity, "broadcast", False))
                allowed = getattr(permissions, "post_messages", False) if is_broadcast else getattr(permissions, "send_messages", True)
                if allowed is False:
                    raise PoolError(403, "telegram_post_forbidden")
        except PoolError:
            raise
        except Exception as exc:
            raise _pool_error(exc) from exc

    def start(self) -> None:
        self._submit(self._start_async())

    async def _start_async(self) -> None:
        await self._ensure_connected()
        await self._verify_entity(bool(self.settings.librarian_enabled or
                                       (self.settings.pool_upload and not self.settings.librarian)))

    def set_message_handler(self, handler) -> None:
        self._message_handler = handler

    async def _dispatch_message(self, event) -> None:
        try:
            result = self._message_handler(event, self)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            log.warning("Telegram librarian message failed (%s)", type(exc).__name__)

    def login(self, phone: str | None = None, input_fn: Callable[[str], str] = input,
              password_fn: Callable[[str], str] = getpass.getpass) -> None:
        """Interactively authorize a user session; bot-token sessions use start()."""
        if self.settings.telegram_bot_token:
            raise PoolError(400, "bot_login_uses_token")
        phone = phone or input_fn("Telegram phone number: ")
        if not phone.strip():
            raise PoolError(400, "telegram_phone_required")
        self._submit(self._login_async(phone, input_fn, password_fn))

    async def _login_async(self, phone: str, input_fn, password_fn) -> None:
        await self._ensure_connected(allow_unauthorized=True)
        try:
            sent = await self._client.send_code_request(phone.strip())
            code = input_fn("Telegram login code: ").strip()
            try:
                await self._client.sign_in(phone=phone.strip(), code=code, phone_code_hash=sent.phone_code_hash)
            except Exception as exc:
                try:
                    from telethon.errors import SessionPasswordNeededError
                except ImportError:
                    SessionPasswordNeededError = ()
                if SessionPasswordNeededError and isinstance(exc, SessionPasswordNeededError):
                    await self._client.sign_in(password=password_fn("Telegram 2-step password: "))
                else:
                    raise
            if self._session_file and self._session_file.exists() and os.name == "posix":
                os.chmod(self._session_file, 0o600)
        except PoolError:
            raise
        except Exception as exc:
            raise _pool_error(exc) from exc

    def join_invite(self, invite: str) -> str:
        if self.settings.telegram_bot_token:
            raise PoolError(400, "bot_cannot_join_invite")
        invite_hash = _invite_hash(invite)
        return self._submit(self._join_invite(invite_hash))

    async def _join_invite(self, invite_hash: str) -> str:
        await self._ensure_connected()
        try:
            from telethon.tl.functions.messages import ImportChatInviteRequest
            from telethon import utils

            try:
                updates = await self._client(ImportChatInviteRequest(invite_hash))
                entity = (getattr(updates, "chats", None) or [None])[0]
            except Exception as exc:
                if type(exc).__name__ != "UserAlreadyParticipantError":
                    raise
                entity = await self._client.get_entity(self.channel_id)
            if entity is None:
                entity = await self._client.get_entity(self.channel_id)
            return str(utils.get_peer_id(entity))
        except PoolError:
            raise
        except Exception as exc:
            raise _pool_error(exc) from exc

    def put_tile(self, data: bytes, caption: str, metadata: dict | None = None,
                 target: str | None = None) -> int:
        match = _CAPTION.fullmatch(caption or "")
        if (not match or not is_fingerprint(match.group(1)) or match.group(2) != PROFILE or
                len(data) > MAX_TILE_BYTES or not jpeg_ok(data) or
                hashlib.sha256(data).hexdigest() != match.group(4)):
            raise PoolError(400, "bad_tile")
        filename = "tile.jpg"
        if metadata:
            filename = _tile_filename(int(metadata["pts_ms"]), int(metadata["duration_ms"]),
                                      int(metadata["n_tiles"]))
            if _tile_metadata_from_filename(filename, int(match.group(3))) is None:
                raise PoolError(400, "bad_tile_metadata")
        return self._submit(self._put_tile(bytes(data), caption, filename, target))

    async def _put_tile(self, data: bytes, caption: str, filename: str, target: str | None) -> int:
        await self._ensure_connected()
        if target:
            entity = await self._client.get_entity(target)
        elif self._entity is None:
            await self._verify_entity(True)
            entity = self._entity
        else:
            entity = self._entity
        try:
            message = await self._retry(lambda: self._client.send_file(
                entity, (io.BytesIO(data), filename), caption=caption,
                force_document=True, mime_type="image/jpeg"))
            return int(message.id)
        except PoolError:
            raise
        except Exception as exc:
            raise _pool_error(exc) from exc

    def get_tile(self, message_id: int) -> bytes:
        return self._submit(self._get_tile(message_id))

    async def _get_tile(self, message_id: int) -> bytes:
        await self._ensure_connected()
        if self._entity is None:
            await self._verify_entity(False)
        try:
            message = await self._retry(lambda: self._client.get_messages(self._entity, ids=message_id))
            if message is None or not getattr(message, "document", None):
                raise PoolError(404, "telegram_message_not_found")
            size = int(getattr(message.document, "size", 0) or 0)
            if size <= 0 or size > MAX_TILE_BYTES:
                raise PoolError(413, "tile_too_large")
            caption = getattr(message, "message", None) or getattr(message, "text", "") or ""
            match = _CAPTION.fullmatch(caption)
            if not match:
                raise PoolError(502, "stored_tile_corrupt")
            data = await self._download_bounded(message.document)
            if not jpeg_ok(data) or hashlib.sha256(data).hexdigest() != match.group(4):
                raise PoolError(502, "stored_tile_corrupt")
            return data
        except PoolError:
            raise
        except Exception as exc:
            raise _pool_error(exc) from exc

    def scan(self, min_id: int = 0, limit: int = 2000) -> list[ChannelMessage]:
        if min_id < 0 or limit < 1:
            raise ValueError("invalid channel scan bounds")
        return self._submit(self._scan(min_id, limit))

    async def _scan(self, min_id: int, limit: int) -> list[ChannelMessage]:
        await self._ensure_connected()
        if self._entity is None:
            await self._verify_entity(False)
        try:
            for attempt in range(4):
                messages = []
                try:
                    async for message in self._client.iter_messages(
                            self._entity, min_id=min_id, limit=limit, reverse=True):
                        document = getattr(message, "document", None)
                        caption = getattr(message, "message", None) or getattr(message, "text", "") or ""
                        attributes = getattr(document, "attributes", ()) if document else ()
                        file_name = next((getattr(attr, "file_name", "") for attr in attributes
                                          if getattr(attr, "file_name", "")), "")
                        messages.append(ChannelMessage(int(message.id), caption,
                                                       int(getattr(document, "size", 0) or 0), file_name))
                    return messages
                except Exception as exc:
                    delay = _retry_delay(exc, attempt)
                    if delay is None or attempt == 3:
                        raise
                    await asyncio.sleep(delay)
        except PoolError:
            raise
        except Exception as exc:
            raise _pool_error(exc) from exc

    def delete_message(self, message_id: int, target: str | None = None) -> bool:
        try:
            return bool(self._submit(self._delete_message(message_id, target)))
        except Exception as exc:
            log.info("Telegram duplicate cleanup failed (%s)", type(exc).__name__)
            return False

    async def _delete_message(self, message_id: int, target: str | None) -> bool:
        await self._ensure_connected()
        if target:
            entity = await self._client.get_entity(target)
        elif self._entity is None:
            await self._verify_entity(True)
            entity = self._entity
        else:
            entity = self._entity
        await self._retry(lambda: self._client.delete_messages(entity, [message_id]))
        return True

    async def _retry(self, operation):
        for attempt in range(4):
            try:
                return await operation()
            except Exception as exc:
                delay = _retry_delay(exc, attempt)
                if delay is None or attempt == 3:
                    raise
                await asyncio.sleep(delay)

    async def _download_bounded(self, document) -> bytes:
        for attempt in range(4):
            buffer = bytearray()
            try:
                async for chunk in self._client.iter_download(document, request_size=64 * 1024):
                    buffer.extend(chunk)
                    if len(buffer) > MAX_TILE_BYTES:
                        raise PoolError(413, "tile_too_large")
                return bytes(buffer)
            except PoolError:
                raise
            except Exception as exc:
                delay = _retry_delay(exc, attempt)
                if delay is None or attempt == 3:
                    raise
                await asyncio.sleep(delay)
        raise PoolError(503, "telegram_unavailable")

    def close(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            if thread.is_alive() and self._client is not None:
                asyncio.run_coroutine_threadsafe(self._client.disconnect(), loop).result(10)
        except Exception:
            pass
        self._connected = False
        loop.call_soon_threadsafe(loop.stop)
        if thread.is_alive():
            thread.join(timeout=10)
        self._thread = None
        self._loop = None
        self._client = None
        self._entity = None
        self._handler_attached = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.close()


def _looks_like_path(value: str) -> bool:
    path = Path(value).expanduser()
    return path.exists() or value.endswith(".session") or "/" in value or "\\" in value


def _invite_hash(invite: str) -> str:
    value = invite.strip()
    if not value:
        raise PoolError(400, "bad_invite_link")
    parsed = urlsplit(value if "://" in value else "https://t.me/" + value.lstrip("/"))
    if parsed.hostname not in ("t.me", "telegram.me", "www.t.me", "www.telegram.me"):
        raise PoolError(400, "bad_invite_link")
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].startswith("+") and len(parts[0]) > 1:
        return parts[0][1:]
    if len(parts) >= 2 and parts[0] == "joinchat":
        return parts[1]
    raise PoolError(400, "bad_invite_link")


def _retry_delay(exc: Exception, attempt: int) -> float | None:
    name = type(exc).__name__
    if name == "FloodWaitError":
        return min(max(float(getattr(exc, "seconds", 1)), 1.0), 60.0)
    if isinstance(exc, (OSError, TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return min(2 ** attempt, 8)
    return None


def _pool_error(exc: Exception) -> PoolError:
    if isinstance(exc, PoolError):
        return exc
    name = type(exc).__name__
    if name in ("MessageIdInvalidError", "MessageEmptyError"):
        return PoolError(404, "telegram_message_not_found")
    if name in ("ChannelPrivateError", "ChatAdminRequiredError", "UserNotParticipantError"):
        return PoolError(403, "telegram_access_denied")
    if name in ("FloodWaitError", "TimeoutError", "ConnectionError") or isinstance(exc, (OSError, asyncio.TimeoutError)):
        return PoolError(503, "telegram_unavailable")
    if name in ("AuthKeyError", "SessionPasswordNeededError", "PhoneCodeInvalidError"):
        return PoolError(401, "telegram_login_required")
    if name in ("ImportError", "ModuleNotFoundError"):
        return PoolError(503, "telethon_unavailable")
    log.warning("Telegram operation failed (%s)", name)
    return PoolError(502, "telegram_error")
