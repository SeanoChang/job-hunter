"""Source protocol and the normalisers shared by all adapters. Adapters do no I/O."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol

from jobhunter.models import Board, PostingVersion, RawRecord


class EnvelopeError(Exception):
    """The response body is not the shape this source promises."""


class NormalizeError(Exception):
    """One record could not be normalised; the others are unaffected."""


class Source(Protocol):
    name: str
    adapter_version: str

    def url(self, board: Board) -> str: ...
    def parse(self, body: bytes) -> Iterator[RawRecord]: ...
    def normalize(self, rec: RawRecord, board: Board) -> PostingVersion: ...


# -- two-phase sources (spec 2026-09-04 §3.2): a list phase that returns rows
# without descriptions, then one detail request per row. The adapter only
# *describes* the requests; `fetch.py` issues them, so adapters stay pure.

_EMPTY_PAYLOAD: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """One HTTP request, described. `json_body` is sent as a JSON body (Workday CXS posts)."""

    url: str
    method: str = "GET"
    json_body: Any | None = None


@dataclass(frozen=True, slots=True)
class ListRow:
    """One row of a list page: the uid plus the coarse fields the list carries.

    `uid` is the source's own identifier for the posting (the `source_id` the
    detail normalises to), stable across runs — the detail budget keys on it.
    `payload` is the row verbatim, so an adapter can normalise from the list
    alone when a source's list is already complete.
    """

    uid: str
    detail_path: str | None = None
    title: str | None = None
    locations: tuple[str, ...] = ()
    posted_at: datetime | None = None
    payload: Mapping[str, Any] = field(default=_EMPTY_PAYLOAD)


@dataclass(frozen=True, slots=True)
class ListPage:
    """One parsed list page: its rows, and the board's total as the source reports it."""

    rows: tuple[ListRow, ...]
    total: int


class TwoPhaseSource(Protocol):
    name: str
    adapter_version: str

    def list_url(self, board: Board, offset: int) -> RequestSpec: ...
    def parse_list(self, body: bytes) -> ListPage: ...
    def detail_url(self, board: Board, row: ListRow) -> RequestSpec: ...
    def normalize_detail(self, body: bytes, row: ListRow, board: Board) -> PostingVersion: ...


def load_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise EnvelopeError(f"body is not JSON: {e}") from e


def record_id(item: Any) -> str | None:
    if isinstance(item, dict) and item.get("id") is not None:
        return str(item["id"])
    return None


def as_payload(item: Any) -> dict[str, Any]:
    return item if isinstance(item, dict) else {"value": item}


def opt_str(v: Any) -> str | None:
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return None


def req_str(payload: dict[str, Any], key: str) -> str:
    s = opt_str(payload.get(key))
    if s is None:
        raise NormalizeError(f"missing or empty {key!r}")
    return s


def norm_locations(values: Iterable[Any]) -> tuple[str, ...]:
    out = {s for v in values if (s := opt_str(v)) is not None}
    return tuple(sorted(out))


def norm_workplace(v: Any) -> str | None:
    s = opt_str(v)
    return s.lower() if s else None


_EMPLOYMENT_MAP = {"fulltime": "full_time", "parttime": "part_time"}


def norm_employment(v: Any) -> str | None:
    s = opt_str(v)
    if s is None:
        return None
    key = re.sub(r"[\s-]+", "", s).lower()
    if key in _EMPLOYMENT_MAP:
        return _EMPLOYMENT_MAP[key]
    return re.sub(r"[\s-]+", "_", s.strip()).lower()
