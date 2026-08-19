"""Content-addressed, write-once key/value archive."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol


class ArchiveError(Exception):
    """The archive backend is unreachable or misconfigured."""


class ArchiveStore(Protocol):
    def put(self, key: str, data: bytes) -> bool:
        """Write iff absent. Returns False (and writes nothing) if the key already exists."""
        ...

    def get(self, key: str) -> bytes:
        """Return the object; raise KeyError if absent."""
        ...

    def exists(self, key: str) -> bool: ...

    def list(self, prefix: str, start_after: str | None = None) -> Iterator[str]:
        """Yield keys under prefix in sorted order, strictly after start_after."""
        ...
