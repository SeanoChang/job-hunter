"""Oracle HCM Cloud Recruiting (CE) adapter tests. Fixtures recorded live from
JPMC (jpmc.fa.oraclecloud.com, siteNumber CX_1001), spec §4.2. The detail
request shape is pinned by the probe (please-map-it/web/E-20260905-4W5W-...),
not the spec's `ById` guess on the list resource (that 400s).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from jobhunter.models import Board
from jobhunter.sources.base import EnvelopeError, ListRow, NormalizeError
from jobhunter.sources.oraclehcm import OracleHCM
from jobhunter.timeutil import parse_iso

FIXTURES = Path(__file__).parent / "fixtures"

DISTINCTIVE_SENTENCE = (
    "We have an opportunity to impact your career and provide an adventure "
    "where you can push the limits of what's possible."
)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def board() -> Board:
    return Board(
        company="JPMorganChase",
        source="oraclehcm",
        board="jpmc",
        extra=MappingProxyType({"base": "https://jpmc.fa.oraclecloud.com", "site": "CX_1001"}),
    )


# ---- list phase (ac-1) ----------------------------------------------------


def test_list_url_gets_the_ce_requisitions_endpoint(board: Board) -> None:
    spec = OracleHCM().list_url(board, offset=400)
    assert spec.url == (
        "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        "?onlyData=true&expand=requisitionList.secondaryLocations"
        "&finder=findReqs;siteNumber=CX_1001,limit=200,offset=400"
    )
    assert spec.method == "GET"
    assert spec.json_body is None


def test_list_url_offset_zero(board: Board) -> None:
    spec = OracleHCM().list_url(board, offset=0)
    assert spec.url.endswith("limit=200,offset=0")


def test_parse_list_real_fixture_rows_and_total() -> None:
    # Fixture recorded with limit=5 (fixture-size concession, ticket note); the
    # adapter itself always requests limit=200 (see test_list_url above).
    page = OracleHCM().parse_list(_fixture("oraclehcm_list.json"))
    assert page.total == 7328
    assert len(page.rows) == 5
    first = page.rows[0]
    assert first.uid == "210642927"
    assert first.title == "Lead Software Engineering - Automation"
    assert first.locations == ("Jersey City, NJ, United States",)
    assert first.posted_at == parse_iso("2026-09-03")


def _synthetic_page(n_rows: int, total: int) -> bytes:
    rows = [
        {
            "Id": str(i),
            "Title": f"Job {i}",
            "PrimaryLocation": "Remote",
            "secondaryLocations": [],
        }
        for i in range(n_rows)
    ]
    return json.dumps(
        {"items": [{"requisitionList": rows, "TotalJobsCount": total}]}
    ).encode()


def test_parse_list_pagination_offset_advances_in_limit_200_windows() -> None:
    # Mirrors fetch.py's driver: offset += len(page.rows) each iteration, stop
    # when a page returns no rows or offset reaches the reported total.
    source = OracleHCM()
    offset = 0

    page1 = source.parse_list(_synthetic_page(200, total=450))
    assert page1.total == 450
    offset += len(page1.rows)
    assert offset == 200
    assert offset < page1.total

    page2 = source.parse_list(_synthetic_page(200, total=450))
    offset += len(page2.rows)
    assert offset == 400
    assert offset < page2.total

    page3 = source.parse_list(_synthetic_page(50, total=450))
    offset += len(page3.rows)
    assert offset == 450
    assert offset >= page3.total  # driver would stop here


# ---- envelope (ac-3) --------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"items": []}',
        b'{"items": "nope"}',
        b'{"items": [{"requisitionList": "nope", "TotalJobsCount": 1}]}',
        b'{"items": [{"requisitionList": [], "TotalJobsCount": "nope"}]}',
        b'{"items": [{"requisitionList": []}]}',
    ],
)
def test_parse_list_bad_envelope_raises_envelope_error(body: bytes) -> None:
    with pytest.raises(EnvelopeError):
        OracleHCM().parse_list(body)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"items": []}',
        b'{"items": ["nope"]}',
        b'{"other": {}}',
    ],
)
def test_normalize_detail_bad_envelope_raises_envelope_error(board: Board, body: bytes) -> None:
    row = ListRow(uid="210642927")
    with pytest.raises(EnvelopeError):
        OracleHCM().normalize_detail(body, row, board)


# ---- detail phase (ac-2) ----------------------------------------------------


def test_detail_url_uses_the_probe_pinned_finder(board: Board) -> None:
    row = ListRow(uid="210642927")
    spec = OracleHCM().detail_url(board, row)
    assert spec.url == (
        "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitionDetails"
        '?onlyData=true&expand=all&finder=ById;Id="210642927",siteNumber=CX_1001'
    )
    assert spec.method == "GET"
    assert spec.json_body is None


def test_normalize_detail_real_fixture(board: Board) -> None:
    row = ListRow(uid="210642927")
    pv = OracleHCM().normalize_detail(_fixture("oraclehcm_detail.json"), row, board)
    assert pv.uid == "oh:jpmc:210642927"
    assert pv.title == "Lead Software Engineering - Automation"
    assert pv.company == "JPMorganChase"
    assert "Jersey City, NJ, United States" in pv.locations
    assert "Jersey City, NJ" in pv.locations
    assert pv.employment_type is None
    assert pv.compensation is None
    assert pv.url is None
    assert pv.apply_url is None
    assert pv.source_created_at == parse_iso("2026-09-03T15:57:40+00:00")
    assert DISTINCTIVE_SENTENCE in pv.description_html
    # OrganizationDescriptionStr, ExternalDescriptionStr, CorporateDescriptionStr order.
    org_idx = pv.description_html.index("Our professionals in our Corporate Functions")
    ext_idx = pv.description_html.index(DISTINCTIVE_SENTENCE)
    corp_idx = pv.description_html.index("JPMorganChase, one of the oldest financial institutions")
    assert org_idx < ext_idx < corp_idx


def test_normalize_detail_missing_title_raises(board: Board) -> None:
    row = ListRow(uid="210642927")
    body = json.dumps({"items": [{"ExternalDescriptionStr": "<p>x</p>"}]}).encode()
    with pytest.raises(NormalizeError):
        OracleHCM().normalize_detail(body, row, board)


def test_normalize_detail_missing_all_descriptions_raises(board: Board) -> None:
    row = ListRow(uid="210642927")
    body = json.dumps({"items": [{"Title": "X"}]}).encode()
    with pytest.raises(NormalizeError):
        OracleHCM().normalize_detail(body, row, board)


def test_normalize_detail_skips_absent_description_parts(board: Board) -> None:
    row = ListRow(uid="210642927")
    body = json.dumps(
        {"items": [{"Title": "X", "ExternalDescriptionStr": "<p>core body</p>"}]}
    ).encode()
    pv = OracleHCM().normalize_detail(body, row, board)
    assert pv.description_html == "<p>core body</p>"
