"""Amazon jobs adapter (two-phase list, EMBEDDED content). Spec:
docs/superpowers/specs/2026-09-04-multi-ats-expansion-design.md §4.3. The
fixture decided the description path (ticket T-20260904-2MPS ac-3): search.json
rows carry `description`, `basic_qualifications` and `preferred_qualifications`
in full, and the per-job `.json` path serves HTML — so there is no detail
phase; versions come straight from list rows (driver mode T-20260906-7PTV).
Pagination is offset/result_limit (max 100 per page) against `hits`, which the
site caps at 10,000 (accepted, Q-20260904-9S3R). uid is `id_icims`: the stable
numeric id the public posting URL carries.
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

_BASE = "https://www.amazon.jobs"
_LIST_LIMIT = 100  # "Result limit cannot be greater than 100" (probed 2026-09-06)


def _posted_at(value: Any) -> datetime | None:
    """amazon.jobs writes dates like 'September  6, 2026' (double space and all)."""
    s = opt_str(value)
    if s is None:
        return None
    try:
        return datetime.strptime(" ".join(s.split()), "%B %d, %Y").replace(tzinfo=UTC)
    except ValueError:
        return None


class AmazonJobs:
    """amazon.jobs public search.json; one fixed board `amazonjobs:amazon`."""

    name = "amazonjobs"
    adapter_version = "amazonjobs/1"
    embedded = True

    def list_url(self, board: Board, offset: int) -> RequestSpec:
        return RequestSpec(
            url=f"{_BASE}/en/search.json?offset={offset}&result_limit={_LIST_LIMIT}&sort=recent"
        )

    def parse_list(self, body: bytes) -> ListPage:
        obj = load_json(body)
        if not isinstance(obj, dict) or not isinstance(obj.get("jobs"), list):
            raise EnvelopeError("amazonjobs: expected {jobs: [...], hits: N}")
        hits = obj.get("hits")
        if not isinstance(hits, int):
            raise EnvelopeError("amazonjobs: hits is not an integer")
        rows = []
        for job in obj["jobs"]:
            if not isinstance(job, dict):
                continue
            uid = opt_str(job.get("id_icims"))
            if uid is None:
                continue
            rows.append(
                ListRow(
                    uid=uid,
                    detail_path=None,  # embedded: nothing to fetch per row
                    title=opt_str(job.get("title")),
                    locations=norm_locations([job.get("normalized_location")]),
                    posted_at=_posted_at(job.get("posted_date")),
                    payload=job,
                )
            )
        return ListPage(rows=tuple(rows), total=hits)

    def detail_url(self, board: Board, row: ListRow) -> RequestSpec:
        raise NormalizeError("amazonjobs is embedded; there is no detail endpoint")

    def normalize_detail(self, body: bytes, row: ListRow, board: Board) -> PostingVersion:
        raise NormalizeError("amazonjobs is embedded; versions come from normalize_row")

    def normalize_row(self, row: ListRow, board: Board) -> PostingVersion:
        job = dict(row.payload)
        title = req_str(job, "title")
        description = req_str(job, "description")
        # Qualifications are demand-profile substance; keep them in the one
        # canonical text, labelled the way the site labels them.
        parts = [description]
        for key, heading in (
            ("basic_qualifications", "Basic qualifications"),
            ("preferred_qualifications", "Preferred qualifications"),
        ):
            text = opt_str(job.get(key))
            if text is not None:
                parts.append(f"<h2>{heading}</h2>\n{text}")
        path = opt_str(job.get("job_path"))
        return PostingVersion(
            source=self.name,
            board=board.board,
            source_id=row.uid,
            title=title,
            company=board.company,
            locations=norm_locations([job.get("normalized_location")]),
            workplace_type=None,
            is_remote=None,
            department=opt_str(job.get("job_category")),
            team=opt_str(job.get("team")),
            employment_type=opt_str(job.get("job_schedule_type")),
            compensation=None,
            url=f"{_BASE}{path}" if path else None,
            apply_url=None,
            source_created_at=_posted_at(job.get("posted_date")),
            source_updated_at=None,
            description_html="\n".join(parts),
        )
