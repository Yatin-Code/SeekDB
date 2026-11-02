# Contributing

SeekDB is licensed under AGPL-3.0-or-later. Contributions that include code
derived from OpenSeek must retain its license and notices.

The project is in active v1 development. Keep the pool protocol compatible with
the current Nuvio client, keep media decoding out of SeekDB, and preserve the
first-writer-wins and duration-conflict rules. Configuration should come from
environment variables and `.env`; v1 has no interactive setup wizard.

Python 3.12 or newer is required. The package is formatted as a standard Python
project and keeps implementation modules under `seekdb/`. New store and index
backends should implement the interfaces in `seekdb/store/base.py` and the
index package rather than coupling application routes to a specific database.
