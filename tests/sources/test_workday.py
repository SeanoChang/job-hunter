"""Workday CXS adapter tests. Fixtures recorded live from the verified NVIDIA
tenant (nvidia.wd5.myworkdayjobs.com, NVIDIAExternalCareerSite), spec §4.1.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from jobhunter.models import Board
from jobhunter.sources.base import EnvelopeError, ListRow, NormalizeError
from jobhunter.sources.workday import Workday

FIXTURES = Path(__file__).parent / "fixtures"

SALARY_SENTENCE = (
    "The base salary range is 168,000 USD - 264,500 USD for Level 4, "
    "and 196,000 USD - 310,500 USD for Level 5."
)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def board() -> Board:
    return Board(
        company="NVIDIA",
        source="workday",
        board="nvidia",
        extra=MappingProxyType({"host": "wd5", "site": "NVIDIAExternalCareerSite"}),
    )


# ---- list phase (ac-1) ----------------------------------------------------


def test_list_url_posts_the_cxs_jobs_endpoint(board: Board) -> None:
    spec = Workday().list_url(board, offset=40)
    assert spec.url == (
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    )
    assert spec.method == "POST"
    assert spec.json_body == {
        "appliedFacets": {},
        "limit": 20,
        "offset": 40,
        "searchText": "",
    }


def test_parse_list_real_fixture_uid_from_bullet_fields_and_total() -> None:
    page = Workday().parse_list(_fixture("workday_list.json"))
    assert page.total == 2000
    assert len(page.rows) == 20
    first = page.rows[0]
    # bulletFields[0] ("JR2006356") wins over externalPath when both are present.
    assert first.uid == "JR2006356"
    assert first.title == "Senior DFT Engineer"
    assert first.detail_path == "/job/US-CA-Santa-Clara/Senior-DFT-Engineer_JR2006356"
    assert first.locations == ("US, CA, Santa Clara",)


def test_parse_list_uid_falls_back_to_external_path_when_bullet_fields_empty() -> None:
    # Synthetic: real NVIDIA rows always carry a populated bulletFields, so the
    # fallback path is exercised with a hand-built minimal body, per the ticket.
    body = (
        b'{"total": 1, "jobPostings": [{"title": "Foo", '
        b'"externalPath": "/job/US-CA-Foo/Foo_JR9", "locationsText": "US, CA, Foo", '
        b'"postedOn": "Posted Today", "bulletFields": []}]}'
    )
    page = Workday().parse_list(body)
    assert len(page.rows) == 1
    assert page.rows[0].uid == "/job/US-CA-Foo/Foo_JR9"


def test_parse_list_uid_falls_back_to_external_path_when_bullet_fields_absent() -> None:
    # Synthetic: same fallback, but bulletFields is missing from the row entirely.
    body = (
        b'{"total": 1, "jobPostings": [{"title": "Foo", '
        b'"externalPath": "/job/US-CA-Foo/Foo_JR9"}]}'
    )
    page = Workday().parse_list(body)
    assert page.rows[0].uid == "/job/US-CA-Foo/Foo_JR9"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"total": 1}',
        b'{"jobPostings": []}',
        b'{"jobPostings": "nope", "total": 1}',
        b'{"jobPostings": [], "total": "nope"}',
    ],
)
def test_parse_list_bad_envelope_raises_envelope_error(body: bytes) -> None:
    with pytest.raises(EnvelopeError):
        Workday().parse_list(body)


# ---- detail phase (ac-2) ---------------------------------------------------


def test_detail_url_gets_the_cxs_job_path(board: Board) -> None:
    row = ListRow(
        uid="JR2006356", detail_path="/job/US-CA-Santa-Clara/Senior-DFT-Engineer_JR2006356"
    )
    spec = Workday().detail_url(board, row)
    assert spec.url == (
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite"
        "/job/US-CA-Santa-Clara/Senior-DFT-Engineer_JR2006356"
    )
    assert spec.method == "GET"
    assert spec.json_body is None


def test_detail_url_without_external_path_raises() -> None:
    row = ListRow(uid="JR2006356", detail_path=None)
    with pytest.raises(NormalizeError):
        Workday().detail_url(Board("NVIDIA", "workday", "nvidia"), row)


def test_normalize_detail_real_fixture(board: Board) -> None:
    row = ListRow(
        uid="JR2006356", detail_path="/job/US-CA-Santa-Clara/Senior-DFT-Engineer_JR2006356"
    )
    pv = Workday().normalize_detail(_fixture("workday_detail.json"), row, board)
    assert pv.uid == "wd:nvidia:JR2006356"
    assert pv.title == "Senior DFT Engineer"
    assert pv.company == "NVIDIA"
    assert pv.locations == ("US, CA, Santa Clara",)
    assert pv.employment_type == "full_time"
    assert pv.compensation is None
    assert pv.url == (
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
        "/job/US-CA-Santa-Clara/Senior-DFT-Engineer_JR2006356"
    )
    assert pv.apply_url == pv.url
    assert SALARY_SENTENCE in pv.description_html


def test_normalize_detail_missing_description_raises(board: Board) -> None:
    row = ListRow(uid="JR1", detail_path="/job/x")
    body = b'{"jobPostingInfo": {"title": "X", "startDate": "2026-09-04"}}'
    with pytest.raises(NormalizeError):
        Workday().normalize_detail(body, row, board)


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"other": {}}', b'{"jobPostingInfo": "x"}'])
def test_normalize_detail_bad_envelope_raises_envelope_error(board: Board, body: bytes) -> None:
    row = ListRow(uid="JR1", detail_path="/job/x")
    with pytest.raises(EnvelopeError):
        Workday().normalize_detail(body, row, board)
