"""Amazon jobs adapter tests. Fixture recorded live from amazon.jobs
search.json (2026-09-06), spec §4.3. The fixture decided the description path:
rows carry description, basic_qualifications and preferred_qualifications in
full, and the per-job `.json` path serves HTML — so the adapter is EMBEDDED
(versions come from list rows; there is no detail phase). See ticket
T-20260904-2MPS Interfaces and the prerequisite T-20260906-7PTV.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobhunter.models import Board
from jobhunter.sources.amazonjobs import AmazonJobs
from jobhunter.sources.base import EnvelopeError, ListRow, NormalizeError

FIXTURES = Path(__file__).parent / "fixtures"

# ac-2: a verbatim span of the fixture posting's qualifications text.
QUALIFICATIONS_SENTENCE = (
    "using SQL to pull data from a database or data warehouse and scripting "
    "experience (Python) to process data for modeling"
)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def board() -> Board:
    return Board(company="Amazon", source="amazonjobs", board="amazon")


# ---- list phase (ac-1) ----------------------------------------------------


def test_embedded_flag() -> None:
    assert AmazonJobs().embedded is True


def test_list_url_pages_search_json(board: Board) -> None:
    spec = AmazonJobs().list_url(board, offset=300)
    assert spec.url == (
        "https://www.amazon.jobs/en/search.json?offset=300&result_limit=100&sort=recent"
    )
    assert spec.method == "GET"
    assert spec.json_body is None


def test_parse_list_real_fixture_rows_and_hits(board: Board) -> None:
    page = AmazonJobs().parse_list(_fixture("amazonjobs_list.json"))
    assert page.total == 10000  # the display cap: pagination walks against `hits`
    assert len(page.rows) == 3
    row = page.rows[0]
    assert row.uid == "10530730"  # id_icims: the stable id the public URL carries
    assert row.title == "Business Intelligence Engineer II, Customer Returns , Amazon"
    assert "Bengaluru, Karnataka, IND" in row.locations
    assert row.posted_at == datetime(2026, 9, 6, tzinfo=UTC)  # "September  6, 2026"
    assert row.detail_path is None  # embedded: nothing to fetch per row


def test_parse_list_bad_envelope_raises() -> None:
    with pytest.raises(EnvelopeError):
        AmazonJobs().parse_list(b'{"jobs": "nope"}')
    with pytest.raises(EnvelopeError):
        AmazonJobs().parse_list(b"not json")


def test_parse_list_row_without_id_is_skipped(board: Board) -> None:
    body = json.dumps({"hits": 1, "jobs": [{"title": "no id here"}]}).encode()
    page = AmazonJobs().parse_list(body)
    assert page.rows == ()


# ---- embedded normalization (ac-2) ----------------------------------------


def test_normalize_row_description_carries_qualifications_verbatim(board: Board) -> None:
    page = AmazonJobs().parse_list(_fixture("amazonjobs_list.json"))
    pv = AmazonJobs().normalize_row(page.rows[0], board)
    assert pv.uid == "az:amazon:10530730"
    assert pv.title == "Business Intelligence Engineer II, Customer Returns , Amazon"
    assert pv.company == "Amazon"
    assert QUALIFICATIONS_SENTENCE in pv.description_html
    # description, basic and preferred qualifications all present, in order
    d = pv.description_html
    assert d.index("Business Intelligence Engineer II, you will be a core member") < d.index(
        QUALIFICATIONS_SENTENCE
    ) < d.index("Experience with AWS solutions such as EC2, DynamoDB, S3, and Redshift")
    assert pv.url == (
        "https://www.amazon.jobs/en/jobs/10530730/"
        "business-intelligence-engineer-ii-customer-returns-amazon"
    )
    assert pv.employment_type == "full-time"
    assert pv.source_created_at == datetime(2026, 9, 6, tzinfo=UTC)
    assert "Bengaluru, Karnataka, IND" in pv.locations


def test_normalize_row_missing_content_raises(board: Board) -> None:
    row = ListRow(uid="1", payload={"title": "T"})
    with pytest.raises(NormalizeError):
        AmazonJobs().normalize_row(row, board)


# ---- the not-applicable detail surface -------------------------------------


def test_detail_surface_refuses(board: Board) -> None:
    row = ListRow(uid="10530730")
    with pytest.raises(NormalizeError):
        AmazonJobs().detail_url(board, row)
    with pytest.raises(NormalizeError):
        AmazonJobs().normalize_detail(b"{}", row, board)
