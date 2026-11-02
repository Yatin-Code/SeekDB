# Hosting profiles

SeekDB v1 has three single-node profiles. All use SQLite; only blob storage and
the pool API behavior vary.

| Profile | Environment | Storage | Pool API |
|---|---|---|---|
| Host + filesystem | `POOL_MODE=host`, `SEEKDB_STORE=fs` | Local content-addressed files | Read and write |
| Host + Telegram | `POOL_MODE=host`, `SEEKDB_STORE=telegram` | Telegram Bot API documents | Read and write |
| Channel + Telegram | `POOL_MODE=channel` | Shared Telegram channel plus local cache | Local read-only |

Host mode can also use `POOL_URL` to read through to another pool. Set
`POOL_SHARE=1` and a `POOL_WRITE_KEY` to enable a durable upstream outbox for
local package and tile uploads. The upstream URL must use HTTPS except for a
loopback development address; standard certificate validation is used.

## Host + filesystem

This is the default profile. It needs no Telegram account or external database.
Mount `SEEKDB_DATA` on persistent storage and keep it backed up. The generated
certificate and its pin live beside the database and objects.

## Host + Telegram

The Bot API stores immutable package and tile documents. Set
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHANNEL_ID`; the bot must be able to post to
the channel. SQLite records the Bot API file IDs and content hashes. Keep the
SQLite data directory: Telegram file IDs are bot-specific and are not a
rebuildable index.

## Channel + Telegram

Each SeekDB node scans the same channel through a user MTProto session, stores
a local SQLite index, and caches validated JPEG bytes. Set
`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_FILE`, and
`TELEGRAM_CHANNEL_ID`, then run `seekdb telegram login`. Protect the session
file like a password; it is written with owner-only permissions and is never
logged.

The channel endpoint is read-only. Contributions are Telegram documents with
the existing `openseek:v1` caption. For user submissions, configure the
librarian options and add the librarian bot to the channel. A bot cannot join a
private invite link or DM another bot; an administrator must add it to the
group. Without a librarian, contributors post through their own authorized
Telegram user session.

Run `seekdb pool scan`, `seekdb pool rebuild`, or `seekdb pool status` to manage
the local index. Removing `seekdb.db` loses only the materialized index; a
rebuild scans the channel again. Cached files can be removed and downloaded
again.

## TLS, pairing, and backups

The v1 server creates a self-signed P-256 certificate and prints its stable
SPKI pin. Nuvio pins that key. Keep the data volume when moving or upgrading a
host. Create and revoke device invites with `seekdb invite`; the CLI displays a
new invite code once, and SQLite keeps only the derived OSP1 verifier.

`SEEKDB_MAX_STORAGE_GB` caps local object storage. Contributor devices also
have configurable daily byte and package limits. Backups include SQLite,
objects, and TLS identity, so store them securely. `seekdb gc` reports
unreferenced local or Telegram objects by default; add `--execute` to remove
them.
