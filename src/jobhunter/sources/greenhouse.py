"""Greenhouse Job Board API adapter. Payload analysis: docs/sources/greenhouse.md."""

from __future__ import annotations

import html
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from jobhunter.models import Board, PostingVersion, RawRecord
from jobhunter.sources.base import (
    EnvelopeError,
    NormalizeError,
    as_payload,
    load_json,
    norm_locations,
    opt_str,
    record_id,
    req_str,
)
from jobhunter.timeutil import parse_iso


class Greenhouse:
    name = "greenhouse"
    adapter_version = "greenhouse/1"

    def url(self, board: Board) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{board.board}/jobs?content=true"

    def parse(self, body: bytes) -> Iterator[RawRecord]:
        obj = load_json(body)
        if not isinstance(obj, dict) or not isinstance(obj.get("jobs"), list):
            raise EnvelopeError("greenhouse: expected {jobs: [...]}")
        for i, item in enumerate(obj["jobs"]):
            yield RawRecord(record_id(item), i, as_payload(item))

    def normalize(self, rec: RawRecord, board: Board) -> PostingVersion:
        p: dict[str, Any] = rec.payload
        if rec.source_id is None:
            raise NormalizeError("record has no id")
        title = req_str(p, "title")
        content = p.get("content")
        if not isinstance(content, str):
            raise NormalizeError("missing content")
        loc = p.get("location")
        locations = [loc.get("name") if isinstance(loc, dict) else None]
        for office in p.get("offices") or []:
            if isinstance(office, dict):
                locations.append(office.get("location"))
        depts = p.get("departments") or []
        department = opt_str(depts[0].get("name")) if depts and isinstance(depts[0], dict) else None
        return PostingVersion(
            source=self.name,
            board=board.board,
            source_id=rec.source_id,
            title=title,
            company=opt_str(p.get("company_name")) or board.company,
            locations=norm_locations(locations),
            workplace_type=None,
            is_remote=None,
            department=department,
            team=None,
            employment_type=None,
            compensation=None,
            url=opt_str(p.get("absolute_url")),
            apply_url=None,
            source_created_at=_dt(p.get("first_published")),
            source_updated_at=_dt(p.get("updated_at")),
            description_html=html.unescape(content),
        )


def _dt(v: Any) -> datetime | None:
    s = opt_str(v)
    if s is None:
        return None
    try:
        return parse_iso(s)
    except ValueError as e:
        raise NormalizeError(f"bad timestamp {s!r}") from e
