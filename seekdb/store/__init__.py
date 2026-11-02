"""Blob stores for immutable thumbnail objects."""

from .base import ObjectStore
from .fs import FileSystemObjectStore

__all__ = ["ObjectStore", "FileSystemObjectStore"]
