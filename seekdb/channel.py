from __future__ import annotations

import secrets

from seekdb.channel_index import ChannelIndex
from seekdb.protocol.nvmt import NVMT_PROFILES, PoolError


class ChannelReadService:
    """Read-only package API assembled from the local Telegram channel index."""

    def __init__(self, index: ChannelIndex, read_key: str = ""):
        self.index = index
        self.read_key = read_key

    def authorize(self, given: str | None, write: bool = False) -> bool:
        return not write and bool(given and self.read_key and secrets.compare_digest(given, self.read_key))

    def manifest(self, fp: str, profile: str):
        return self.index.manifest(fp, profile)

    def tile(self, fp: str, profile: str, slot: int):
        return self.index.tile(fp, profile, slot)

    def package(self, fp: str, profile: str):
        return self.index.package(fp, profile)

    def package_slots(self, fp: str, profile: str, slots: list[int]):
        return self.index.package_slots(fp, profile, slots)

    def episode_candidates(self, episode_key: str, duration_ms: int):
        return self.index.episode_candidates(episode_key, duration_ms)

    def index_episode_package(self, fp: str, profile: str, episode_key: str) -> bool:
        if profile not in NVMT_PROFILES:
            raise PoolError(400, "bad_key")
        return self.index.index_episode_package(fp, profile, episode_key)
