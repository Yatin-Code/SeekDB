# Plan: SeekDB — a standalone thumbnail pool

Status: v1 implementation is in place; all 14 focused Python tests and the Nuvio pool client checks pass. Live Telegram deployment checks, broader load testing, and resource measurement remain. Cloud phases remain future work.
Scope: new `seekdb` service; OpenSeek becomes an optional generator, not the pool runtime.
Implementation rule: copy working pool code and tests from `openseek-telegram`; do not rewrite them from scratch.

## What is actually new

Almost nothing in the core is new. SeekDB is a slimmed `openseek-telegram`, not
a new implementation.

New logic:

- `channel` mode's local read-only `/v1/pool/*` endpoint;
- the app/storage glue that removes `Core` / `jobs.py` coupling;
- env-only packaging, `.env.example`, and the Docker entrypoint.

Everything else — protocol, validation, first-writer-wins, pairing, TLS, auth,
Telegram MTProto, channel index, librarian, pool routes, and tests — is copied
and pruned.

If v1 supports only `host` + Telegram and `channel`, the filesystem store is
optional. That makes v1 almost pure copy-and-prune.

## The decision

Build **SeekDB**, a small service that only:

1. accepts finished NVMT thumbnail packages or individual JPEG tiles;
2. validates and stores them;
3. indexes them by fingerprint, profile, duration, and episode;
4. serves them to compatible players such as Nuvio.

SeekDB does **not** decode video, probe sources, run FFmpeg/PyAV, proxy video,
manage a library, generate subtitles, or schedule thumbnail jobs. Nuvio already
generates thumbnails locally in `src/miniatura.c`; OpenSeek's generation stack is
not needed to run a pool.

This is a decomposition, not a rewrite of the wire protocol. SeekDB speaks the
existing Nuvio pool protocol, so current TVs keep working.

## Why the old plan was too heavy

The current `openseek-telegram` implementation works, but it carries the whole
OpenSeek runtime:

- PyAV/FFmpeg workers and video source handling;
- HTTP Range proxy, playback guard, and job scheduler;
- a large Python image, native codecs, and a long-lived process;
- Telegram MTProto as the primary storage path;
- pool code coupled to `Core`, `jobs.py`, `cache.py`, and the OpenSeek API.

Almost none of that is required to collect and serve already-rendered tiles.
The pool role is a small database plus object storage plus a validation API.
Separating it makes the service easier to host, test, secure, and reason about.

Extracting the pool into a new package is safer than amputating OpenSeek in
place. The current pool code depends on OpenSeek's database, cache, auth, and
`Core`, so deleting modules in place would leave hidden coupling. A standalone
package forces a clean boundary: OpenSeek may still generate thumbnails for
other clients, but only SeekDB owns the pool.


## What SeekDB is

| Concern | SeekDB |
|---|---|
| Modes | `POOL_MODE=host` (pool API) or `POOL_MODE=channel` (local Telegram index); unset = local-only |
| Input | NVMT v1 packages, optional individual JPEG tiles |
| Output | NVMT packages, sparse NVMT subsets, episode candidates, manifests |
| Storage | local filesystem, Telegram (first-class), S3/R2 (cloud) |
| Index | SQLite (single node) or PostgreSQL (cloud) |
| Auth | OSP1 device pairing, invite codes, read/write roles |
| Transport | HTTPS with a stable SPKI pin, or standard CA in v2 |
| Deployment | one container, one binary, or a stateless serverless handler |

## What SeekDB is not

- Not a media server, player, transcoder, or debrid client.
- Not a metadata scraper or library manager.
- Not a replacement for Nuvio's local thumbnail generator.
- Not a social server, recommendation engine, or watch-party backend.
- Not a mandatory public service. A user can run it on a LAN or a NAS.

## Operator burden

The host still runs one service, but far less of one:

| Task | OpenSeek pool host | SeekDB host |
|---|---|---|
| FFmpeg/PyAV, video probing, range proxy | yes | no |
| Thumbnail generation | yes | no; Nuvio does it |
| Telegram bot/channel/session | required | first-class backend (choose FS or Telegram) |
| Shared index and outbox | OpenSeek SQLite + jobs | SeekDB SQLite |
| Validate uploads | yes | yes |
| TLS, pin, pairing, invites | yes | yes, one process |
| Disk, bandwidth, uptime | large | small |
| Approve each contributor | no, but keys | no, invite codes |

The host is still responsible for storage and abuse. SeekDB cannot make a public
pool free of an operator; it makes the operator's job small enough to run on a
free VM or a NAS.

## What SeekDB takes from OpenSeek

SeekDB is not a greenfield protocol implementation. The current
`openseek-telegram` tree already contains the hard, tested parts. We take the
pool slice and leave the media engine.

**Do not hesitate to copy from `openseek-telegram`.** Most of SeekDB already
exists there. Prefer copying a working module, pruning the media imports, and
adapting it over rewriting from scratch. Copy the tests and fixtures too. Only
rewrite where the current code is coupled to `Core`, `jobs.py`, or a single
storage object: the storage boundary, app wiring, and the channel local
endpoint. Everything else should be copy-and-prune.

### Copy the mode model exactly

`POOL_MODE=host|channel` does not change. SeekDB keeps it, including the legacy
fallback:

| Mode | Behaviour | Code to copy |
|---|---|---|
| `host` | This instance runs the pool API and serves TVs. It can store packages on the local filesystem or in Telegram. If `POOL_URL` is set instead, it acts as a client to another host. | `PoolService`, `TelegramBotStore`, `PoolClient`, `PoolSync`, pool HTTP routes, admin routes, read/write keys |
| `channel` | No shared pool API. This instance indexes one shared Telegram group/channel locally and serves its own TVs through a local read-only pool endpoint. Telegram is the source of truth; SQLite is a rebuildable index. | `TelegramMTProtoStore`, `ChannelIndex`, `ChannelPoolClient`, `ChannelPoolSync`, `TelegramLibrarian`, channel admin/scan commands |
| unset + `POOL_ENABLED=1` | Legacy compatibility: resolve to `host`. | `Settings.effective_pool_mode` |
| unset | Local-only. No pool API, no Telegram, no scanning, no contribution. | Existing config validation |

`POOL_MODE=off` stays out of the user-facing choices. An unset mode already
means local-only.

`SEEKDB_STORE=fs|telegram` is a sub-choice inside `host` mode. It does not
replace `POOL_MODE`. `channel` mode always uses Telegram.

### Take as-is

| Module | What it provides | Why SeekDB needs it |
|---|---|---|
| `pool.py` | `NVMT_PROFILES`, `TILE_INTERVAL_MS`, `MAX_TILE_BYTES`, `MAX_NVMT_BYTES`, `decode_nvmt`, `encode_nvmt`, `validate_tile`, caption regex | The exact wire format and validation the TV already trusts |
| `pool.py` `PoolService` | first-writer-wins, package merge, duration conflict, episode index, sparse slots | The pool semantics are already correct and tested |
| `pool.py` `TelegramBotStore` | Bot API upload/download, caption metadata, message deletion | Telegram write path for `host` |
| `pool.py` `PoolClient` / `PoolSync` | HTTP client and durable outbox for a remote `host` | OpenSeek peers and host-to-host sync |
| `pool.py` `ChannelPoolClient` / `ChannelPoolSync` | local index reader and durable publisher | `channel` read/write path |
| `telegram_mt.py` | Telethon client, session handling, `join_invite`, `put_tile`, `get_tile`, `scan`, retry, bounded download | Telegram history and downloads; Bot API cannot list old posts |
| `channel_index.py` | scan, upsert, rebuild, manifest, package slots, episode candidates | Rebuildable Telegram index |
| `contribution.py` | librarian DM relay, validation, per-sender limits, blocklist | No-approval Telegram contribution |
| `pairing.py` | OSP1 PBKDF2/HMAC proof, `InviteManager`, code normalization, TTL/uses | Device pairing and invite codes |
| `tls.py` | self-signed P-256 certificate, SPKI pin, certificate persistence | v1 transport and TV pin |
| `auth.py` | device token hashes, roles, admin token, secret storage | Revocable device auth |
| `db.py` | pool, device, invite, outbox schema and migrations | Metadata index |
| `cache.py` | `jpeg_ok`, `atomic_write`, `secure_mkdir` | Small, safe file helpers |
| `ids.py` | fingerprint validation | Reject malformed keys early |
| `logutil.py` | URL/token/code redaction | Never leak secrets in logs |
| `app.py` | route shapes, bearer auth, body limits, error envelope, pool routes | The v1 HTTP contract |
| `config.py` | `pool_mode`, `effective_pool_mode`, `pool_host_enabled`, `pool_upload`, mode-specific validation and ignored-variable warnings | Preserve `host`/`channel` exactly |
| `cli.py` | command structure, `pool scan/rebuild/status/login` | Admin operations; not a setup wizard |
| `tests/test_pool.py` | two-instance fake Bot API tests | `host` regression safety |
| `tests/test_pool_channel.py` | channel scan/share/rebuild tests | `channel` regression safety |

### Adapt

| OpenSeek shape | SeekDB shape |
|---|---|
| `PoolService` talks directly to `DB` and a storage object | `PackageService` talks to `Index` and `ObjectStore` interfaces |
| `ChannelPoolClient` / `ChannelPoolSync` are Telegram-specific | `PoolClient` / `PoolSync` select FS, Telegram, or S3 |
| `db.py` has OpenSeek and pool tables | Keep only devices, invites, packages, tiles, episodes, objects, audit |
| `app.py` owns jobs, cache, proxy, previews | `api/app.py` owns only pairing, pool, health, admin |
| `setup.py` writes an env file interactively | v1 is env-only: ship `.env.example`; no wizard |
| `docs/TELEGRAM-POOL.md` documents OpenSeek hosting | `docs/HOSTING.md` documents SeekDB profiles |

### Keep for compatibility

- `POOL_MODE=host|channel`, legacy `POOL_ENABLED=1`, and the unset/local-only
  behaviour.
- `POOL_URL`, `POOL_READ_KEY`, `POOL_WRITE_KEY`, `POOL_SHARE`,
  `POOL_TIMEOUT_S`.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_API_ID`,
  `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`, `TELEGRAM_SESSION_FILE`.
- `POOL_CHANNEL_SCAN_LIMIT`, `POOL_INDEX_REFRESH_S`, `CONTRIBUTION`,
  `LIBRARIAN`, `LIBRARIAN_KEY`, `LIBRARIAN_ENABLED`.
- `OPENSEEK_*` spellings as aliases for the pool variables, so existing
  deployments keep working.

### Leave behind (media only)

- `engine.py`, `worker.py`, `fetch.py`, `vtt.py`, `timeline.py`, `library.py`,
  `jobs.py`, `guard.py`, `governor.py`, `altpipeline.py`, `altsource.py`,
  `mediasniff.py`, `netpolicy.py`.
- `scheduler.py` except `tile_count()`.
- `public.py`; SeekDB has its own listener and does not need OpenSeek's guest
  API classifier.
- `av`, `Pillow`, `numpy`, the video cache, and every video source path.

### Rewrite, not copy

- **Pairing transport:** OSP1 currently bakes the SPKI pin into the proof. For a
  standard-CA/serverless mode, `spki_pin` must become optional and the stable
  device identity must come from the token, not the certificate. The HMAC
  proof itself stays.
- **Storage boundary:** the current service assumes a single storage object.
  SeekDB needs a real `ObjectStore` interface with FS, S3, and Telegram
  implementations.
- **App wiring:** `Core` currently constructs jobs, cache, proxy, and pool. The
  SeekDB app constructs only the index, store, pairing, and API.
- **Channel local endpoint:** current channel mode uses `ChannelPoolClient`
  inside `jobs.py`; it has no HTTP pool routes. SeekDB must expose a local
  read-only endpoint backed by the channel index so Nuvio can use it directly
  without OpenSeek. Add full-package assembly to the channel client and route
  it like host mode.
- **Config and CLI:** keep the pool variable names and the
  `host`/`channel`/legacy resolution; rename only the binary and data-dir
  settings, and accept `OPENSEEK_*` aliases.
- **Onboarding:** v1 is environment-only. Ship a complete `.env.example`,
  Docker Compose, and copy-paste commands. No interactive wizard, no `init`
  prompt, no `join` prompt. Add a wizard later as a separate convenience layer
  that only writes the same env file.

### Already solved, do not reinvent

- NVMT v1 binary format and per-tile JPEG validation.
- Caption format and content-addressed digests.
- First-writer-wins and duration-conflict behavior.
- Episode key derivation and candidate lookup.
- OSP1 pairing, invite codes, and device token revocation.
- Telegram scan, rebuild, librarian relay, and MTProto download rules.
- The Nuvio v1 HTTP contract.

This is the "lot of things from OpenSeek": roughly the pool, pairing, TLS,
auth, Telegram, database, logging, and tests — not the decoder, scheduler,
range proxy, library, or playback stack.

## v1 scope (ship this first)


The first release is deliberately small:

- one Python package, one process, one container;
- environment-only configuration: `.env` plus env vars; no setup wizard in v1;
- `POOL_MODE=host` and `POOL_MODE=channel` preserved exactly;
- `host` supports SQLite + local filesystem **or** Telegram;
- `channel` supports SQLite + Telegram, serves no shared pool API, and runs a
  local read-only pool endpoint for its own TVs;
- both modes share the same Nuvio v1 read endpoints and the same index rules;
- legacy `POOL_ENABLED=1` with no `POOL_MODE` still resolves to `host`;
- OSP1 pairing and invite codes;
- the existing Nuvio v1 endpoints, byte-for-byte compatible;
- NVMT package ingest, merge, sparse reads, and episode candidates;
- Telegram MTProto scan/rebuild, librarian relay, and bot upload path;
- CLI for init, serve, invite, status, gc, backup, and doctor;
- tests proving a current Nuvio client works unchanged in both modes.

Not in v1:

- S3/R2 and PostgreSQL;
- Vercel/Cloudflare deployment;
- a web dashboard;
- a Go/Rust rewrite;
- multi-region replication.

Shipping v1 on one small VM already removes Azure and OpenSeek hosting from the
common path. `host` + filesystem needs no Telegram at all. `host` + Telegram and
`channel` are first-class for hosts that want free blob storage, a no-pool-host
deployment, or a migration of the current channel pool.


## Compatibility contract

SeekDB v1 must serve exactly what the current Nuvio client expects. Both
`host` and `channel` expose the same **read** contract to the TV; in `channel`
the API is local to that node and backed by the Telegram index. Only `host`
accepts HTTP uploads.

The client:

- requires a URL and a `sha256//...` SPKI TLS pin;
- pairs through `POST /v1/pair/start` and `POST /v1/pair/claim`;
- sends `Authorization: Bearer <device-token>`;
- downloads `GET /v1/pool/{fp}/nvmt?profile=nvmt-v1-g5|g10`;
- downloads sparse samples with `&slots=...`;
- discovers alternatives through `GET /v1/pool/episodes/{key}?duration_ms=...`;
- uploads through `PUT /v1/pool/{fp}/nvmt?profile=...&duration_ms=...&episode_key=...`;
- caps a package at 16 MiB and a tile at 256 KiB;
- verifies `X-OpenSeek-Duration-Ms` and `X-OpenSeek-Sha256`;
- does not follow HTTP redirects;
- uses a 15 s package read timeout and a 45 s upload timeout.

SeekDB must preserve these details:

- NVMT v1 header: `NVMT`, version `1`, grid `5000` or `10000`, slot count,
  tile count, then records of `slot, pts_ms, length, jpeg`.
- Slot count equals `ceil(duration_ms / grid)`.
- Each tile is a valid JPEG, 125 bytes to 200 KiB.
- First accepted tile for a slot is immutable.
- A package upload may add coverage but may not replace a tile.
- A different duration for the same `(fp, profile)` returns
  `409 media_metadata_conflict` with `X-OpenSeek-Duration-Ms`.
- The response digest is the SHA-256 of the exact response body.

If SeekDB cannot preserve one of these, it is not a drop-in replacement.

## Architecture

```
Nuvio TV ──HTTPS + pin──► SeekDB API
                              │
                    ┌─────────┴─────────┐
                    │                   │
              metadata index       object store
              SQLite/Postgres     FS / S3 / R2 / Telegram
```

Components:

- `api` — FastAPI/Starlette routes for pairing, pool reads/writes, health, admin.
- `protocol` — NVMT encode/decode/validate, caption parse, digest checks.
- `index` — SQLite or PostgreSQL; packages, tiles, episodes, devices, invites.
- `store` — content-addressed blobs; filesystem, Telegram (first-class), S3/R2.
- `pairing` — OSP1 invite and device-token flow.
- `tls` — self-signed P-256 certificate and SPKI pin for v1.
- `cli` — `init`, `serve`, `invite`, `status`, `gc`, `backup`, `pin`.
- `worker` — optional only for Telegram scans and object cleanup; never required
  for a normal read or write.

Mode differences:

- `host` — shared pool API plus the local TV API. Reads and writes are allowed
  according to device role or read/write key.
- `channel` — local TV API only, read-only. There is no shared pool API. Writes
  go to Telegram out-of-band (group post or librarian), and the local index
  picks them up.

### Request path

Read:

1. Validate token or public-read policy.
2. Look up `(fp, profile)` and the requested slots in the index.
3. Read the package object, or only the byte ranges for the requested tiles.
4. Verify the stored SHA-256 and NVMT structure.
5. Return `application/vnd.openseek.nvmt` with duration and digest headers.

Write (`host` only):

1. Validate token, role, quota, and rate limit.
2. Parse and validate the incoming NVMT package.
3. Reject a duration conflict, or merge missing slots with first-writer-wins.
4. Re-encode the merged package.
5. Write the object, then commit the index row.
6. Update the episode index when `episode_key` is supplied.
7. Return `200`/`201`, or `409` with the stored duration.

`channel` has no HTTP write path. Its writes are Telegram messages; the index
scans and validates them, then serves the resulting packages locally.

The order is write-object-then-commit. An orphan object is harmless and is
removed by GC; a committed row without an object is not.

## Deployment profiles

All profiles keep `POOL_MODE=host|channel`. The profile only chooses where blobs
live and whether this instance serves a pool API.

| Profile | `POOL_MODE` | Store | Pool API | Best for |
|---|---|---|---|---|
| A: Host + filesystem | `host` | SQLite + FS | yes | default public pool, one small VM |
| B: Host + Telegram | `host` | SQLite + Telegram | yes | current Option A, free blob storage |
| C: Channel + Telegram | `channel` | SQLite + Telegram | no | Option B, no pool host |
| D: SeekDB Cloud | `host` | PostgreSQL + S3/R2 | yes | serverless later, Nuvio v2 |

### Profile A: Host + filesystem (recommended)

- `POOL_MODE=host`.
- One process.
- SQLite + local filesystem.
- Self-signed certificate with a 20-year pin.
- No Telegram, no S3, no external database.
- Runs on a PC, NAS, Raspberry Pi, VPS, or Oracle Always Free instance.
- Full compatibility with the current Nuvio client.

This is the simplest new deployment. One command:

```sh
docker run -d --name seekdb \
  -p 8787:8787 \
  -v seekdb-data:/data \
  -e POOL_MODE=host \
  -e SEEKDB_STORE=fs \
  -e SEEKDB_PUBLIC_URL=https://203.0.113.10:8787 \
  ghcr.io/<owner>/seekdb:latest
```

The container prints the URL, pin, and an initial invite on first start.

### Profile B: Host + Telegram (current Option A, first-class)

- `POOL_MODE=host`.
- This instance runs the pool API and stores packages in Telegram.
- `PoolService` + `TelegramBotStore` + read/write keys, exactly as today.
- A bot token and a private channel/group are required.
- Clients can be Nuvio TVs or OpenSeek peers with `POOL_URL`.
- The host owns the index and serves everyone; Telegram is the blob store.
- Existing Option A deployments can move to SeekDB without changing their
  mode or variables.

This profile must not regress. It is the compatibility path for every current
pool user.

### Profile C: Channel + Telegram (Option B, first-class)

- `POOL_MODE=channel`.
- No shared pool API. Each node indexes one shared Telegram group/channel
  locally and serves its own TVs through a local read-only endpoint.
- SQLite is a rebuildable materialised view; Telegram is the source of truth.
- A bot token handles uploads. A user session handles history scans and
  downloads, because the Bot API cannot list old posts and `file_id` is
  bot-specific.
- Reads fetch by `message_id`, verify the caption digest, and cache bytes
  locally. A missing message is a cache miss, not a fatal error.
- Contribution has two first-class paths:
  - a user session joins a private supergroup by invite link and posts tiles;
  - a user session DMs the librarian bot, which validates and reposts.
- A bot cannot join by invite link and cannot DM another bot. The docs must say
  this plainly; an admin adds the bot to the group, and bot-to-bot DMs are
  rejected.
- Reuses `telegram_mt.py`, `channel_index.py`, and `contribution.py`.

Choose channel mode when:

- there should be no pool host at all;
- each participant already runs a SeekDB/OpenSeek node;
- the pool is community-run and contributors already use Telegram;
- Telegram should be the durable source of truth.

### Profile D: SeekDB Cloud (future)

- `POOL_MODE=host`.
- Stateless API instances.
- PostgreSQL/Neon for metadata.
- S3/R2/Vercel Blob for packages.
- No local disk, no SQLite, no Telethon.
- Requires the Nuvio v2 client for standard TLS and direct uploads.
- Can run on Vercel, Cloudflare Workers, Fly.io, or any container platform.

This is the profile that makes serverless hosting possible. It is not v1.

### Telegram trade-offs

Telegram is a first-class backend, not a demo or a fallback. It is chosen by
`POOL_MODE=channel` or by `POOL_MODE=host` + `SEEKDB_STORE=telegram`.

Trade-offs:

- Telegram rate limits, `FloodWait`, and scan cost.
- Media can be deleted or become unavailable; local cache and a rebuild handle
  misses.
- A user session is a sensitive credential; it is stored `0600` and never
  logged.
- Telegram is storage, not a pool host. In channel mode SeekDB still runs
  somewhere to serve the TV, even if that somewhere is a small PC or NAS.

The filesystem profile is faster and simpler. The Telegram profiles are the
zero-disk and migration paths. All are first-class and all are tested.



## Why the current Nuvio client blocks serverless SeekDB

**SeekDB can be stateless. The Nuvio client is not.** The client was written for
a self-hosted pool that owns its TLS certificate, accepts one 16 MiB PUT, and
returns the package bytes directly. Vercel and Cloudflare insert themselves
between the client and the service, and that breaks three assumptions.

### What the client actually does today

`src/minipool.c` configures every pool request like this:

```c
p.seguir = 0;          // never follow a redirect
p.tls_pessoal = 1;     // do not validate a public CA for this origin
p.tls_pin = pin;       // authenticate the origin with an SPKI pin
```

`src/rede_pedido.inc` then enforces:

```c
OPT_FOLLOWLOCATION = 0;
if (p->tls_pin) OPT_PINNEDPUBLICKEY = p->tls_pin;
if (p->tls_pin && p->seguir) reject;   // pinning and redirects are mutually exclusive
```

`start_pair()` puts `spki_pin` into the PBKDF2 salt and compares the server's
returned pin with the configured pin. `minipool_enviar()` sends the whole NVMT
package in one `PUT`. `minipool_package_get()` reads the whole package in one
`GET`.

So the current protocol is: **one origin, one pinned certificate, one direct
request, no redirects, up to 16 MiB in each direction.** That works on a box
you control. It does not work through a managed edge.

### Block 1: standard TLS (the pin)

The pin is the SHA-256 of the server certificate's public key, written as
`sha256//...`. It is the trust anchor. SeekDB generates a self-signed P-256
certificate valid for 20 years, so the pin never changes. The TV checks that
exact public key on every request.

On Vercel or Cloudflare, the TLS connection terminates at the platform edge.
The platform presents its own managed certificate, not SeekDB's. You cannot
upload SeekDB's 20-year certificate to Vercel, and the edge certificate rotates
(Let's Encrypt or the platform's CA). Pinning the current edge certificate works
until the next rotation, then every pool request fails the TLS handshake. This
is not a warning that can be ignored; `CURLOPT_PINNEDPUBLICKEY` fails closed.

There is a second, deeper problem: the pin is part of pairing. `spki_pin` is in
the PBKDF2 salt, and the client compares the returned `spki_pin` with the
configured value. If the certificate changes, the pairing proof changes too.
The user cannot just re-pin the TV; the invite proof and the stored device
relationship must be re-established.

A v2 client needs a **standard CA mode**:

- validate the platform certificate against the system trust store;
- use the device token for authentication, not the certificate;
- make `spki_pin` optional in `/v2/pair/start`;
- keep pin mode for self-hosted SeekDB and LAN deployments.

This is a protocol change (`tls_mode=ca|pin`), not a one-line curl option.

### Block 2: presigned direct uploads (the 16 MiB PUT)

Nuvio builds an NVMT package in memory and sends it as one
`PUT /v1/pool/{fp}/nvmt` with a body of up to 16 MiB. On a VM, SeekDB reads the
body and writes it to disk. On Vercel, the function request body limit is around
4.5 MB, so a full package is rejected before SeekDB code runs.

The standard serverless pattern is a **presigned direct upload**:

1. Nuvio asks SeekDB for an upload: `POST /v2/uploads`.
2. SeekDB returns a short-lived signed URL for R2/S3 and an upload id.
3. Nuvio uploads the package **directly to R2/S3**, not through the function.
4. Nuvio calls `POST /v2/uploads/{id}/complete`.
5. SeekDB validates the object's size, SHA-256, and NVMT structure, then indexes
   it.

The API only handles small JSON. The object store handles the 16 MiB body. This
is cheaper, faster, and avoids function timeouts.

Nuvio currently has no upload session, no presign request, and no completion
call. `minipool_enviar()` is a single PUT. That is the gap.

The download side has the same shape: a 16 MiB package should come from R2/S3
through a signed URL or redirect, not through a serverless function.

### Block 3: redirects (the 302 to R2/S3)

Nuvio sets `seguir = 0` and `OPT_FOLLOWLOCATION = 0`. Worse, the request
validator rejects `tls_pin && seguir`: **a pinned request is not allowed to
follow redirects at all.** This was a deliberate security decision, because a
redirect could move to another host without the same pin.

Serverless platforms commonly answer a large download with `302` or `307` to
R2/S3. Nuvio sees a 3xx response, treats it as a failed pool read, and returns a
cache miss. It never fetches the object.

A v2 client must follow redirects safely:

- allow only HTTPS and an explicit host allowlist (the configured object store
  or CDN);
- re-validate TLS on every hop;
- strip `Authorization` on cross-origin hops;
- cap the number of redirects;
- reject a downgrade to HTTP.

Or it can use a presigned download URL directly, without a redirect at all. Either
way, the current "no redirects, ever" rule must change for serverless mode.

### Block 4: state and Telegram

This is not a Nuvio gap, but it belongs in the same explanation.

Vercel Functions have no persistent local disk. The `host` + filesystem
profile's SQLite database and package files cannot live there. SeekDB Cloud must
use PostgreSQL/Neon for metadata and R2/S3 for packages. Pairing sessions,
invites, devices, and the index all become database rows. That is a deployment
change, not a protocol change.

Telegram is worse for serverless: Telethon needs a long-lived event loop, a
session file, and background history scans. Vercel functions start and stop per
request, so Telethon cannot run there. Telegram is first-class in `host` +
Telegram and in `channel` on a VM, NAS, or PC, not on Vercel. A cloud Telegram
design would need a separate worker and Bot API webhooks; that is out of scope
for v1.

### Why this only matters now

The old pool was a self-hosted box. It owned the certificate, accepted 16 MiB,
streamed bytes, kept SQLite, and ran Telethon. The client's assumptions matched
the deployment.

SeekDB removes the CPU, codec, and video stack, which makes a serverless
deployment *architecturally* possible. But the client still assumes a
self-hosted endpoint. That is why "SeekDB is simple" does not automatically mean
"Vercel works". The service is simple; the protocol is still pinned and direct.

### What Nuvio v2 must add

| Area | Current | v2 |
|---|---|---|
| TLS | `tls_pessoal` + SPKI pin only | `tls_mode=ca|pin`; CA validation; pin optional |
| Pairing | `spki_pin` required in the proof | `tls_mode` in start; pin optional; token identity |
| Upload | one PUT, up to 16 MiB | `POST /v2/uploads` + direct PUT + `complete` |
| Download | one GET, up to 16 MiB | signed URL or validated redirect |
| Redirects | disabled; forbidden with a pin | allowlist, max 5, HTTPS only, strip auth cross-origin |
| Discovery | none | `GET /v2/capabilities` |
| Large packages | none | optional resumable/chunked upload |
| Errors | status + JSON | same, plus retry and upload-resume hints |

### What SeekDB v2 must add

- `GET /v2/capabilities` — `tls_modes`, `max_upload`, `upload_mode`,
  `redirect_downloads`, `chunking`.
- `POST /v2/uploads` — create an upload and return a presigned URL.
- `POST /v2/uploads/{id}/complete` — validate and index the uploaded object.
- `GET /v2/packages/{id}` — return a signed download URL or a stream.
- PostgreSQL/Neon metadata and R2/S3 objects.
- Standard TLS deployment on a custom domain.
- Signing keys for presigned URLs, rotated independently of device tokens.
- Contract tests with a real CA certificate, a redirecting CDN, and a 16 MiB
  object.

### Vercel vs Cloudflare

| | Vercel Functions | Cloudflare Workers |
|---|---|---|
| Request body | around 4.5 MB | much larger (100 MB class) |
| Object storage | R2/S3 via presign | native R2 binding |
| Database | Neon/Postgres | D1/Postgres |
| TLS pin | impossible | impossible |
| Redirects | required for large objects | required for large objects |
| Pairing CPU | okay | PBKDF2 600k may exceed free CPU limits |

Cloudflare is the better serverless target because the upload body limit is not
the immediate blocker. It still needs the Nuvio v2 TLS and redirect changes. On
Workers, pairing may need a Durable Object, a paid plan, or a small VM because
OSP1 uses 600,000 PBKDF2 iterations.

### Verdict

- **SeekDB v1 + current Nuvio:** run on a VM, NAS, or PC. It works today, with
  the pin, 16 MiB packages, and Telegram if wanted. This is the recommended
  path until v2 ships.
- **SeekDB Cloud + Nuvio v2:** Vercel or Cloudflare can work after standard CA,
  presigned uploads, and redirect support. Do not promise serverless before
  those exist.

A drop-in "deploy SeekDB to Vercel and paste the URL into Nuvio" is therefore
not possible with the current client. It becomes possible with a v2 client, and
the service-side work is small compared with the client/protocol work.

### Until then

The realistic free public host is a small always-on VM:

- **Oracle Cloud Always Free** — Ampere A1 (up to 4 OCPU / 24 GB) or AMD micro,
  persistent block storage, public IPv4, 10 TB/month egress.
- **Google Cloud always-free e2-micro** — 1 vCPU, 1 GB RAM, 30 GB disk in
  select regions.
- **Home NAS / Raspberry Pi** — with a direct port or a raw TCP tunnel.

A VM is also the only option that preserves the existing pin because SeekDB can
serve its own 20-year self-signed certificate.


## Data model

SeekDB keeps the existing pool tables and migrations. The SQL below is the
logical target; v1 preserves the current names so existing databases keep
working.

| Logical object | `host` | `channel` |
|---|---|---|
| packages | `pool_packages` | assembled from `pool_channel_tiles` |
| tiles | `pool_tiles` | `pool_channel_tiles` |
| episode index | `pool_episode_packages` | derived from packages/tiles |
| upload outbox | `pool_outbox` | `pool_channel_outbox` |
| scan state | n/a | `pool_channel_state` |
| devices/invites | OpenSeek `devices`/`invites` | same |

SQLite for single-node `host` and for `channel`; the same tables map to
PostgreSQL for Cloud.

```sql
CREATE TABLE devices (
  device_id   TEXT PRIMARY KEY,
  token_hash  TEXT NOT NULL,
  name        TEXT NOT NULL,
  role        TEXT NOT NULL DEFAULT 'reader', -- reader | contributor | admin
  created     REAL NOT NULL,
  last_seen   REAL NOT NULL DEFAULT 0,
  revoked     REAL
);

CREATE TABLE invites (
  invite_id   TEXT PRIMARY KEY,
  tag         TEXT NOT NULL,
  secret_hash TEXT NOT NULL,
  role        TEXT NOT NULL DEFAULT 'contributor',
  max_uses    INTEGER NOT NULL DEFAULT 1,
  uses        INTEGER NOT NULL DEFAULT 0,
  expires     REAL NOT NULL,
  created     REAL NOT NULL,
  revoked     REAL
);

CREATE TABLE packages (
  fp          TEXT NOT NULL,
  profile     TEXT NOT NULL,             -- nvmt-v1-g5 | nvmt-v1-g10
  duration_ms INTEGER NOT NULL,
  n_slots     INTEGER NOT NULL,
  n_tiles     INTEGER NOT NULL,
  size        INTEGER NOT NULL,
  sha256      TEXT NOT NULL,
  object_key  TEXT NOT NULL,
  episode_key TEXT,
  uploader    TEXT NOT NULL DEFAULT '',
  created     REAL NOT NULL,
  PRIMARY KEY (fp, profile)
);

CREATE TABLE package_tiles (
  fp       TEXT NOT NULL,
  profile  TEXT NOT NULL,
  k        INTEGER NOT NULL,
  pts_ms   INTEGER NOT NULL,
  offset   INTEGER NOT NULL,
  length   INTEGER NOT NULL,
  sha256   TEXT NOT NULL,
  PRIMARY KEY (fp, profile, k)
);

CREATE TABLE episodes (
  episode_key TEXT NOT NULL,
  fp          TEXT NOT NULL,
  profile     TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
  updated     REAL NOT NULL,
  PRIMARY KEY (episode_key, fp, profile)
);

CREATE TABLE pairing (
  pair_id    TEXT PRIMARY KEY,
  salt       TEXT NOT NULL,
  iterations INTEGER NOT NULL,
  key_hash   TEXT NOT NULL,
  expires    REAL NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  used       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE objects (
  key      TEXT PRIMARY KEY,
  size     INTEGER NOT NULL,
  sha256   TEXT NOT NULL,
  refs     INTEGER NOT NULL DEFAULT 0,
  created  REAL NOT NULL
);

CREATE TABLE rate_limits (
  bucket   TEXT PRIMARY KEY,
  window   REAL NOT NULL,
  count    INTEGER NOT NULL
);

CREATE TABLE audit (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  at       REAL NOT NULL,
  actor    TEXT NOT NULL,
  action   TEXT NOT NULL,
  key      TEXT NOT NULL DEFAULT '',
  result   TEXT NOT NULL
);
```

Notes:

- `package_tiles` indexes byte ranges inside the package object, so sparse
  requests do not need to parse the whole package. On S3, use a ranged GET.
- `objects` makes GC safe and supports content-addressed deduplication.
- No raw tokens, invite secrets, or private keys are stored. Rate-limit buckets
  use a keyed hash of the IP, not the IP itself.
- `audit` stores key prefixes, not full fingerprints, if privacy is a concern.

### First-writer-wins rules

For `(fp, profile)`:

- no existing row: accept, store, index;
- same duration, same digest: duplicate, update episode index, return 200;
- same duration, new slots: merge, keep the existing tile for every slot,
  store a new package object, update `n_tiles`;
- different duration or slot count: reject with
  `409 media_metadata_conflict` and `X-OpenSeek-Duration-Ms: <stored>`;
- corrupt stored object: return 502 and mark the row for GC; never serve it.

For individual tiles: first valid tile for `(fp, profile, k)` wins; a conflicting
digest returns `409 tile_conflict`.

## HTTP API

Both `host` and `channel` serve the public and paired-reader routes. The
contributor routes exist only in `host`. In `channel`, a `PUT` returns
`405 method_not_allowed` (or `404`), because contributions go through Telegram,
not HTTP.

### Public

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/health` | liveness; no host details when public |
| `POST` | `/v1/pair/start` | open a pairing window or resolve an invite tag |
| `POST` | `/v1/pair/claim` | claim with `client_proof` |

### Paired reader

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/pair/status` | device identity and role |
| `DELETE` | `/v1/pair` | revoke this device |
| `GET` | `/v1/pool/{fp}/manifest` | per-tile manifest |
| `GET` | `/v1/pool/{fp}/tiles/{name}` | one tile (compat) |
| `GET` | `/v1/pool/{fp}/nvmt` | full or sparse package |
| `GET` | `/v1/pool/episodes/{key}` | duration-near candidates |

### Contributor

| Method | Path | Purpose |
|---|---|---|
| `PUT` | `/v1/pool/{fp}/nvmt` | upload a package |
| `PUT` | `/v1/pool/{fp}/tiles/{name}` | upload one tile (compat) |

### Admin

Admin routes bind to localhost by default, or require `SEEKDB_ADMIN_TOKEN`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/status` | counts, disk, queue, peers |
| `POST` | `/admin/invites` | create an invite |
| `GET` | `/admin/invites` | list invites |
| `DELETE` | `/admin/invites/{id}` | revoke an invite |
| `GET` | `/admin/devices` | list devices |
| `DELETE` | `/admin/devices/{id}` | revoke a device |
| `POST` | `/admin/gc` | dry-run or collect unreferenced objects |
| `POST` | `/admin/rebuild` | rebuild the index from storage |
| `GET` | `/admin/export` | metadata backup (no secrets) |

### SeekDB v2 (cloud profile)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v2/uploads` | create upload, return presigned URL |
| `POST` | `/v2/uploads/{id}/complete` | validate and index the object |
| `GET` | `/v2/packages/{id}` | signed object URL or stream |
| `GET` | `/v2/episodes/{key}` | candidates |

The v2 API is additive. It does not replace v1 for existing TVs.

## Storage

`SEEKDB_STORE` chooses the blob backend in `host` mode: `fs`, `telegram`, or
(later) `s3`. `channel` mode always uses `telegram`.

### Filesystem

```
$SEEKDB_DATA/
  seekdb.db
  tls/cert.pem
  tls/key.pem
  objects/
    ab/cd/abcdef...nvmt
    ab/cd/abcdef...jpg
```

- Atomic write: temp file, fsync, rename, fsync directory.
- `0600` for the database, certificate key, and env file.
- `0700` for the data directory.
- Content-addressed paths make deduplication and verification trivial.

### S3 / R2

- Key: `packages/{sha256}.nvmt` and `tiles/{sha256}.jpg`.
- Server-side encryption on.
- Bucket is private; signed URLs only.
- Lifecycle rule for incomplete multipart uploads.
- `HEAD`/`GET` with checksum verification.

### Telegram (first-class)

- Used by `host` + Telegram and by every `channel` node.
- One message per package or tile.
- Caption: `openseek:v1:<fp>:<profile>:<k>:<sha256>` for tiles,
  `openseek:nvmt:v1:<fp>:<profile>:<sha256>` for packages.
- MTProto for history and downloads; Bot API for uploads.
- Telegram is cold storage, not the index. The index is always local.
- A missing message is a cache miss and is removed from the local index.
- This backend reuses the existing, tested channel code.

## Pairing and contribution without owner approval

The owner creates an invite, shares one code, and does not approve each user.

- A 16-character invite is `4-char tag + 12-char secret`, entered as
  `ABCD-EFGH-JKLM-NPQR`.
- The tag selects the invite row; the secret is never stored in plaintext.
- The invite carries a role: `reader` or `contributor`.
- Limits: max uses, TTL, optional device count, revocable at any time.
- Pairing uses the existing OSP1 PBKDF2/HMAC proof; the server stores only the
  device token hash.
- A contributor can upload immediately after pairing.
- A reader can download but not upload.
- Public read can be enabled separately for a completely open pool.

In `host` mode with the filesystem store, invites and pairing are the default
contribution path. In `host` + Telegram, the read/write keys and paired devices
both work as they do today. In `channel` mode, the Telegram group and librarian
paths are the contribution path; there is no shared pool API and no pool write
key. Its local TV endpoint is read-only.

Telegram is not a fallback. It is the primary storage/contribution path in
`channel` mode and a first-class option in `host` mode.

### Abuse controls

- Per-device and per-IP token buckets.
- Per-device daily upload bytes and package count.
- Global disk quota with a low-water mark.
- Reject packages above 16 MiB and tiles above 256 KiB.
- Reject non-JPEG payloads, bad NVMT structure, bad SHA-256, and duration
  conflicts before writing.
- Optional allowlist for contributors on a private pool.
- Revoke an invite or device in one command.

## Onboarding (v1: env-only; wizard later)

> v1 is **env-only**: copy `.env.example`, edit it, and run `docker compose --env-file .env up -d`. The wizard steps below are the later convenience layer, not v1.


### v1: env-only quick start

```sh
cp .env.example .env
$EDITOR .env
docker compose --env-file .env up -d
```

Set `POOL_MODE=host`, `SEEKDB_STORE=fs`, `SEEKDB_DATA`, `SEEKDB_PUBLIC_URL`, and the bootstrap invite variables in `.env`. No prompt is required.

### Future wizard: host mode, filesystem store

```sh
seekdb init --mode host --store fs --data /srv/seekdb \
  --public-url https://203.0.113.10:8787
seekdb invite create --role contributor --uses 100 --ttl 30d
seekdb serve
```

First start prints:

```
SeekDB is ready.
MODE:   host
STORE:  fs
URL:    https://203.0.113.10:8787
PIN:    sha256//...
INVITE: ABCD-EFGH-JKLM-NPQR
```

The host pastes URL, pin, and invite into Nuvio. No account and no owner
approval. No Telegram is required.

### Future wizard: host mode, Telegram store

```sh
seekdb init --mode host --store telegram --data /srv/seekdb \
  --public-url https://203.0.113.10:8787
seekdb serve
```

The later wizard asks for the bot token and channel/group ID, and writes the existing
`POOL_READ_KEY` / `POOL_WRITE_KEY` pair. This is the current Option A behaviour,
unchanged.

### Future wizard: channel mode (Option B)

```sh
seekdb init --mode channel --data /srv/seekdb
seekdb telegram login          # user session
seekdb pool scan               # first local index
seekdb serve                   # local TV endpoint only
```

The later wizard asks for:

- Telegram API ID/hash and a session file;
- the shared group/channel ID or invite link;
- whether this node contributes;
- optionally, a librarian bot username.

There is no `POOL_URL` and no pool read/write key. Each node scans the shared
Telegram group/channel and serves its own TVs. `seekdb pool rebuild` recreates
the local index from Telegram.

### Future wizard: join an existing host

```sh
seekdb join https://203.0.113.10:8787 sha256//... ABCD-EFGH-JKLM-NPQR
```

or the same values in the Nuvio pool settings. `join` verifies the pin, pairs,
stores the token, and sends one test package if contribution is enabled. This
is the `host` client path, not `channel`.

### Commands

```sh
seekdb status
seekdb pin
seekdb invite list
seekdb invite revoke <id>
seekdb device list
seekdb device revoke <id>
seekdb pool scan
seekdb pool rebuild
seekdb pool status
seekdb telegram login
seekdb gc --dry-run
seekdb backup /backup/seekdb-$(date +%F)
seekdb restore /backup/seekdb-2026-11-03
seekdb doctor
```

### Environment

The pool variables keep their names. `OPENSEEK_*` spellings are accepted as
aliases.

```sh
# --- app ---
SEEKDB_DATA=/srv/seekdb
SEEKDB_HOST=0.0.0.0
SEEKDB_PORT=8787
SEEKDB_PUBLIC_URL=https://203.0.113.10:8787
SEEKDB_BOOTSTRAP_INVITE=1
SEEKDB_BOOTSTRAP_INVITE_USES=100
SEEKDB_BOOTSTRAP_INVITE_TTL=30d
SEEKDB_MAX_PACKAGE_MB=16
SEEKDB_MAX_STORAGE_GB=200

# --- mode (unchanged) ---
POOL_MODE=host                 # host | channel; unset = local-only
# POOL_ENABLED=1               # legacy: no POOL_MODE -> host
SEEKDB_STORE=fs                # host only: fs | telegram

# --- host server ---
POOL_READ_KEY=read-secret-at-least-24-chars
POOL_WRITE_KEY=write-secret-at-least-24-chars
TELEGRAM_BOT_TOKEN=123456789:ABC...       # host + telegram only
TELEGRAM_CHANNEL_ID=-1001234567890

# --- host client (OpenSeek peer, optional) ---
# POOL_URL=https://pool.example.net
# POOL_SHARE=1

# --- channel mode ---
# TELEGRAM_API_ID=123456
# TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
# TELEGRAM_SESSION_FILE=/srv/seekdb/telegram/me.session
# POOL_CHANNEL_SCAN_LIMIT=2000
# POOL_INDEX_REFRESH_S=300
# CONTRIBUTION=1
# LIBRARIAN=@openseek_pool_bot
# LIBRARIAN_KEY=optional-http-bearer-key
# LIBRARIAN_ENABLED=0

# --- cloud profile (future) ---
# SEEKDB_INDEX_URL=postgresql://...
# SEEKDB_S3_BUCKET=...
# SEEKDB_S3_ENDPOINT=...
```

`POOL_MODE=host` and `POOL_MODE=channel` are the same public modes as today.
There is no user-facing `off`; unset means local-only.


## Repository layout

New standalone repository `seekdb` (working name; verify the package name):

```
seekdb/
  pyproject.toml
  README.md
  .env.example
  LICENSE
  seekdb/
    __init__.py
    config.py
    db.py
    models.py
    protocol/
      __init__.py
      nvmt.py
      caption.py
      validate.py
    store/
      __init__.py
      base.py
      fs.py
      s3.py
      telegram.py
    index/
      __init__.py
      sqlite.py
      postgres.py
    api/
      __init__.py
      app.py
      pairing.py
      pool.py
      admin.py
    pairing.py
    tls.py
    cli.py
    gc.py
  tests/
    fixtures/
    test_protocol.py
    test_pairing.py
    test_pool_fs.py
    test_pool_s3.py
    test_pool_telegram.py
    test_gc.py
    test_nuvio_contract.py
  docker/
    Dockerfile
    compose.yml
  docs/
    INSTALL.md
    HOSTING.md
    PROTOCOL.md
    CONTRIBUTING.md
```

Python 3.12+ and FastAPI first, because the protocol, pairing, and Telegram code
already exist and are tested. A Go/Rust rewrite is possible later if cold start
or binary size becomes important; it is not worth blocking v1.

## Migration from the current code

Copy first, prune second. The default is to take the existing file and remove
what SeekDB does not need, not to reimplement it. Most of the pool code is
already in `openseek-telegram`.

Copy from `openseek-telegram`:

- `pool.py`: `decode_nvmt`, `encode_nvmt`, `validate_tile`, caption regex,
  profile constants, package merge rules.
- `pool.py` `PoolService`, `TelegramBotStore`, `PoolClient`, `PoolSync`:
  `POOL_MODE=host` server and client paths.
- `pool.py` `ChannelPoolClient`, `ChannelPoolSync`: `POOL_MODE=channel` reader
  and publisher.
- `pairing.py`: OSP1 proof, invite handling.
- `tls.py`: self-signed P-256 certificate and SPKI pin.
- `telegram_mt.py`, `channel_index.py`, `contribution.py`: Telegram storage and
  channel mode.
- `db.py`: all pool/device/invite tables, including `pool_tiles`,
  `pool_packages`, `pool_episode_packages`, `pool_outbox`, `pool_channel_tiles`,
  `pool_channel_state`, and `pool_channel_outbox`.
- `config.py`: `pool_mode`, `effective_pool_mode`, `pool_host_enabled`,
  `pool_upload`, Telegram fields, and mode-specific validation.
- `app.py`: pool routes, admin routes, startup/shutdown, auth checks.
- `cli.py`: `pool scan`, `pool rebuild`, `pool status`, `pool login`.
- `tests/test_pool.py`, `tests/test_pool_channel.py`: host and channel
  regression tests.

Drop from SeekDB (media only):

- `engine.py`, `worker.py`, `fetch.py`, `scheduler.py` except `tile_count()`,
  `vtt.py`, `guard.py`, `library.py`, `jobs.py`, `timeline.py`,
  `altpipeline.py`, `altsource.py`, `mediasniff.py`, `netpolicy.py`, `public.py`.
- `av`, `Pillow`, `numpy`, and the video cache.

Keep the pool configuration:

- `POOL_MODE=host|channel`, legacy `POOL_ENABLED=1`, `POOL_URL`,
  `POOL_READ_KEY`, `POOL_WRITE_KEY`, `POOL_SHARE`, `POOL_TIMEOUT_S`.
- Telegram and channel variables, `CONTRIBUTION`, `LIBRARIAN*`.
- `OPENSEEK_*` aliases.

OpenSeek migration:

1. Keep `PoolClient` in OpenSeek and point it at a SeekDB URL. OpenSeek then
   reads/writes a shared pool without owning it.
2. Move `PoolService`, `TelegramBotStore`, `ChannelIndex`, and the pool routes
   to SeekDB. OpenSeek may keep a thin `/v1/pool/*` proxy for one release.
3. Do not remove `POOL_MODE=host|channel` from SeekDB. Host mode is Option A;
   channel mode is Option B.
4. Nuvio does not need OpenSeek at all: it already generates tiles and can talk
   directly to SeekDB.


## Tests

Protocol:

- Golden NVMT fixtures from Nuvio's `nvmt_ok` and OpenSeek's `decode_nvmt`.
- Every boundary: 20-byte header, max 16 MiB, 8640 slots, 200 KiB tile,
  duplicate slots, out-of-range slots, bad JPEG markers, bad PTS.
- Caption regex and digest checks.
- Sparse slot selection and nearest-slot fallback.

Pairing:

- OSP1 vectors from `docs/api/CONTRACT-v1.md`.
- Wrong code, expired invite, max uses, revoked invite, revoked device.
- Token hash only; no plaintext token in the database.

Pool and modes:

- `POOL_MODE=host` + filesystem: two instances sharing a directory, upload,
  download, first-writer-wins, rebuild.
- `POOL_MODE=host` + Telegram: the existing fake Bot API tests.
- `POOL_MODE=channel`: two independent SQLite DBs index one fake Telegram
  channel, both serve the same package through their local read-only endpoint.
- `POOL_MODE=channel`: no shared pool API, no `PUT` route, and no pool keys.
- `POOL_MODE=channel`: delete `seekdb.db`, rebuild from the channel, serve
  again.
- No `POOL_MODE` + `POOL_ENABLED=1` resolves to `host`.
- No `POOL_MODE` and no `POOL_ENABLED` starts no store and no scan thread.
- Invalid `POOL_MODE` is rejected at startup.
- A and B upload different bytes for the same slot; first message wins.
- Different duration returns 409 with the stored duration.
- Corrupt object is not served and is collected.
- Restart preserves data.

HTTP contract:

- Run SeekDB and exercise every Nuvio request with `curl`.
- A small C harness or the existing Nuvio client against a local instance.
- Check status codes, headers, body digest, and timeout behavior.

Cloud:

- Memory store + SQLite for unit tests.
- Postgres + MinIO for integration tests.
- S3 range reads for sparse slots.
- Presigned upload happy path and expired URL.

Load:

- 100 concurrent package reads of 10 MB.
- One 16 MiB upload while reads are in flight.
- Disk full, object store 503, and SQLite busy.

## Phases

| Phase | Work | Output |
|---|---|---|
| 0 | Freeze the protocol; copy mode resolution and shared code; decide repo name | `seekdb/protocol`, `pool_mode`, golden tests |
| 1 | `POOL_MODE=host` + filesystem: config, SQLite, FS store, pairing, v1 API | runnable `seekdb serve` |
| 2 | `POOL_MODE=host` + Telegram, and `POOL_MODE=channel`: stores, MTProto, index, librarian | both modes feature-complete |
| 3 | CLI, invite/device admin, Docker, install docs | one-command host |
| 4 | Quotas, GC, backup/restore, audit, doctor | safe public pool |
| 5 | S3/R2 + PostgreSQL profile | stateless SeekDB Cloud |
| 6 | Nuvio v2 client: standard CA, presigned upload, redirects | serverless-compatible client |
| 7 | Vercel and Cloudflare deployment examples | hosted pool |
| 8 | Migrate OpenSeek off pool hosting; keep `POOL_MODE` compatible | one pool implementation |
| 9 | Optional setup wizard that only writes `.env` | later convenience |

Rough effort: phases 1–3 are the core and are small enough for one developer;
phase 5 is the only part that touches the TV client and should be gated on a
protocol version bump.

## Acceptance criteria

- `POOL_MODE=host` + filesystem starts with no OpenSeek, no Telegram, and no
  external database.
- `POOL_MODE=host` + Telegram behaves exactly like current Option A, including
  read/write keys and the existing env variables.
- `POOL_MODE=channel` runs no shared pool API, indexes one shared Telegram
  group/channel locally, and serves its own TVs through a local read-only
  endpoint.
- No `POOL_MODE` + `POOL_ENABLED=1` still resolves to `host`.
- No `POOL_MODE` and no `POOL_ENABLED` stays local-only.
- A current Nuvio TV pairs with URL + pin + invite and downloads a package in
  both modes.
- A contributor uploads a package and another instance reads it.
- First-writer-wins and duration conflicts behave exactly like the current pool.
- Restart, backup, and restore preserve all packages and devices.
- A corrupt object is rejected, never served, and collected by GC.
- `host` + filesystem runs on a 1 GB Oracle/GCP free instance with a 200 GB
  disk.
- `channel` rebuilds its index from Telegram after deleting `seekdb.db`.
- SeekDB Cloud passes the same contract with a v2 client on Vercel or
  Cloudflare.
- v1 starts from `.env` alone; no interactive setup step is required.
- No token, invite secret, session, or private key appears in logs or API
  responses.
- OpenSeek can run without any pool code and still generate thumbnails.

## Capacity and cost

Approximate package sizes:

- g10 package: 720 tiles for a 2-hour title, about 5–10 MB.
- g5 package: 1440 tiles, about 10–16 MB.
- 1,000 titles: roughly 5–16 GB.
- 10,000 titles: roughly 50–160 GB.

A free Oracle instance with 200 GB block storage can hold tens of thousands of
packages if the cache is pruned. A public pool should set
`SEEKDB_MAX_STORAGE_GB`, a per-device quota, and a GC low-water mark.

Telegram can store blobs for free, but it adds rate limits, MTProto, and scan
complexity. It is a first-class backend in `host` (with `SEEKDB_STORE=telegram`)
and the only backend in `channel`; it is not a second-class extra.

## Risks

| Risk | Mitigation |
|---|---|
| TLS pin blocks serverless | Keep v1 on a VM; ship v2 standard-CA support before promising Vercel |
| 16 MiB body exceeds platform limits | Presigned direct uploads; chunking in v2; do not proxy through the function |
| No redirects in v1 | Serve bytes directly on v1; add redirect support in v2 |
| Public pool abuse | Invite roles, quotas, rate limits, revocable devices, disk cap |
| Storage cost | Content-addressed dedup, GC, per-device quotas, first-class Telegram |
| SQLite writer contention | WAL, one process, short transactions; Postgres for multi-node |
| Object/DB inconsistency | Write object first, commit second; GC orphan objects |
| Protocol drift | Golden vectors and a contract test against the Nuvio client |
| Telegram rate limits | Bounded scans, backoff, cache; never block reads |
| Legal/takedown | Private pools, audit log, delete-by-fingerprint command |

## Open questions

1. **Repository name:** `seekdb` may already exist on PyPI/GitHub. Check before
   publishing; fall back to `seekdb-pool` or `openseek-seekdb`.
2. **Language:** Python for reuse, or Go for a single binary. Recommendation:
   Python v1, revisit after the protocol is stable.
3. **Default public read:** recommendation is off. A pool should require pairing
   unless the owner explicitly enables public reads.
4. **Package vs tile storage:** v1 stores package objects plus a byte-range
   index. Tile-level dedup can come later if storage becomes the bottleneck.
5. **Telegram:** first-class in both `host` (as the store) and `channel` (the
   only store). It is not a fallback and not a replacement for `POOL_MODE`.
6. **Vercel first or Cloudflare first:** Cloudflare Workers + R2 + D1 is the
   better serverless target. Vercel is worth supporting because users ask for
   it, but it needs the v2 client first.
7. **OpenSeek deprecation:** remove pool hosting after one release with
   `PoolClient` pointed at SeekDB. Do not maintain two pool implementations.
