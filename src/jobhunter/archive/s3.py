"""Placeholder for the s3:// backend so ``open_store`` resolves; Task 9 implements it."""

from __future__ import annotations

from collections.abc import Iterator


class S3Compatible:
    def __init__(self, bucket: str, prefix: str = "") -> None:
        self._bucket = bucket
        self._prefix = prefix
        raise NotImplementedError("S3Compatible is not implemented yet")

    def put(self, key: str, data: bytes) -> bool:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def list(self, prefix: str, start_after: str | None = None) -> Iterator[str]:
        raise NotImplementedError
