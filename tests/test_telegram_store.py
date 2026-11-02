from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from email.parser import BytesParser
from email.policy import default
from urllib.parse import parse_qs, urlsplit

from seekdb.index.sqlite import SQLiteIndex
from seekdb.pool import PoolService
from seekdb.protocol.nvmt import encode_nvmt
from seekdb.store.telegram_bot import TelegramBotStore, TelegramObjectStore


class FakeResponse:
    def __init__(self, data: bytes):
        self.data = data

    def read(self, size=-1):
        return self.data if size < 0 else self.data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeBotAPI:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.next_id = 1
        self.deleted: list[int] = []

    def urlopen(self, request, timeout=8):
        parsed = urlsplit(request.full_url)
        if parsed.path.endswith("/sendDocument"):
            mime = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.get_header("Content-type").encode() +
                b"\r\nMIME-Version: 1.0\r\n\r\n" + (request.data or b""))
            part = next(item for item in mime.iter_parts()
                        if item.get_param("name", header="content-disposition") == "document")
            file_id = f"file-{self.next_id}"
            data = part.get_payload(decode=True)
            self.files[file_id] = data
            result = {"ok": True, "result": {"message_id": self.next_id,
                                                   "document": {"file_id": file_id}}}
            self.next_id += 1
            return FakeResponse(json.dumps(result).encode())
        if parsed.path.endswith("/getFile"):
            file_id = parse_qs((request.data or b"").decode()).get("file_id", [""])[0]
            if file_id not in self.files:
                body = io.BytesIO(b'{"ok":false,"error_code":400}')
                raise urllib.error.HTTPError(request.full_url, 400, "missing", {}, body)
            return FakeResponse(json.dumps({"ok": True, "result": {
                "file_path": f"docs/{file_id}.jpg"}}).encode())
        if parsed.path.endswith("/deleteMessage"):
            args = parse_qs((request.data or b"").decode())
            self.deleted.append(int(args["message_id"][0]))
            return FakeResponse(b'{"ok":true,"result":true}')
        if "/file/bot" in parsed.path:
            file_id = parsed.path.rsplit("/", 1)[-1].removesuffix(".jpg")
            return FakeResponse(self.files[file_id])
        raise AssertionError(f"unexpected Bot API request {request.full_url}")


def test_telegram_bot_object_store_round_trip_dedupe_and_gc(tmp_path, jpeg_bytes):
    index = SQLiteIndex(tmp_path / "seekdb.db")
    api = FakeBotAPI()
    store = TelegramObjectStore(TelegramBotStore("123456:secret", "-100123", opener=api.urlopen), index)
    try:
        pool = PoolService(index, store)
        jpeg = jpeg_bytes()
        package = encode_nvmt(5_000, 4, {0: (0, jpeg)})
        result = pool.upload_package("d" * 64, "nvmt-v1-g5", 20_000, package)
        assert result["stored"] and api.next_id == 2
        key = index.get_package("d" * 64, "nvmt-v1-g5")["object_key"]
        assert store.get(key) == package
        assert store.put(package, "nvmt") == key
        assert api.next_id == 2
        assert pool.package("d" * 64, "nvmt-v1-g5")[0] == package

        index.delete_package("d" * 64, "nvmt-v1-g5")
        report = pool.gc(dry_run=False)
        assert report["deleted"] == [key]
        assert api.deleted == [1]
        assert not store.exists(key)
    finally:
        index.close()

