from __future__ import annotations

import hashlib

import pytest

from seekdb.config import Settings
from seekdb.index.sqlite import SQLiteIndex
from seekdb.pool import PoolService
from seekdb.protocol.nvmt import (MAX_NVMT_BYTES, PoolError, decode_nvmt, encode_nvmt,
                                  validate_tile)
from seekdb.store.fs import FileSystemObjectStore


FP = "a" * 64
PROFILE = "nvmt-v1-g5"
DURATION_MS = 20_000


def package(tiles: dict[int, tuple[int, bytes]], duration_ms=DURATION_MS) -> bytes:
    grid = 5_000
    return encode_nvmt(grid, (duration_ms + grid - 1) // grid, tiles)


def test_nvmt_round_trip_and_validation_boundaries(jpeg_bytes):
    jpeg = jpeg_bytes()
    body = package({0: (0, jpeg), 2: (10_000, jpeg)})

    grid, slots, decoded = decode_nvmt(FP, PROFILE, DURATION_MS, body)
    assert (grid, slots) == (5_000, 4)
    assert decoded == {0: (0, jpeg), 2: (10_000, jpeg)}
    assert validate_tile(FP, "t320x180q75g10v1", 0, 0, DURATION_MS, 2, jpeg)

    with pytest.raises(PoolError, match="bad_key"):
        decode_nvmt("bad", PROFILE, DURATION_MS, body)
    with pytest.raises(PoolError, match="bad_package"):
        decode_nvmt(FP, PROFILE, DURATION_MS, b"bad")
    with pytest.raises(PoolError, match="package_too_large"):
        decode_nvmt(FP, PROFILE, DURATION_MS, b"x" * (MAX_NVMT_BYTES + 1))
    duplicate = bytearray(package({0: (0, jpeg)}))
    record = bytes(duplicate[20:])
    duplicate[16:20] = (2).to_bytes(4, "little")
    duplicate.extend(record)
    with pytest.raises(PoolError, match="bad_package_tile"):
        decode_nvmt(FP, PROFILE, DURATION_MS, bytes(duplicate))


def test_filesystem_pool_merges_first_writer_wins_and_survives_restart(tmp_path, jpeg_bytes):
    db_path = tmp_path / "seekdb.db"
    data_dir = tmp_path / "data"
    first = jpeg_bytes((10, 20, 30))
    conflicting = jpeg_bytes((220, 10, 30))
    later = jpeg_bytes((50, 60, 70))
    episode_key = hashlib.sha256(b"episode").hexdigest()

    index = SQLiteIndex(db_path)
    pool = PoolService(index, FileSystemObjectStore(data_dir), max_storage_bytes=2_000_000)
    assert pool.upload_package(FP, PROFILE, DURATION_MS, package({0: (0, first)}), episode_key)["stored"]
    assert pool.upload_package(FP, PROFILE, DURATION_MS, package({2: (10_000, later)}))["stored"]

    # An overlapping slot cannot replace its original bytes.
    assert pool.upload_package(FP, PROFILE, DURATION_MS,
                              package({0: (0, conflicting)}))["duplicate"]
    full, meta = pool.package(FP, PROFILE)
    assert meta["n_tiles"] == 2
    assert decode_nvmt(FP, PROFILE, DURATION_MS, full)[2] == {0: (0, first), 2: (10_000, later)}

    sparse, sparse_meta = pool.package_slots(FP, PROFILE, [1, 3])
    assert sparse_meta["n_tiles"] == 2
    assert set(decode_nvmt(FP, PROFILE, DURATION_MS, sparse)[2]) == {0, 2}
    assert pool.episode_candidates(episode_key, DURATION_MS)[0]["fp"] == FP

    with pytest.raises(PoolError) as conflict:
        pool.upload_package(FP, PROFILE, DURATION_MS + 5_000,
                            package({0: (0, first)}, DURATION_MS + 5_000))
    assert conflict.value.status == 409
    assert conflict.value.duration_ms == DURATION_MS
    index.close()

    reopened = SQLiteIndex(db_path)
    try:
        restored = PoolService(reopened, FileSystemObjectStore(data_dir))
        downloaded, _ = restored.package(FP, PROFILE)
        assert decode_nvmt(FP, PROFILE, DURATION_MS, downloaded)[2][0][1] == first
    finally:
        reopened.close()


def test_corrupt_package_is_removed_and_gc_collects_its_object(tmp_path, jpeg_bytes):
    index = SQLiteIndex(tmp_path / "seekdb.db")
    try:
        store = FileSystemObjectStore(tmp_path / "data")
        pool = PoolService(index, store)
        pool.upload_package(FP, PROFILE, DURATION_MS, package({0: (0, jpeg_bytes())}))
        object_key = index.get_package(FP, PROFILE)["object_key"]
        store._path(object_key).write_bytes(b"corrupt")

        with pytest.raises(PoolError, match="stored_package_corrupt"):
            pool.package(FP, PROFILE)
        assert index.get_package(FP, PROFILE) is None
        report = pool.gc(dry_run=True)
        assert report["objects"] == 1
        pool.gc(dry_run=False)
        assert not store.exists(object_key)
    finally:
        index.close()


def test_legacy_pool_enabled_alias_and_local_mode():
    legacy = Settings.from_env({"POOL_ENABLED": "1"})
    assert legacy.effective_pool_mode == "host"
    assert Settings.from_env({}).effective_pool_mode == "local"

