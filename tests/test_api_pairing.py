from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from seekdb.config import Settings
from seekdb.pairing import client_proof, derive_key
from seekdb.protocol.nvmt import TILE_PROFILE, decode_nvmt, encode_nvmt


FP = "b" * 64
PROFILE = "nvmt-v1-g5"
DURATION_MS = 20_000


def _package(tiles):
    return encode_nvmt(5_000, 4, tiles)


def test_host_api_pair_upload_merge_sparse_and_duration_conflict(tmp_path, jpeg_bytes):
    settings = Settings(data_dir=tmp_path, pool_mode="host", store="fs")
    with TestClient(__import__("seekdb.api.app", fromlist=["create_app"]).create_app(settings)) as client:
        app = client.app
        invite = app.state.invites.create(role="contributor", max_uses=1)
        code = invite["code"].replace("-", "")
        started = client.post("/v1/pair/start", json={"invite": code[:4]})
        assert started.status_code == 200
        start = started.json()
        key = derive_key(code[4:], start["pair_id"], start["salt"], start["spki_pin"], start["iterations"])
        name = "Nuvio TV"
        claimed = client.post("/v1/pair/claim", json={
            "pair_id": start["pair_id"],
            "device_name": name,
            "client_proof": client_proof(key, start["pair_id"], name),
        })
        assert claimed.status_code == 200
        token = claimed.json()["token"]
        assert client.get("/v1/pair/status", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        stored_hashes = [row[0] for row in app.state.index.q("SELECT token_hash FROM devices")]
        assert hashlib.sha256(token.encode()).hexdigest() in stored_hashes
        assert token not in stored_hashes

        first = jpeg_bytes((10, 20, 30))
        later = jpeg_bytes((90, 80, 70))
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/vnd.openseek.nvmt"}
        url = f"/v1/pool/{FP}/nvmt?profile={PROFILE}&duration_ms={DURATION_MS}"
        uploaded = client.put(url + "&episode_key={}".format(hashlib.sha256(b"episode").hexdigest()),
                              content=_package({0: (0, first)}), headers=headers)
        assert uploaded.status_code == 201
        merged = client.put(url, content=_package({2: (10_000, later)}), headers=headers)
        assert merged.status_code == 201

        full = client.get(f"/v1/pool/{FP}/nvmt?profile={PROFILE}", headers=headers)
        assert full.status_code == 200
        assert full.headers["x-openseek-n-tiles"] == "2"
        assert decode_nvmt(FP, PROFILE, DURATION_MS, full.content)[2] == {0: (0, first), 2: (10_000, later)}

        sparse = client.get(f"/v1/pool/{FP}/nvmt?profile={PROFILE}&slots=1,3", headers=headers)
        assert sparse.status_code == 200
        assert set(decode_nvmt(FP, PROFILE, DURATION_MS, sparse.content)[2]) == {0, 2}

        duplicate = client.put(url, content=_package({0: (0, jpeg_bytes((200, 10, 10)))}), headers=headers)
        assert duplicate.status_code == 200
        conflict = client.put(f"/v1/pool/{FP}/nvmt?profile={PROFILE}&duration_ms=25000",
                              content=encode_nvmt(5_000, 5, {0: (0, first)}), headers=headers)
        assert conflict.status_code == 409
        assert conflict.headers["x-openseek-duration-ms"] == str(DURATION_MS)

        candidates = client.get(f"/v1/pool/episodes/{hashlib.sha256(b'episode').hexdigest()}?duration_ms={DURATION_MS}",
                                headers=headers)
        assert candidates.status_code == 200
        assert candidates.json()["candidates"][0]["fp"] == FP

        jpeg = jpeg_bytes()
        tile_query = f"profile={TILE_PROFILE}&pts_ms=0&duration_ms={DURATION_MS}&n_tiles=2"
        tile_url = f"/v1/pool/{FP}/tiles/0.jpg?{tile_query}"
        tile_upload = client.put(tile_url, content=jpeg,
                                 headers={**headers, "Content-Type": "image/jpeg"})
        assert tile_upload.status_code == 201
        tile_read = client.get(f"/v1/pool/{FP}/tiles/0.jpg?profile={TILE_PROFILE}", headers=headers)
        assert tile_read.status_code == 200 and tile_read.content == jpeg
        manifest = client.get(f"/v1/pool/{FP}/manifest?profile={TILE_PROFILE}", headers=headers)
        assert manifest.status_code == 200 and manifest.json()["tiles"][0]["k"] == 0

        assert client.post("/v1/pair/start", json={"invite": code[:4]}).status_code == 409

