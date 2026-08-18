"""Ashby Job Board API adapter. Payload analysis: docs/sources/ashby.md."""

from __future__ import annotations

from collections.abc import Iterator
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
from jobhunter.timeutil import parse_iso

_INTERVALS = {
    "1 YEAR": "year",
    "1 MONTH": "month",
    "1 WEEK": "week",
    "1 DAY": "day",
    "1 HOUR": "hour",
}


class Ashby:
    name = "ashby"
    adapter_version = "ashby/1"

    def url(self, board: Board) -> str:
        return (
            f"https://api.ashbyhq.com/posting-api/job-board/{board.board}"
            "?includeCompensation=true"
        )

    def parse(self, body: bytes) -> Iterator[RawRecord]:
        obj = load_json(body)
        if not isinstance(obj, dict) or not isinstance(obj.get("jobs"), list):
            raise EnvelopeError("ashby: expected {apiVersion, jobs: [...]}")
        for i, item in enumerate(obj["jobs"]):
            yield RawRecord(record_id(item), i, as_payload(item))

    def normalize(self, rec: RawRecord, board: Board) -> PostingVersion:
        p: dict[str, Any] = rec.payload
        if rec.source_id is None:
            raise NormalizeError("record has no id")
        title = req_str(p, "title")
        desc = p.get("descriptionHtml")
        if not isinstance(desc, str):
            raise NormalizeError("missing descriptionHtml")
        locations = [p.get("location")]
        for sec in p.get("secondaryLocations") or []:
            if isinstance(sec, dict):
                locations.append(sec.get("location"))
        is_remote = p.get("isRemote")
        published = opt_str(p.get("publishedAt"))
        return PostingVersion(
            source=self.name,
            board=board.board,
            source_id=rec.source_id,
            title=title,
            company=board.company,
            locations=norm_locations(locations),
            workplace_type=norm_workplace(p.get("workplaceType")),
            is_remote=is_remote if isinstance(is_remote, bool) else None,
            department=opt_str(p.get("department")),
            team=opt_str(p.get("team")),
            employment_type=norm_employment(p.get("employmentType")),
            compensation=_salary(p.get("compensation")),
            url=opt_str(p.get("jobUrl")),
            apply_url=opt_str(p.get("applyUrl")),
            source_created_at=parse_iso(published) if published else None,
            source_updated_at=None,
            description_html=desc,
        )


def _salary(v: Any) -> Compensation | None:
    if not isinstance(v, dict):
        return None
    for comp in v.get("summaryComponents") or []:
        if not isinstance(comp, dict) or comp.get("compensationType") != "Salary":
            continue
        lo, hi = comp.get("minValue"), comp.get("maxValue")
        if not isinstance(lo, int | float) and not isinstance(hi, int | float):
            continue
        raw_interval = opt_str(comp.get("interval"))
        if raw_interval is None or raw_interval.upper() == "NONE":
            interval = None
        else:
            interval = _INTERVALS.get(raw_interval, raw_interval.lower())
        return Compensation(
            min=float(lo) if isinstance(lo, int | float) else None,
            max=float(hi) if isinstance(hi, int | float) else None,
            currency=opt_str(comp.get("currencyCode")),
            interval=interval,
        )
    return None
