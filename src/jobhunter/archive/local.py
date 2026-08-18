"""file:// backend for tests and offline runs."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path


class LocalFS:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        return self._root / key

    def put(self, key: str, data: bytes) -> bool:
        p = self._path(key)
        if p.exists():
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        return True

    def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.exists():
            raise KeyError(key)
        return p.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list(self, prefix: str, start_after: str | None = None) -> Iterator[str]:
        base = self._path(prefix)
        if not base.exists():
            return
        keys = sorted(
            str(p.relative_to(self._root)).replace(os.sep, "/")
            for p in base.rglob("*")
            if p.is_file() and not p.name.endswith(".tmp")
        )
        for k in keys:
            if start_after is None or k > start_after:
                yield k
