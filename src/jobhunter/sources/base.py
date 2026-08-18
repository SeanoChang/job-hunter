"""Source protocol and the normalisers shared by all adapters. Adapters do no I/O."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
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
