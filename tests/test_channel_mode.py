from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from seekdb.api.app import create_app
from seekdb.channel_index import ChannelIndex
from seekdb.config import Settings
from seekdb.index.sqlite import SQLiteIndex
from seekdb.protocol.nvmt import TILE_PROFILE, decode_nvmt
from seekdb.store.telegram_bot import _tile_filename
from seekdb.telegram_mt import ChannelMessage


FP = "c" * 64
DURATION_MS = 20_000
N_TILES = 2


class MemoryChannel:
    channel_id = "-100123"

    def __init__(self):
        self.messages: list[ChannelMessage] = []
        self.data: dict[int, bytes] = {}

    def add(self, message_id: int, slot: int, data: bytes):
        pts = slot * 10_000
        caption = f"openseek:v1:{FP}:{TILE_PROFILE}:{slot}:{hashlib.sha256(data).hexdigest()}"
        filename = _tile_filename(pts, DURATION_MS, N_TILES)
        self.messages.append(ChannelMessage(message_id, caption, len(data), filename))
        self.data[message_id] = data

    def scan(self, min_id=0, limit=2000):
        return [message for message in sorted(self.messages, key=lambda m: m.message_id)
                if message.message_id > min_id][:limit]

    def get_tile(self, message_id):
        if message_id not in self.data:
            from seekdb.protocol.nvmt import PoolError
            raise PoolError(404, "telegram_message_not_found")
        return self.data[message_id]

    def start(self):
        return None

    def close(self):
        return None


def _settings(data_dir: Path) -> Settings:
    return Settings(data_dir=data_dir, pool_mode="channel", store="telegram", public_read=True,
                    telegram_channel_id=MemoryChannel.channel_id, telegram_api_id=1,
                    telegram_api_hash="a" * 32, telegram_session_file=data_dir / "user.session")


def test_channel_indexes_shared_telegram_tiles_and_serves_read_only_packages(tmp_path, jpeg_bytes):
    channel = MemoryChannel()
    first, second = jpeg_bytes((30, 60, 90)), jpeg_bytes((90, 60, 30))
    channel.add(11, 0, first)
    channel.add(12, 1, second)

    with TestClient(create_app(_settings(tmp_path), store=channel)) as client:
        assert client.app.state.channel_index.status()["tiles"] == 2
        episode_key = hashlib.sha256(b"channel episode").hexdigest()
        response = client.get(f"/v1/pool/{FP}/nvmt?profile=nvmt-v1-g5&episode_key={episode_key}")
        assert response.status_code == 200
        grid, slots, tiles = decode_nvmt(FP, "nvmt-v1-g5", DURATION_MS, response.content)
        assert grid == 5_000 and slots == 4
        assert tiles == {0: (0, first), 2: (10_000, second)}

        sparse = client.get(f"/v1/pool/{FP}/nvmt?profile=nvmt-v1-g5&slots=1,3")
        assert sparse.status_code == 200
        assert set(decode_nvmt(FP, "nvmt-v1-g5", DURATION_MS, sparse.content)[2]) == {0, 2}

        candidates = client.get(f"/v1/pool/episodes/{episode_key}?duration_ms={DURATION_MS}")
        assert candidates.status_code == 200
        assert candidates.json()["candidates"][0]["fp"] == FP
        assert client.put(f"/v1/pool/{FP}/nvmt?profile=nvmt-v1-g5&duration_ms={DURATION_MS}",
                          content=response.content,
                          headers={"Content-Type": "application/vnd.openseek.nvmt"}).status_code == 405


def test_channel_index_rebuilds_independent_sqlite_views(tmp_path, jpeg_bytes):
    channel = MemoryChannel()
    channel.add(3, 0, jpeg_bytes())
    indexes = []
    try:
        for name in ("tv-a", "tv-b"):
            index = SQLiteIndex(tmp_path / name / "seekdb.db")
            channel_index = ChannelIndex(index, channel, channel_id=channel.channel_id)
            assert channel_index.rebuild()["indexed"] == 1
            assert channel_index.lookup(FP, TILE_PROFILE, 0)["message_id"] == 3
            indexes.append(index)
        # Removing one materialized view and rebuilding reconstructs it from Telegram.
        indexes[1].x("DELETE FROM pool_channel_tiles")
        rebuilt = ChannelIndex(indexes[1], channel, channel_id=channel.channel_id)
        assert rebuilt.rebuild()["indexed"] == 1
        assert rebuilt.lookup(FP, TILE_PROFILE, 0)["message_id"] == 3
    finally:
        for index in indexes:
            index.close()


def test_channel_setup_keeps_user_scan_session_separate_from_librarian_bot(tmp_path):
    settings = Settings(data_dir=tmp_path, pool_mode="channel", store="telegram",
                        telegram_bot_token="123456:secret", telegram_channel_id="-100123",
                        telegram_api_id=1, telegram_api_hash="a" * 32,
                        telegram_session_file=tmp_path / "user.session", librarian_enabled=True)
    app = create_app(settings)
    assert app.state.channel_store.settings.telegram_bot_token == ""
    assert app.state.librarian_store.settings.telegram_bot_token == "123456:secret"
    assert app.state.librarian_store.settings.telegram_session_file == tmp_path / "telegram" / "librarian-bot.session"

