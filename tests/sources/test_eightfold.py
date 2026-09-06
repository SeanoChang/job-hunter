"""Eightfold adapter tests. Fixtures recorded live from Netflix
(explore.jobs.netflix.net, 2026-09-06), spec §4.5. The fixture decided the
description path (ticket T-20260904-YZPA): the LIST's positions carry an empty
`job_description`, the per-position call carries it in full — so this family
is classic list+detail. Pagination is start/num against `count`, and the API
clamps num to 10 (num=100 returns 10 positions, probed live). Netflix only:
Microsoft and Qualcomm are Eightfold but gated anonymously.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from jobhunter.models import Board
from jobhunter.sources.base import EnvelopeError, ListRow, NormalizeError
from jobhunter.sources.eightfold import Eightfold

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def board() -> Board:
    return Board(
        company="Netflix",
        source="eightfold",
        board="netflix",
        extra=MappingProxyType(
            {"base": "https://explore.jobs.netflix.net", "domain": "netflix.com"}
        ),
    )


# ---- list phase (ac-1) ----------------------------------------------------


def test_list_url_pages_with_start_num(board: Board) -> None:
    spec = Eightfold().list_url(board, offset=30)
    assert spec.url == (
        "https://explore.jobs.netflix.net/api/apply/v2/jobs"
        "?domain=netflix.com&start=30&num=10"
    )
    assert spec.method == "GET"


def test_parse_list_real_fixture_positions_and_count(board: Board) -> None:
    page = Eightfold().parse_list(_fixture("eightfold_list.json"))
    assert page.total == 501
    assert len(page.rows) == 3
    row = page.rows[0]
    assert row.uid == "790298014263"
    assert row.title == "AI Engineer 6 - AI Foundation & Tooling, Ads Platform"
    assert "USA - Remote" in row.locations
    assert row.posted_at == datetime.fromtimestamp(1721692800, tz=UTC)


def test_parse_list_bad_envelope_raises() -> None:
    with pytest.raises(EnvelopeError):
        Eightfold().parse_list(b'{"positions": 9}')
    with pytest.raises(EnvelopeError):
        Eightfold().parse_list(b"not json")


# ---- detail phase (ac-2, description) --------------------------------------


def test_detail_url_is_the_per_position_resource(board: Board) -> None:
    row = ListRow(uid="790298014263")
    spec = Eightfold().detail_url(board, row)
    assert spec.url == (
        "https://explore.jobs.netflix.net/api/apply/v2/jobs/790298014263?domain=netflix.com"
    )


def test_normalize_detail_full_description(board: Board) -> None:
    row = ListRow(uid="790298014263")
    pv = Eightfold().normalize_detail(_fixture("eightfold_detail.json"), row, board)
    assert pv.uid == "ef:netflix:790298014263"
    assert pv.title == "AI Engineer 6 - AI Foundation & Tooling, Ads Platform"
    assert pv.company == "Netflix"
    assert len(pv.description_html) > 8000
    assert "our mission is to entertain the world" in pv.description_html
    assert pv.url == (
        "https://explore.jobs.netflix.net/careers/job/790298014263?microsite=netflix.com"
    )
    assert "USA - Remote" in pv.locations
    assert pv.source_created_at == datetime.fromtimestamp(1721692800, tz=UTC)
    assert pv.source_updated_at == datetime.fromtimestamp(1779148800, tz=UTC)


def test_normalize_detail_empty_description_raises(board: Board) -> None:
    row = ListRow(uid="1")
    body = json.dumps({"id": 1, "name": "T", "job_description": ""}).encode()
    with pytest.raises(NormalizeError):
        Eightfold().normalize_detail(body, row, board)


def test_normalize_row_refuses(board: Board) -> None:
    with pytest.raises(NormalizeError):
        Eightfold().normalize_row(ListRow(uid="1"), board)
