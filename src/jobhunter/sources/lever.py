"""Lever Postings API adapter. Payload analysis: docs/sources/lever.md."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from jobhunter.models import Board, Compensation, PostingVersion, RawRecord
from jobhunter.sources.base import (
    EnvelopeError,
    NormalizeError,
    as_payload,
    load_json,
    norm_employment,
    norm_locations,
    norm_workplace,
    opt_str,
    record_id,
    req_str,
)
from jobhunter.timeutil import from_epoch_ms


class Lever:
    name = "lever"
    adapter_version = "lever/1"

    def url(self, board: Board) -> str:
        return f"https://api.lever.co/v0/postings/{board.board}?mode=json"

    def parse(self, body: bytes) -> Iterator[RawRecord]:
        obj = load_json(body)
        if not isinstance(obj, list):
            raise EnvelopeError("lever: expected a bare JSON array")
        for i, item in enumerate(obj):
            yield RawRecord(record_id(item), i, as_payload(item))

    def normalize(self, rec: RawRecord, board: Board) -> PostingVersion:
        p: dict[str, Any] = rec.payload
        if rec.source_id is None:
            raise NormalizeError("record has no id")
        title = req_str(p, "text")
        raw_cats = p.get("categories")
        cats: dict[str, Any] = raw_cats if isinstance(raw_cats, dict) else {}
        all_locations = cats.get("allLocations") or []
        locations = norm_locations(all_locations) or norm_locations([cats.get("location")])
        workplace = norm_workplace(p.get("workplaceType"))
        created = p.get("createdAt")
        return PostingVersion(
            source=self.name,
            board=board.board,
            source_id=rec.source_id,
            title=title,
            company=board.company,
            locations=locations,
            workplace_type=workplace,
            is_remote=(workplace == "remote") if workplace else None,
            department=opt_str(cats.get("team")),
            team=None,
            employment_type=norm_employment(cats.get("commitment")),
            compensation=_salary(p.get("salaryRange")),
            url=opt_str(p.get("hostedUrl")),
            apply_url=opt_str(p.get("applyUrl")),
            source_created_at=_created_at(created),
            source_updated_at=None,
            description_html=_description(p),
        )


def _created_at(v: Any) -> datetime | None:
    if not isinstance(v, int) or isinstance(v, bool):
        return None
    try:
        return from_epoch_ms(v)
    except (OverflowError, OSError, ValueError) as e:
        raise NormalizeError(f"bad createdAt {v!r}") from e


def _salary(v: Any) -> Compensation | None:
    if not isinstance(v, dict):
        return None
    lo, hi = v.get("min"), v.get("max")
    if not isinstance(lo, int | float) and not isinstance(hi, int | float):
        return None
    return Compensation(
        min=float(lo) if isinstance(lo, int | float) else None,
        max=float(hi) if isinstance(hi, int | float) else None,
        currency=opt_str(v.get("currency")),
        interval=opt_str(v.get("interval")),
    )


def _description(p: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("opening", "descriptionBody"):
        s = p.get(key)
        if isinstance(s, str) and s:
            parts.append(s)
    for section in p.get("lists") or []:
        if not isinstance(section, dict):
            continue
        heading = opt_str(section.get("text"))
        content = section.get("content")
        if heading:
            parts.append(f"<h3>{heading}</h3>")
        if isinstance(content, str) and content:
            parts.append(f"<ul>{content}</ul>")
    add = p.get("additional")
    if isinstance(add, str) and add:
        parts.append(add)
    return "".join(parts)
