"""Archive key layout (spec §5.2). Only place that knows the paths."""

from __future__ import annotations

from datetime import datetime

from jobhunter.timeutil import iso

ATTEMPTS_PREFIX = "attempts/"


def blob_key(sha256: str) -> str:
    return f"blobs/sha256/{sha256[:2]}/{sha256}.gz"


def attempt_key(source: str, board: str, started_at: datetime) -> str:
    ts = iso(started_at)  # 2026-08-18T06:01:02Z
    y, m, rest = ts[0:4], ts[5:7], ts[8:]
    d, hms = rest[0:2], rest[3:11].replace(":", "")
    return f"{ATTEMPTS_PREFIX}{source}/{board}/{y}/{m}/{d}T{hms}Z.json"


def attempts_prefix(source: str | None = None, board: str | None = None) -> str:
    p = ATTEMPTS_PREFIX
    if source:
        p += f"{source}/"
        if board:
            p += f"{board}/"
    return p


def registry_key(revision: str) -> str:
    return f"registry/{revision}.json"


def version_key(version_hash: str) -> str:
    return f"versions/{version_hash[:2]}/{version_hash}.html.gz"
