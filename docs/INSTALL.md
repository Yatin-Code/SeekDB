# Installation

SeekDB v1 supports `POOL_MODE=host` with either the local filesystem store or
the Telegram Bot API store, and `POOL_MODE=channel` with a Telegram MTProto
history index. The service creates an SQLite metadata index and a self-signed
P-256 TLS certificate in `SEEKDB_DATA`.

## Docker Compose

```sh
cp .env.example .env
# Set SEEKDB_PUBLIC_URL to the externally reachable HTTPS origin.
docker compose --env-file .env -f docker/compose.yml up -d --build
docker compose --env-file .env -f docker/compose.yml logs -f seekdb
```

On the first host-mode start, the process prints one contributor invite and
the TLS SPKI pin. Channel mode prints a reader invite. Save both. The invite
secret is only printed once; the service keeps its derived key in SQLite. The admin token is generated in the data
directory when `SEEKDB_ADMIN_TOKEN` is unset, and local admin requests are
limited to loopback.

The current v1 client uses the self-signed certificate pin. Keep the data
volume when upgrading so the certificate and pin stay stable.

## Python

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
seekdb init
seekdb serve
```

For direct Python use, `seekdb serve` reads `.env` from the current directory.
The API listens on `SEEKDB_HOST:SEEKDB_PORT` and uses the generated certificate.

## Local administration

The CLI manages invites without exposing their secret through an API response:

```sh
seekdb invite create --role contributor --uses 100 --ttl-s 2592000
seekdb invite list
seekdb status
seekdb pin
seekdb gc
seekdb gc --execute
seekdb backup /path/to/backup
```

`seekdb gc` is a dry run unless `--execute` is supplied. Backups include the
database, object files, and TLS identity. Treat the backup as sensitive.

## Modes

`POOL_MODE=host` enables the shared pool API. Choose `SEEKDB_STORE=fs` or
`SEEKDB_STORE=telegram`. Telegram host mode needs a bot token and channel ID.
`POOL_MODE=channel` uses a user MTProto session to scan a shared Telegram
channel and serves a local read-only pool API. Set `TELEGRAM_API_ID`,
`TELEGRAM_API_HASH`, `TELEGRAM_SESSION_FILE`, and `TELEGRAM_CHANNEL_ID` for
that mode. Unset `POOL_MODE` keeps pool routes and Telegram scanning disabled.
`POOL_ENABLED=1` remains a legacy alias for host mode when `POOL_MODE` is
unset.
