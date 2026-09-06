"""Oracle HCM Cloud Recruiting (CE) adapter (two-phase: list + detail). Spec:
docs/superpowers/specs/2026-09-04-multi-ats-expansion-design.md §4.2. The list
endpoint is spec-verified live; the detail request shape is NOT the spec's
`ById` guess on `recruitingCEJobRequisitions` (that 400s) but the shape pinned
by the probe (please-map-it/web/E-20260905-4W5W-...): the sibling
`recruitingCEJobRequisitionDetails` resource with `finder=ById;Id="<Id>",
siteNumber=<site>` + `onlyData=true&expand=all`. Fixtures recorded live from
JPMC (jpmc.fa.oraclecloud.com, siteNumber CX_1001).
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

_LIST_LIMIT = 200


class OracleHCM:
    """Oracle HCM Cloud Recruiting CE (Candidate Experience) site.

    `board.extra["base"]` is the tenant's Fusion base (e.g.
    "https://jpmc.fa.oraclecloud.com"); `board.extra["site"]` is the CE site
    number (e.g. "CX_1001").
    """

    name = "oraclehcm"
    adapter_version = "oraclehcm/1"
    embedded = False

    def list_url(self, board: Board, offset: int) -> RequestSpec:
        base = board.extra["base"]
        site = board.extra["site"]
        return RequestSpec(
            url=(
                f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                "?onlyData=true&expand=requisitionList.secondaryLocations"
                f"&finder=findReqs;siteNumber={site},limit={_LIST_LIMIT},offset={offset}"
            )
        )

    def parse_list(self, body: bytes) -> ListPage:
        obj = load_json(body)
        root = _first_item(obj)
        if root is None:
            raise EnvelopeError("oraclehcm: expected {items: [{...}]}")
        req_list = root.get("requisitionList")
        if not isinstance(req_list, list):
            raise EnvelopeError("oraclehcm: expected items[0].requisitionList to be a list")
        total = root.get("TotalJobsCount")
        if not isinstance(total, int) or isinstance(total, bool):
            raise EnvelopeError("oraclehcm: expected an integer items[0].TotalJobsCount")
        rows = []
        for item in req_list:
            if not isinstance(item, dict):
                continue
            uid = _row_uid(item)
            if uid is None:
                continue
            locations = [item.get("PrimaryLocation"), *(item.get("secondaryLocations") or [])]
            rows.append(
                ListRow(
                    uid=uid,
                    title=opt_str(item.get("Title")),
                    locations=norm_locations(locations),
                    posted_at=_dt(item.get("PostedDate")),
                    payload=item,
                )
            )
        return ListPage(rows=tuple(rows), total=total)

    def detail_url(self, board: Board, row: ListRow) -> RequestSpec:
        base = board.extra["base"]
        site = board.extra["site"]
        return RequestSpec(
            url=(
                f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                f'?onlyData=true&expand=all&finder=ById;Id="{row.uid}",siteNumber={site}'
            )
        )

    def normalize_detail(self, body: bytes, row: ListRow, board: Board) -> PostingVersion:
        obj = load_json(body)
        rec = _first_item(obj)
        if rec is None:
            raise EnvelopeError("oraclehcm: expected {items: [{...}]}")
        title = req_str(rec, "Title")
        description_html = _description(rec)
        # The payload carries no link field, but the CE site URL is deterministic:
        # {base}/hcmUI/CandidateExperience/en/sites/{site}/job/{id} (probe-verified
        # live on jpmc, 2026-09-06). Ingest replay may hand us a stub Board with no
        # extra (lifecycle._board's snapshot-missing fallback) — degrade to None
        # like company degrades, never raise.
        base = board.extra.get("base")
        site = board.extra.get("site")
        url = (
            f"{base}/hcmUI/CandidateExperience/en/sites/{site}/job/{row.uid}"
            if base and site
            else None
        )
        locations = norm_locations(
            [rec.get("PrimaryLocation"), *_work_locations(rec.get("workLocation"))]
        )
        return PostingVersion(
            source=self.name,
            board=board.board,
            source_id=row.uid,
            title=title,
            company=board.company,
            locations=locations,
            workplace_type=None,
            is_remote=None,
            department=None,
            team=None,
            employment_type=None,
            # no structured compensation field observed; ranges (if any) live in the description
            compensation=None,
            url=url,
            apply_url=None,
            source_created_at=_dt(rec.get("ExternalPostedStartDate")),
            source_updated_at=None,
            description_html=description_html,
        )


    def normalize_row(self, row: ListRow, board: Board) -> PostingVersion:
        raise NormalizeError("oraclehcm is list+detail; versions come from normalize_detail")


def _first_item(obj: Any) -> dict[str, Any] | None:
    """`{"items": [{...}, ...]}` -> the first item, or None if the shape doesn't match."""
    if not isinstance(obj, dict):
        return None
    items = obj.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    return items[0]


_DESCRIPTION_KEYS = (
    "OrganizationDescriptionStr",
    "ExternalDescriptionStr",
    "CorporateDescriptionStr",
)


def _description(rec: dict[str, Any]) -> str:
    # The rendered posting concatenates all three; the external body is the core.
    parts = [s for key in _DESCRIPTION_KEYS if (s := opt_str(rec.get(key))) is not None]
    if not parts:
        raise NormalizeError(f"missing all of {_DESCRIPTION_KEYS}")
    return "\n".join(parts)


def _work_locations(entries: Any) -> list[str | None]:
    out: list[str | None] = []
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        city = opt_str(entry.get("TownOrCity"))
        region = opt_str(entry.get("Region2")) or opt_str(entry.get("Region1"))
        parts = [p for p in (city, region) if p]
        if parts:
            out.append(", ".join(parts))
    return out


def _row_uid(item: dict[str, Any]) -> str | None:
    v = item.get("Id")
    if v is None:
        return None
    return str(v).strip() or None


def _dt(v: Any) -> datetime | None:
    s = opt_str(v)
    if s is None:
        return None
    try:
        return parse_iso(s)
    except ValueError as e:
        raise NormalizeError(f"bad date {s!r}") from e
