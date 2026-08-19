"""file:// backend for tests and offline runs. Safe for concurrent writers of the same key."""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

from jobhunter.archive.base import ArchiveError


class LocalFS:
    def __init__(self, root: Path) -> None:
        if root.exists() and not root.is_dir():
            raise ArchiveError(f"archive root is not a directory: {root}")
        self._root = root

    def _path(self, key: str) -> Path:
        return self._root / key

    def put(self, key: str, data: bytes) -> bool:
        p = self._path(key)
        try:
            if p.exists():
                return False
            p.parent.mkdir(parents=True, exist_ok=True)
            # Unique temp name per writer: two threads storing the same content-addressed
            # blob must never share a temp file (the loser's os.replace would fail).
            tmp = p.with_name(
                f"{p.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
            )
            tmp.write_bytes(data)
            os.replace(tmp, p)  # atomic; identical content if two writers race on one key
        except OSError as e:
            raise ArchiveError(f"put {key}: {e}") from e
        return True

    def get(self, key: str) -> bytes:
        p = self._path(key)
        try:
            return p.read_bytes()
        except FileNotFoundError as e:
            raise KeyError(key) from e
        except OSError as e:
            raise ArchiveError(f"get {key}: {e}") from e

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list(self, prefix: str, start_after: str | None = None) -> Iterator[str]:
        if self._root.exists() and not self._root.is_dir():
            raise ArchiveError(f"archive root is not a directory: {self._root}")
        base = self._path(prefix)
        if not base.exists():
            return
        try:
            keys = sorted(
                str(p.relative_to(self._root)).replace(os.sep, "/")
                for p in base.rglob("*")
                if p.is_file() and not p.name.endswith(".tmp")
            )
        except OSError as e:
            raise ArchiveError(f"list {prefix}: {e}") from e
        for k in keys:
            if start_after is None or k > start_after:
                yield k
