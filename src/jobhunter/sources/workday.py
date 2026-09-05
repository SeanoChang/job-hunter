"""Workday CXS adapter (two-phase: list + detail). Spec: docs/superpowers/specs/
2026-09-04-multi-ats-expansion-design.md §4.1. Fixtures recorded live from the
verified NVIDIA tenant (nvidia.wd5.myworkdayjobs.com, NVIDIAExternalCareerSite).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jobhunter.models import Board, PostingVersion
from jobhunter.sources.base import (
    EnvelopeError,
    ListPage,
    ListRow,
    NormalizeError,
    RequestSpec,
    load_json,
    norm_employment,
    norm_locations,
    opt_str,
    req_str,
)
from jobhunter.timeutil import parse_iso

_LIST_LIMIT = 20


class Workday:
    """Workday CXS: `POST .../wday/cxs/{tenant}/{site}/jobs` for the list,
    `GET .../wday/cxs/{tenant}/{site}{externalPath}` per posting for the detail.

    The tenant is `board.board` itself (e.g. "nvidia"); `board.extra["host"]`
    is the Workday pod label (e.g. "wd5"), not a full hostname — the two
    combine as `{tenant}.{host}.myworkdayjobs.com` (spec §4.1).
    """

    name = "workday"
    adapter_version = "workday/1"

    def list_url(self, board: Board, offset: int) -> RequestSpec:
        return RequestSpec(
            url=f"{_cxs_base(board)}/jobs",
            method="POST",
            json_body={
                "appliedFacets": {},
                "limit": _LIST_LIMIT,
                "offset": offset,
                "searchText": "",
            },
        )

    def parse_list(self, body: bytes) -> ListPage:
        obj = load_json(body)
        if not isinstance(obj, dict) or not isinstance(obj.get("jobPostings"), list):
            raise EnvelopeError("workday: expected {jobPostings: [...], total}")
        total = obj.get("total")
        if not isinstance(total, int):
            raise EnvelopeError("workday: expected an integer 'total'")
        rows = []
        for item in obj["jobPostings"]:
            if not isinstance(item, dict):
                continue
            uid = _row_uid(item)
            if uid is None:
                continue
            rows.append(
                ListRow(
                    uid=uid,
                    detail_path=opt_str(item.get("externalPath")),
                    title=opt_str(item.get("title")),
                    locations=norm_locations([item.get("locationsText")]),
                    posted_at=None,  # postedOn is relative text ("Posted Today"); see spec §7
                    payload=item,
                )
            )
        return ListPage(rows=tuple(rows), total=total)

    def detail_url(self, board: Board, row: ListRow) -> RequestSpec:
        if not row.detail_path:
            raise NormalizeError(f"row {row.uid!r} has no externalPath for the detail fetch")
        return RequestSpec(url=f"{_cxs_base(board)}{row.detail_path}")

    def normalize_detail(self, body: bytes, row: ListRow, board: Board) -> PostingVersion:
        obj = load_json(body)
        if not isinstance(obj, dict) or not isinstance(obj.get("jobPostingInfo"), dict):
            raise EnvelopeError("workday: expected {jobPostingInfo: {...}}")
        jpi: dict[str, Any] = obj["jobPostingInfo"]
        title = req_str(jpi, "title")
        desc = jpi.get("jobDescription")
        if not isinstance(desc, str):
            raise NormalizeError("missing jobDescription")
        locations = [jpi.get("location"), *(jpi.get("additionalLocations") or [])]
        apply_url = opt_str(jpi.get("externalUrl"))
        return PostingVersion(
            source=self.name,
            board=board.board,
            source_id=row.uid,
            title=title,
            company=board.company,
            locations=norm_locations(locations),
            workplace_type=None,
            is_remote=None,
            department=None,
            team=None,
            employment_type=norm_employment(jpi.get("timeType")),
            compensation=None,  # no structured field; ranges live in the description (spec §4.1)
            url=apply_url,
            apply_url=apply_url,
            source_created_at=_dt(jpi.get("startDate")),
            source_updated_at=None,
            description_html=desc,
        )


def _cxs_base(board: Board) -> str:
    tenant = board.board
    host = board.extra["host"]
    site = board.extra["site"]
    return f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def _row_uid(item: dict[str, Any]) -> str | None:
    bullets = item.get("bulletFields")
    if isinstance(bullets, list) and bullets:
        s = opt_str(bullets[0])
        if s is not None:
            return s
    return opt_str(item.get("externalPath"))


def _dt(v: Any) -> datetime | None:
    s = opt_str(v)
    if s is None:
        return None
    try:
        return parse_iso(s)
    except ValueError as e:
        raise NormalizeError(f"bad startDate {s!r}") from e
