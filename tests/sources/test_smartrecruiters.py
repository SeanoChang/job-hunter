"""SmartRecruiters adapter tests. Fixtures recorded live from Canva
(api.smartrecruiters.com, 2026-09-06), spec §4.4 — the one family with an
official documented public API: postings list (totalFound pagination) plus
posting detail whose jobAd.sections carry the description. Snap's low count
re-verified at add time: SNAPInc1 really publishes 20 postings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunter.models import Board
from jobhunter.sources.base import EnvelopeError, ListRow, NormalizeError
from jobhunter.sources.smartrecruiters import SmartRecruiters
from jobhunter.timeutil import parse_iso

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def board() -> Board:
    return Board(company="Canva", source="smartrecruiters", board="canva")


# ---- list phase (ac-1) ----------------------------------------------------


def test_list_url_pages_the_postings_api(board: Board) -> None:
    spec = SmartRecruiters().list_url(board, offset=200)
    assert spec.url == (
        "https://api.smartrecruiters.com/v1/companies/canva/postings?limit=100&offset=200"
    )
    assert spec.method == "GET"


def test_parse_list_real_fixture_rows_and_totalfound(board: Board) -> None:
    page = SmartRecruiters().parse_list(_fixture("smartrecruiters_list.json"))
    assert page.total == 263
    assert len(page.rows) == 3
    row = page.rows[0]
    assert row.uid == "6000000001377496"
    assert row.title is not None
    assert row.title.startswith("Growth Partnerships Manager")
    assert row.posted_at == parse_iso("2026-09-04T05:10:39.416Z")
    assert any("San Francisco" in loc for loc in row.locations)


def test_parse_list_bad_envelope_raises() -> None:
    with pytest.raises(EnvelopeError):
        SmartRecruiters().parse_list(b'{"content": 7}')
    with pytest.raises(EnvelopeError):
        SmartRecruiters().parse_list(b"not json")


# ---- detail phase (ac-2) ---------------------------------------------------


def test_detail_url_is_the_posting_resource(board: Board) -> None:
    row = ListRow(uid="6000000001377496")
    spec = SmartRecruiters().detail_url(board, row)
    assert spec.url == (
        "https://api.smartrecruiters.com/v1/companies/canva/postings/6000000001377496"
    )


def test_normalize_detail_joins_jobad_sections(board: Board) -> None:
    row = ListRow(uid="6000000001377496")
    pv = SmartRecruiters().normalize_detail(
        _fixture("smartrecruiters_detail.json"), row, board
    )
    assert pv.uid == "sr:canva:6000000001377496"
    assert pv.title.startswith("Growth Partnerships Manager")
    assert pv.company == "Canva"
    # jobAd.sections joined in site order; empty sections are dropped, so the
    # fixture (only jobDescription has text) yields exactly that body.
    assert "Job Description" not in pv.description_html  # headings only when >1 section
    assert len(pv.description_html) > 5000
    assert pv.url == (
        "https://jobs.smartrecruiters.com/Canva/"
        "6000000001377496-growth-partnerships-manager-startups-12-month-fixed-term-contract-"
    )
    assert pv.apply_url is not None and pv.apply_url.endswith("?oga=true")
    assert pv.employment_type == "Full-time"
    assert pv.source_created_at == parse_iso("2026-09-04T05:10:39.416Z")


def test_normalize_detail_all_sections_empty_raises(board: Board) -> None:
    row = ListRow(uid="1")
    body = json.dumps({"name": "T", "jobAd": {"sections": {}}}).encode()
    with pytest.raises(NormalizeError):
        SmartRecruiters().normalize_detail(body, row, board)


def test_normalize_row_refuses(board: Board) -> None:
    with pytest.raises(NormalizeError):
        SmartRecruiters().normalize_row(ListRow(uid="1"), board)
