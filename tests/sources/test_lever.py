from datetime import UTC, datetime

import pytest

from jobhunter.models import Board, Compensation
from jobhunter.sources import get_source
from jobhunter.sources.base import EnvelopeError
from tests.conftest import fixture_bytes


def test_url(boards: dict[str, Board]) -> None:
    assert get_source("lever").url(boards["lever"]) == (
        "https://api.lever.co/v0/postings/palantir?mode=json"
    )


def test_parse_bare_array() -> None:
    recs = list(get_source("lever").parse(fixture_bytes("lever_board.json")))
    assert [r.source_id for r in recs] == [
        "ac978161-6f46-4f6b-ad9e-a258e642751c",
        "0d1e2f3a-0000-4000-8000-000000000002",
    ]


def test_parse_empty_array_is_valid_envelope() -> None:
    assert list(get_source("lever").parse(b"[]")) == []


@pytest.mark.parametrize("body", [b"{}", b"<html>", b'{"jobs":[]}'])
def test_parse_bad_envelope(body: bytes) -> None:
    with pytest.raises(EnvelopeError):
        list(get_source("lever").parse(body))


def test_normalize_first_record(boards: dict[str, Board]) -> None:
    src = get_source("lever")
    rec = list(src.parse(fixture_bytes("lever_board.json")))[0]
    pv = src.normalize(rec, boards["lever"])
    assert pv.uid == "lv:palantir:ac978161-6f46-4f6b-ad9e-a258e642751c"
    assert pv.title == "Administrative Business Partner"
    assert pv.company == "Palantir"  # from the registry, Lever has no company field
    assert pv.locations == ("London, United Kingdom",)
    assert pv.workplace_type == "hybrid" and pv.is_remote is False
    assert pv.department == "Administrative" and pv.team is None
    assert pv.employment_type == "full_time"
    assert pv.compensation is None
    assert pv.source_created_at == datetime(2024, 3, 25, 21, 50, 16, 463000, tzinfo=UTC)
    assert pv.source_updated_at is None
    assert pv.description_html == (
        "<div><strong>A World-Changing Company</strong></div>"
        "<div><strong>The Role</strong></div>"
        "<h3>What We Value</h3><ul>\n<li>Ability to adjust quickly</li></ul>"
        "<div><strong>Life at Palantir</strong></div>"
    )


def test_normalize_second_record_remote_and_salary(boards: dict[str, Board]) -> None:
    src = get_source("lever")
    rec = list(src.parse(fixture_bytes("lever_board.json")))[1]
    pv = src.normalize(rec, boards["lever"])
    assert pv.title == "Remote Engineer"
    assert pv.locations == ("Remote",)  # falls back to categories.location
    assert pv.workplace_type == "remote" and pv.is_remote is True
    assert pv.employment_type == "contract"
    assert pv.compensation == Compensation(100000, 150000, "USD", "per-year-salary")
    assert pv.description_html == "<p>Hi</p>"
