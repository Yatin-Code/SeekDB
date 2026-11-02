"""Compatibility import for the SQLite-backed metadata Index."""

from seekdb.index.sqlite import DB, SQLiteIndex

__all__ = ["DB", "SQLiteIndex"]
