from datetime import UTC, datetime

import pytest

from jobhunter.models import Board, Compensation, RawRecord
from jobhunter.sources import get_source
from jobhunter.sources.base import EnvelopeError
from tests.conftest import fixture_bytes


def test_url(boards: dict[str, Board]) -> None:
    assert get_source("ashby").url(boards["ashby"]) == (
        "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"
    )


def test_parse() -> None:
    recs = list(get_source("ashby").parse(fixture_bytes("ashby_board.json")))
    assert [r.source_id for r in recs] == ["4e64ab86-4e30-403b-b1b9-41dc052570ce"]


@pytest.mark.parametrize("body", [b"[]", b'{"apiVersion":"v0.1"}', b'{"jobs":"x"}'])
def test_parse_bad_envelope(body: bytes) -> None:
    with pytest.raises(EnvelopeError):
        list(get_source("ashby").parse(body))


def test_normalize_real_record(boards: dict[str, Board]) -> None:
    src = get_source("ashby")
    rec = next(iter(src.parse(fixture_bytes("ashby_board.json"))))
    pv = src.normalize(rec, boards["ashby"])
    assert pv.uid == "ab:ramp:4e64ab86-4e30-403b-b1b9-41dc052570ce"
    assert pv.title == "Software Engineer, Frontend"
    assert pv.company == "Ramp"
    assert pv.locations[0] == "New York, NY (HQ)" or "New York, NY (HQ)" in pv.locations
    assert "Remote (Canada)" in pv.locations and "San Francisco, CA" in pv.locations
    assert pv.locations == tuple(sorted(pv.locations))
    assert pv.workplace_type == "hybrid" and pv.is_remote is True
    assert pv.department == "Engineering" and pv.team == "Frontend"
    assert pv.employment_type == "full_time"
    assert pv.compensation == Compensation(143200.0, 284000.0, "USD", "year")
    assert pv.url == "https://jobs.ashbyhq.com/ramp/4e64ab86-4e30-403b-b1b9-41dc052570ce"
    assert pv.apply_url == (
        "https://jobs.ashbyhq.com/ramp/4e64ab86-4e30-403b-b1b9-41dc052570ce/application"
    )
    assert pv.source_created_at == datetime(2023, 3, 9, 17, 44, 0, 817000, tzinfo=UTC)
    assert pv.source_updated_at is None
    assert pv.description_html.startswith("<h1><strong>About Ramp</strong></h1>")


def test_normalize_trims_title_and_handles_no_compensation(boards: dict[str, Board]) -> None:
    rec = RawRecord(
        "x",
        0,
        {
            "id": "x",
            "title": " Security Engineer ",
            "descriptionHtml": "<p>a</p>",
            "location": "NYC",
            "employmentType": "PartTime",
        },
    )
    pv = get_source("ashby").normalize(rec, boards["ashby"])
    assert pv.title == "Security Engineer"
    assert pv.compensation is None and pv.employment_type == "part_time"
    assert pv.locations == ("NYC",)
