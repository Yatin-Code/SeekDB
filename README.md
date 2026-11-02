# SeekDB

SeekDB is a standalone service for validating, storing, indexing, and serving
pre-rendered NVMT thumbnail packages. It is designed to speak the existing
Nuvio v1 pool protocol and to keep thumbnail generation outside the service.

The implementation carries over pool, pairing, TLS, and Telegram code from
[OpenSeek](https://github.com/Yatin-Code/openseek-telegram). SeekDB is licensed
under AGPL-3.0-or-later to preserve the license of that reused code.

## Status

The v1 API, filesystem and Telegram host stores, Telegram channel indexing,
OSP1 invite pairing, CLI, and Docker setup are implemented. Focused SeekDB and
Nuvio pool tests are available. Cloud storage and the Nuvio v2 client remain
later work. The implementation notes and remaining plan are in
[`docs/PLAN-SEEKDB.md`](docs/PLAN-SEEKDB.md).

## Development

Requires Python 3.12 or newer.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
cp .env.example .env
python -m pytest -q
```

See `.env.example` and the plan for configuration and deployment details.
The CLI offers `seekdb init`, `seekdb serve`, `seekdb invite`, `seekdb status`,
`seekdb gc`, `seekdb backup`, and `seekdb doctor`. Channel mode also provides
`seekdb telegram login` and `seekdb pool scan|rebuild|status`.
