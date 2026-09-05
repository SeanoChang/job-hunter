"""Frozen data types shared by every module. No I/O, no logic beyond validation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from types import MappingProxyType
from typing import Any

from jobhunter.hashing import canonical_json
from jobhunter.timeutil import iso, parse_iso

SOURCE_PREFIX: dict[str, str] = {"greenhouse": "gh", "lever": "lv", "ashby": "ab", "workday": "wd"}

_EMPTY_EXTRA: MappingProxyType[str, str] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Board:
    company: str
    source: str
    board: str
    country: str | None = None
    tags: tuple[str, ...] = ()
    extra: MappingProxyType[str, str] = _EMPTY_EXTRA

    @property
    def key(self) -> str:
        return f"{self.source}:{self.board}"

    def __hash__(self) -> int:
        # MappingProxyType isn't hashable, so hash the sorted items instead of
        # `extra` itself; frozen dataclasses only auto-generate __hash__ when
        # the class doesn't already define one, so this replaces that default.
        return hash(
            (
                self.company,
                self.source,
                self.board,
                self.country,
                self.tags,
                tuple(sorted(self.extra.items())),
            )
        )


@dataclass(frozen=True, slots=True)
class RawRecord:
    source_id: str | None
    index: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Compensation:
    min: float | None
    max: float | None
    currency: str | None
    interval: str | None


@dataclass(frozen=True, slots=True)
class PostingVersion:
    source: str
    board: str
    source_id: str
    title: str
    company: str
    locations: tuple[str, ...]
    workplace_type: str | None
    is_remote: bool | None
    department: str | None
    team: str | None
    employment_type: str | None
    compensation: Compensation | None
    url: str | None
    apply_url: str | None
    source_created_at: datetime | None
    source_updated_at: datetime | None
    description_html: str

    @property
    def uid(self) -> str:
        return f"{SOURCE_PREFIX[self.source]}:{self.board}:{self.source_id}"


@dataclass(frozen=True, slots=True)
class FetchResult:
    status: int | None
    body: bytes
    elapsed: float
    transport: str  # ok | timeout | dns | tls | connect | http_error | too_large | other
    error: str | None


_MANIFEST_DT_FIELDS = ("started_at", "finished_at")
# Fields added after the initial manifest shape: optional, and OMITTED from the
# serialized form entirely when None so every manifest written before they existed
# still parses byte-for-byte the same (spec §3.3).
_MANIFEST_OPTIONAL_FIELDS = ("page_blobs", "details")


@dataclass(frozen=True, slots=True)
class DetailAttempt:
    """One detail fetch for a two-phase (list + detail) board, this attempt.

    A failed detail fetch has `blob_sha256` None and `error` set.
    """

    uid: str
    blob_sha256: str | None
    http_status: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class AttemptManifest:
    attempt_id: str
    run_id: str
    source: str
    board: str
    started_at: datetime
    finished_at: datetime
    url: str
    http_status: int | None
    transport: str
    blob_sha256: str | None
    payload_bytes: int
    record_count: int | None
    adapter_version: str
    registry_revision: str
    cli_version: str
    error: str | None
    # Two-phase boards only (spec §3.3); one manifest per board per run either way.
    page_blobs: tuple[str, ...] | None = None
    details: tuple[DetailAttempt, ...] | None = None

    def to_json(self) -> bytes:
        d: dict[str, Any] = {}
        for f in fields(self):
            if f.name in _MANIFEST_OPTIONAL_FIELDS:
                continue
            v = getattr(self, f.name)
            d[f.name] = iso(v) if f.name in _MANIFEST_DT_FIELDS else v
        if self.page_blobs is not None:
            d["page_blobs"] = list(self.page_blobs)
        if self.details is not None:
            d["details"] = [asdict(da) for da in self.details]
        return canonical_json(d)

    @classmethod
    def from_json(cls, data: bytes) -> AttemptManifest:
        d = json.loads(data.decode("utf-8"))
        for name in _MANIFEST_DT_FIELDS:
            d[name] = parse_iso(d[name])
        if d.get("page_blobs") is not None:
            d["page_blobs"] = tuple(d["page_blobs"])
        if d.get("details") is not None:
            d["details"] = tuple(DetailAttempt(**item) for item in d["details"])
        return cls(**d)
