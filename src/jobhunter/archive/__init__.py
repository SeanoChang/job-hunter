from pathlib import Path
from urllib.parse import urlparse

from jobhunter.archive.base import ArchiveError, ArchiveStore
from jobhunter.archive.local import LocalFS

__all__ = ["ArchiveError", "ArchiveStore", "LocalFS", "open_store"]


def open_store(url: str) -> ArchiveStore:
    u = urlparse(url)
    if u.scheme == "file":
        return LocalFS(Path(u.path))
    if u.scheme == "s3":
        from jobhunter.archive.s3 import S3Compatible

        return S3Compatible(bucket=u.netloc, prefix=u.path.strip("/"))
    raise ValueError(f"unsupported archive url scheme: {u.scheme!r}")
