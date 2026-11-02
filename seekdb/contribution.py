"""Telegram librarian relay adapted from OpenSeek (AGPL-3.0-or-later)."""
from __future__ import annotations

import io
import logging
import time
from collections import deque

from .cache import PROFILE
from .channel_index import ChannelIndex
from .protocol.nvmt import MAX_DURATION_MS, MAX_TILE_BYTES, TILE_INTERVAL_MS, PoolError, tile_count, validate_tile
from .store.telegram_bot import _tile_filename, _tile_metadata_from_filename
from .telegram_mt import ChannelMessage, TelegramMTProtoStore, _CAPTION

log = logging.getLogger("seekdb.contribution")


class TelegramLibrarian:
    """Validate user-submitted tile documents and repost accepted winners."""

    def __init__(self, store: TelegramMTProtoStore, index: ChannelIndex, *,
                 clock=time.monotonic, per_sender_s: float = 1.0, global_per_minute: int = 60):
        self.store, self.index = store, index
        self.clock = clock
        self.per_sender_s = per_sender_s
        self.global_per_minute = global_per_minute
        self._senders: dict[int, float] = {}
        self._recent: deque[float] = deque()
        self._blocked: dict[int, float] = {}

    def attach(self) -> None:
        self.store.set_message_handler(self.handle_message)

    async def handle_message(self, event, store: TelegramMTProtoStore) -> None:
        if not bool(getattr(event, "is_private", False)):
            return
        message = getattr(event, "message", None)
        document = getattr(message, "document", None)
        caption = getattr(message, "message", None) or getattr(message, "text", "") or ""
        match = _CAPTION.fullmatch(caption)
        # Ignore ordinary messages and unknown captions. They are not uploads.
        if not document or not match:
            return
        sender_id = int(getattr(event, "sender_id", 0) or 0)
        if not sender_id or not await self._allow_sender(event, sender_id):
            await self._delete(event)
            return
        fp, profile, raw_k, digest = match.groups()
        try:
            k = int(raw_k)
            size = int(getattr(document, "size", 0) or 0)
            if size < 125 or size > MAX_TILE_BYTES:
                raise PoolError(413, "tile_too_large")
            file_name = self._file_name(document)
            metadata = _tile_metadata_from_filename(file_name, k)
            if metadata is None:
                pts_ms, duration_ms = k * TILE_INTERVAL_MS, MAX_DURATION_MS
                n_tiles = tile_count(duration_ms)
            else:
                pts_ms, duration_ms, n_tiles = metadata
            data = await store._download_bounded(document)
            if len(data) != size:
                raise PoolError(400, "bad_tile_size")
            got_digest = validate_tile(fp, profile, k, pts_ms, duration_ms, n_tiles, data)
            if got_digest != digest:
                raise PoolError(400, "bad_tile_digest")
        except Exception as exc:
            self._block(sender_id)
            await self._delete(event)
            if isinstance(exc, PoolError):
                log.info("librarian rejected tile (%s)", exc.code)
            else:
                log.info("librarian rejected tile (%s)", type(exc).__name__)
            return

        current = self.index.lookup(fp, PROFILE, k)
        if current is not None:
            try:
                existing = await store._client.get_messages(store._entity, ids=current["message_id"])
            except Exception:
                existing = None
            if existing is not None and await self._valid_existing(store, existing, fp, profile, k):
                await self._delete(event)
                return
            self.index.forget(fp, PROFILE, k, current["message_id"])
        if await self._channel_has_key(store, fp, profile, k):
            await self._delete(event)
            return

        outgoing_name = (_tile_filename(pts_ms, duration_ms, n_tiles)
                         if metadata is not None else "tile.jpg")
        try:
            repost = await store._retry(lambda: store._client.send_file(
                store._entity, (io.BytesIO(data), outgoing_name), caption=caption,
                force_document=True, mime_type="image/jpeg"))
            repost_id = int(repost.id)
            record = ChannelMessage(repost_id, caption, len(data), outgoing_name)
            if not self.index.record_message(record):
                await store._client.delete_messages(store._entity, [repost_id])
                await self._delete(event)
                return
            await self._delete(event)
            log.info("librarian reposted tile fp=%s tile=%d", fp[:8], k)
        except Exception as exc:
            log.warning("librarian repost failed (%s)", type(exc).__name__)

    async def _allow_sender(self, event, sender_id: int) -> bool:
        now = self.clock()
        for sender, until in tuple(self._blocked.items()):
            if until <= now:
                self._blocked.pop(sender, None)
        if self._blocked.get(sender_id, 0) > now:
            return False
        try:
            sender = await event.get_sender()
            if bool(getattr(sender, "bot", False)):
                return False
        except Exception:
            return False
        last = self._senders.get(sender_id, 0)
        while self._recent and self._recent[0] < now - 60:
            self._recent.popleft()
        if now - last < self.per_sender_s or len(self._recent) >= self.global_per_minute:
            self._block(sender_id)
            return False
        self._senders[sender_id] = now
        self._recent.append(now)
        if len(self._senders) > 512:
            oldest = min(self._senders, key=self._senders.get)
            self._senders.pop(oldest, None)
        return True

    def _block(self, sender_id: int) -> None:
        self._blocked[sender_id] = self.clock() + 60
        if len(self._blocked) > 512:
            oldest = min(self._blocked, key=self._blocked.get)
            self._blocked.pop(oldest, None)

    async def _channel_has_key(self, store: TelegramMTProtoStore, fp: str, profile: str, k: int) -> bool:
        # The local index is rebuilt before serving. Telegram search covers the
        # short interval before the periodic scanner sees a direct contribution.
        try:
            async for message in store._client.iter_messages(store._entity, search=fp, limit=100):
                caption = getattr(message, "message", None) or getattr(message, "text", "") or ""
                match = _CAPTION.fullmatch(caption)
                if match and match.group(1) == fp and match.group(2) == profile and int(match.group(3)) == k:
                    if await self._valid_existing(store, message, fp, profile, k):
                        return True
        except Exception as exc:
            log.info("librarian duplicate search failed (%s)", type(exc).__name__)
        return False

    async def _valid_existing(self, store, message, fp: str, profile: str, k: int) -> bool:
        document = getattr(message, "document", None)
        caption = getattr(message, "message", None) or getattr(message, "text", "") or ""
        match = _CAPTION.fullmatch(caption)
        if not document or not match or match.group(1) != fp or match.group(2) != profile or int(match.group(3)) != k:
            return False
        try:
            size = int(getattr(document, "size", 0) or 0)
            if size < 125 or size > MAX_TILE_BYTES:
                return False
            name = self._file_name(document)
            metadata = _tile_metadata_from_filename(name, k)
            if metadata is None:
                pts_ms, duration_ms = k * TILE_INTERVAL_MS, MAX_DURATION_MS
                n_tiles = tile_count(duration_ms)
            else:
                pts_ms, duration_ms, n_tiles = metadata
            data = await store._download_bounded(document)
            if len(data) != size or validate_tile(fp, profile, k, pts_ms, duration_ms, n_tiles, data) != match.group(4):
                return False
            self.index.record_message(ChannelMessage(int(message.id), caption, size, name))
            return True
        except Exception:
            return False

    @staticmethod
    def _file_name(document) -> str:
        attrs = getattr(document, "attributes", ()) if document else ()
        return next((getattr(attr, "file_name", "") for attr in attrs
                     if getattr(attr, "file_name", "")), "")

    @staticmethod
    async def _delete(event) -> None:
        try:
            await event.delete()
        except Exception:
            pass
