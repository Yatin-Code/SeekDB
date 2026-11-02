# Nuvio pool protocol

SeekDB implements the existing Nuvio v1 pool contract. Requests use HTTPS and
do not redirect. Pairing uses OSP1, and paired requests send a bearer device
token. Deployments that use legacy pool keys may send the read or write key as
a bearer credential.

## Routes

| Method | Path | Access |
|---|---|---|
| `GET` | `/v1/health` | Public liveness |
| `POST` | `/v1/pair/start` | Public, requires an invite selector |
| `POST` | `/v1/pair/claim` | Public, one-time proof |
| `GET` | `/v1/pair/status` | Paired device |
| `DELETE` | `/v1/pair` | Paired device, revoke itself |
| `GET` | `/v1/pool/{fp}/manifest` | Paired reader or read key |
| `GET` | `/v1/pool/{fp}/tiles/{n}.jpg` | Paired reader or read key |
| `GET` | `/v1/pool/{fp}/nvmt` | Paired reader or read key |
| `GET` | `/v1/pool/episodes/{key}` | Paired reader or read key |
| `PUT` | `/v1/pool/{fp}/tiles/{n}.jpg` | Contributor or write key, host only |
| `PUT` | `/v1/pool/{fp}/nvmt` | Contributor or write key, host only |

Package responses use `application/vnd.openseek.nvmt` and include
`X-OpenSeek-Duration-Ms`, `X-OpenSeek-N-Tiles`, `X-OpenSeek-Profile`, and
`X-OpenSeek-Sha256`. The digest covers the exact response body. A sparse request
uses `?profile=nvmt-v1-g5&slots=0,4,8`; each requested slot may fall back to a
stored neighboring slot when it is within one slot.

## NVMT v1

The 20-byte little-endian header contains `NVMT`, version `1`, grid size,
duration slot count, and tile count. Each tile record contains slot, timestamp
in milliseconds, JPEG byte length, and JPEG bytes. Supported profiles are
`nvmt-v1-g5` (5-second grid) and `nvmt-v1-g10` (10-second grid).

SeekDB limits packages to 16 MiB, slot counts to 8,640, and JPEG payloads to
200,000 bytes per package record. The tile validator requires a bounded JPEG
with 320 × 180 frame dimensions. For an existing `(fingerprint, profile)`, the
first accepted tile for each slot is immutable. Uploads merge missing slots;
duration conflicts return `409 media_metadata_conflict` and the stored duration
header.

The detailed wire and compatibility contract is recorded in
[`PLAN-SEEKDB.md`](PLAN-SEEKDB.md).
