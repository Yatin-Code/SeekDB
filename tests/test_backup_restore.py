from __future__ import annotations

from seekdb.cli import _command_backup, _command_restore
from seekdb.config import Settings
from seekdb.index.sqlite import SQLiteIndex
from seekdb.pool import PoolService
from seekdb.protocol.nvmt import decode_nvmt, encode_nvmt
from seekdb.store.fs import FileSystemObjectStore
from seekdb.tls import ensure_certificate
from PIL import Image
import io


def _jpeg() -> bytes:
    image = io.BytesIO()
    Image.new("RGB", (320, 180), (12, 34, 56)).save(image, "JPEG", quality=75)
    return image.getvalue()


def test_backup_restore_preserves_pool_data_and_tls_identity(tmp_path):
    source_data = tmp_path / "source-data"
    source = Settings(data_dir=source_data, pool_mode="host", store="fs")
    index = SQLiteIndex(source_data / "seekdb.db")
    try:
        pin_before = ensure_certificate(source_data)[2]
        pool = PoolService(index, FileSystemObjectStore(source_data))
        jpeg = _jpeg()
        body = encode_nvmt(5_000, 4, {0: (0, jpeg)})
        pool.upload_package("f" * 64, "nvmt-v1-g5", 20_000, body)
    finally:
        index.close()

    backup = tmp_path / "backup"
    assert _command_backup(backup, source) == 0

    restored = Settings(data_dir=tmp_path / "restored", pool_mode="host", store="fs")
    assert _command_restore(backup, restored) == 0
    restored_index = SQLiteIndex(restored.data_dir / "seekdb.db")
    try:
        restored_pool = PoolService(restored_index, FileSystemObjectStore(restored.data_dir))
        downloaded, _meta = restored_pool.package("f" * 64, "nvmt-v1-g5")
        assert decode_nvmt("f" * 64, "nvmt-v1-g5", 20_000, downloaded)[2][0][1] == _jpeg()
        assert ensure_certificate(restored.data_dir)[2] == pin_before
    finally:
        restored_index.close()

