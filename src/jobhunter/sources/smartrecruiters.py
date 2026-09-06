"""SmartRecruiters adapter (two-phase: list + detail). Spec:
docs/superpowers/specs/2026-09-04-multi-ats-expansion-design.md §4.4. The one
family with an official documented public API (no auth): postings list pages
with limit/offset against `totalFound`; the posting detail's jobAd.sections
carry the description. The board token is the company identifier the API path
uses, case-sensitive ("canva", "Grab", "Wix2", "SNAPInc1"). Fixtures recorded
live from Canva, 2026-09-06.
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
    norm_locations,
    opt_str,
    req_str,
)
from jobhunter.timeutil import parse_iso

_BASE = "https://api.smartrecruiters.com/v1/companies"
_LIST_LIMIT = 100
# jobAd.sections in the order the hosted posting renders them.
_SECTION_ORDER = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def _released(v: Any) -> datetime | None:
    s = opt_str(v)
    if s is None:
        return None
    try:
        return parse_iso(s)
    except ValueError:
        return None


def _location_strings(loc: Any) -> list[str]:
    if not isinstance(loc, dict):
        return []
    return [opt_str(loc.get("fullLocation")) or opt_str(loc.get("city")) or ""]


class SmartRecruiters:
    """SmartRecruiters public postings API; one board per company identifier."""

    name = "smartrecruiters"
    adapter_version = "smartrecruiters/1"
    embedded = False

    def list_url(self, board: Board, offset: int) -> RequestSpec:
        return RequestSpec(
            url=f"{_BASE}/{board.board}/postings?limit={_LIST_LIMIT}&offset={offset}"
        )

    def parse_list(self, body: bytes) -> ListPage:
        obj = load_json(body)
        if not isinstance(obj, dict) or not isinstance(obj.get("content"), list):
            raise EnvelopeError("smartrecruiters: expected {content: [...], totalFound: N}")
        total = obj.get("totalFound")
        if not isinstance(total, int) or isinstance(total, bool):
            raise EnvelopeError("smartrecruiters: totalFound is not an integer")
        rows = []
        for item in obj["content"]:
            if not isinstance(item, dict):
                continue
            uid = opt_str(item.get("id"))
            if uid is None:
                continue
            rows.append(
                ListRow(
                    uid=uid,
                    title=opt_str(item.get("name")),
                    locations=norm_locations(_location_strings(item.get("location"))),
                    posted_at=_released(item.get("releasedDate")),
                    payload=item,
                )
            )
        return ListPage(rows=tuple(rows), total=total)

    def detail_url(self, board: Board, row: ListRow) -> RequestSpec:
        return RequestSpec(url=f"{_BASE}/{board.board}/postings/{row.uid}")

    def normalize_detail(self, body: bytes, row: ListRow, board: Board) -> PostingVersion:
        obj = load_json(body)
        if not isinstance(obj, dict):
            raise EnvelopeError("smartrecruiters: expected a posting object")
        title = req_str(obj, "name")
        sections = ((obj.get("jobAd") or {}).get("sections")) or {}
        parts: list[tuple[str | None, str]] = []
        for key in _SECTION_ORDER:
            sec = sections.get(key)
            if not isinstance(sec, dict):
                continue
            text = opt_str(sec.get("text"))
            if text is None:
                continue
            parts.append((opt_str(sec.get("title")), text))
        if not parts:
            raise NormalizeError("smartrecruiters: every jobAd section is empty")
        if len(parts) == 1:
            description_html = parts[0][1]
        else:
            description_html = "\n".join(
                f"<h2>{heading}</h2>\n{text}" if heading else text for heading, text in parts
            )
        employment = obj.get("typeOfEmployment")
        return PostingVersion(
            source=self.name,
            board=board.board,
            source_id=row.uid,
            title=title,
            company=board.company,
            locations=norm_locations(_location_strings(obj.get("location"))),
            workplace_type=None,
            is_remote=bool((obj.get("location") or {}).get("remote")) or None,
            department=opt_str((obj.get("function") or {}).get("label")),
            team=opt_str((obj.get("department") or {}).get("label")),
            employment_type=opt_str((employment or {}).get("label")),
            compensation=None,
            url=opt_str(obj.get("postingUrl")),
            apply_url=opt_str(obj.get("applyUrl")),
            source_created_at=_released(obj.get("releasedDate")),
            source_updated_at=None,
            description_html=description_html,
        )

    def normalize_row(self, row: ListRow, board: Board) -> PostingVersion:
        raise NormalizeError("smartrecruiters is list+detail; versions come from normalize_detail")
