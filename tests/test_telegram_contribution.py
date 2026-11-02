from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

from seekdb.channel_index import ChannelIndex
from seekdb.config import Settings
from seekdb.contribution import TelegramLibrarian
from seekdb.index.sqlite import SQLiteIndex
from seekdb.protocol.nvmt import TILE_PROFILE
from seekdb.store.telegram_bot import _tile_filename, _tile_metadata_from_filename
from seekdb.telegram_mt import ChannelMessage, TelegramMTProtoStore


FP = "e" * 64


class FakeTelethonClient:
    def __init__(self):
        self.messages = {}
        self.next_id = 1

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def is_user_authorized(self):
        return True

    async def get_entity(self, channel_id):
        return channel_id

    async def get_permissions(self, _entity, _user):
        return SimpleNamespace(send_messages=True, post_messages=True)

    async def send_file(self, _entity, file, caption, **_kwargs):
        source, filename = file
        data = source.read()
        message_id = self.next_id
        self.next_id += 1
        document = SimpleNamespace(size=len(data), data=data,
                                   attributes=[SimpleNamespace(file_name=filename)])
        message = SimpleNamespace(id=message_id, message=caption, document=document)
        self.messages[message_id] = message
        return message

    async def get_messages(self, _entity, ids):
        return self.messages.get(ids)

    async def iter_messages(self, _entity, min_id=0, limit=None, reverse=False, search=None, **_kwargs):
        values = [message for mid, message in sorted(self.messages.items()) if mid > min_id]
        if search:
            values = [message for message in values if search in message.message]
        if not reverse:
            values.reverse()
        for message in values[:limit]:
            yield message

    async def iter_download(self, document, **_kwargs):
        yield document.data

    async def delete_messages(self, _entity, ids):
        for message_id in ids:
            self.messages.pop(message_id, None)


class FakeEvent:
    def __init__(self, caption, data, filename, *, sender_bot=False, sender_id=123):
        document = SimpleNamespace(size=len(data),
                                   attributes=[SimpleNamespace(file_name=filename)])
        self.message = SimpleNamespace(document=document, message=caption)
        self.sender_id = sender_id
        self.is_private = True
        self.sender_bot = sender_bot
        self.deleted = False

    async def get_sender(self):
        return SimpleNamespace(bot=self.sender_bot)

    async def delete(self):
        self.deleted = True


def test_user_session_can_post_scan_and_download_channel_tiles(tmp_path, jpeg_bytes):
    settings = Settings(data_dir=tmp_path, pool_mode="channel", contribution=True,
                        telegram_api_id=123, telegram_api_hash="a" * 32,
                        telegram_channel_id="-100123", telegram_session_file=tmp_path / "user.session")
    client = FakeTelethonClient()
    store = TelegramMTProtoStore(settings, client_factory=lambda *_args, **_kwargs: client)
    data = jpeg_bytes()
    digest = hashlib.sha256(data).hexdigest()
    caption = f"openseek:v1:{FP}:{TILE_PROFILE}:0:{digest}"
    try:
        store.start()
        message_id = store.put_tile(data, caption, {"pts_ms": 0, "duration_ms": 10_000, "n_tiles": 1})
        assert store.get_tile(message_id) == data
        assert store.scan(0, 10) == [
            ChannelMessage(message_id, caption, len(data), _tile_filename(0, 10_000, 1))]
    finally:
        store.close()


class FakeLibrarianStore:
    def __init__(self, data):
        self.data = data
        self.channel_id = "-100123"
        self._entity = "channel"
        self._client = FakeTelethonClient()

    async def _download_bounded(self, _document):
        return self.data

    async def _retry(self, operation):
        return await operation()

    def get_tile(self, message_id):
        message = self._client.messages.get(message_id)
        if message is None:
            from seekdb.protocol.nvmt import PoolError
            raise PoolError(404, "telegram_message_not_found")
        return message.document.data

    def scan(self, min_id=0, limit=2000):
        messages = []
        for mid, message in sorted(self._client.messages.items()):
            if mid <= min_id:
                continue
            attrs = message.document.attributes
            filename = next((attr.file_name for attr in attrs if attr.file_name), "")
            messages.append(ChannelMessage(mid, message.message, message.document.size, filename))
        return messages[:limit]


def test_librarian_reposts_valid_contribution_and_drops_bot_sender(tmp_path, jpeg_bytes):
    data = jpeg_bytes()
    digest = hashlib.sha256(data).hexdigest()
    filename = _tile_filename(0, 10_000, 1)
    caption = f"openseek:v1:{FP}:{TILE_PROFILE}:0:{digest}"
    store = FakeLibrarianStore(data)
    index = SQLiteIndex(tmp_path / "seekdb.db")
    try:
        channel_index = ChannelIndex(index, store, channel_id=store.channel_id)
        librarian = TelegramLibrarian(store, channel_index)

        event = FakeEvent(caption, data, filename)
        asyncio.run(librarian.handle_message(event, store))
        assert event.deleted
        assert channel_index.lookup(FP, TILE_PROFILE, 0)["message_id"] == 1
        assert store.get_tile(1) == data

        bot_event = FakeEvent(caption, data, filename, sender_bot=True, sender_id=456)
        asyncio.run(librarian.handle_message(bot_event, store))
        assert bot_event.deleted
        assert store._client.next_id == 2

        bad = FakeEvent(caption[:-1] + ("0" if caption[-1] != "0" else "1"), data,
                        filename, sender_id=789)
        asyncio.run(librarian.handle_message(bad, store))
        assert bad.deleted
        assert store._client.next_id == 2
    finally:
        index.close()

