"""Eightfold adapter (two-phase: list + detail). Spec:
docs/superpowers/specs/2026-09-04-multi-ats-expansion-design.md §4.5. The
fixture decided the description path (T-20260904-YZPA): list positions carry
an EMPTY `job_description`; the per-position resource carries it in full. The
list pages with start/num against `count`, and the API clamps num to 10
(probed live: num=100 returns 10 positions). `board.extra["base"]` is the
tenant's explore host; `board.extra["domain"]` scopes every request. Netflix
only for now — other Eightfold tenants (Microsoft, Qualcomm) gate the
endpoint anonymously and stay out until an open one is verified.
"""

from __future__ import annotations

from datetime import UTC, datetime
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

_LIST_NUM = 10  # the server clamp; asking for more silently returns 10


def _ts(v: Any) -> datetime | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return datetime.fromtimestamp(v, tz=UTC)


def _positions_locations(pos: dict[str, Any]) -> tuple[str, ...]:
    locs = pos.get("locations")
    if isinstance(locs, list):
        return norm_locations(locs)
    return norm_locations([pos.get("location")])


class Eightfold:
    """Eightfold explore-site public jobs API, domain-scoped."""

    name = "eightfold"
    adapter_version = "eightfold/1"
    embedded = False

    def list_url(self, board: Board, offset: int) -> RequestSpec:
        base = board.extra["base"]
        domain = board.extra["domain"]
        return RequestSpec(
            url=f"{base}/api/apply/v2/jobs?domain={domain}&start={offset}&num={_LIST_NUM}"
        )

    def parse_list(self, body: bytes) -> ListPage:
        obj = load_json(body)
        if not isinstance(obj, dict) or not isinstance(obj.get("positions"), list):
            raise EnvelopeError("eightfold: expected {positions: [...], count: N}")
        total = obj.get("count")
        if not isinstance(total, int) or isinstance(total, bool):
            raise EnvelopeError("eightfold: count is not an integer")
        rows = []
        for pos in obj["positions"]:
            if not isinstance(pos, dict):
                continue
            # position ids are integers on the wire
            uid = opt_str(str(pos["id"])) if pos.get("id") is not None else None
            if uid is None:
                continue
            rows.append(
                ListRow(
                    uid=uid,
                    title=opt_str(pos.get("name")),
                    locations=_positions_locations(pos),
                    posted_at=_ts(pos.get("t_create")),
                    payload=pos,
                )
            )
        return ListPage(rows=tuple(rows), total=total)

    def detail_url(self, board: Board, row: ListRow) -> RequestSpec:
        base = board.extra["base"]
        domain = board.extra["domain"]
        return RequestSpec(url=f"{base}/api/apply/v2/jobs/{row.uid}?domain={domain}")

    def normalize_detail(self, body: bytes, row: ListRow, board: Board) -> PostingVersion:
        obj = load_json(body)
        if not isinstance(obj, dict):
            raise EnvelopeError("eightfold: expected a position object")
        title = req_str(obj, "name")
        description = opt_str(obj.get("job_description"))
        if description is None:
            raise NormalizeError("eightfold: job_description is empty on the detail resource")
        return PostingVersion(
            source=self.name,
            board=board.board,
            source_id=row.uid,
            title=title,
            company=board.company,
            locations=_positions_locations(obj),
            workplace_type=opt_str(obj.get("work_location_option")),
            is_remote=None,
            department=opt_str(obj.get("department")),
            team=opt_str(obj.get("business_unit")),
            employment_type=opt_str(obj.get("type")),
            compensation=None,
            url=opt_str(obj.get("canonicalPositionUrl")),
            apply_url=None,
            source_created_at=_ts(obj.get("t_create")),
            source_updated_at=_ts(obj.get("t_update")),
            description_html=description,
        )

    def normalize_row(self, row: ListRow, board: Board) -> PostingVersion:
        raise NormalizeError("eightfold is list+detail; versions come from normalize_detail")
