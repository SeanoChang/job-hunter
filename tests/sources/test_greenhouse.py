from datetime import UTC, datetime

import pytest

from jobhunter.models import Board, RawRecord
from jobhunter.sources import get_source
from jobhunter.sources.base import EnvelopeError, NormalizeError
from tests.conftest import fixture_bytes


def test_url(boards: dict[str, Board]) -> None:
    assert get_source("greenhouse").url(boards["greenhouse"]) == (
        "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true"
    )


def test_parse_yields_records_with_string_ids() -> None:
    recs = list(get_source("greenhouse").parse(fixture_bytes("greenhouse_board.json")))
    assert len(recs) == 1
    assert recs[0].source_id == "5186067008" and recs[0].index == 0


def test_parse_id_zero_is_valid_and_missing_id_is_none() -> None:
    body = b'{"jobs":[{"id":0,"title":"a"},{"title":"b"},"junk"],"meta":{"total":3}}'
    recs = list(get_source("greenhouse").parse(body))
    assert [r.source_id for r in recs] == ["0", None, None]


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"meta":{}}', b'{"jobs":{}}'])
def test_parse_bad_envelope(body: bytes) -> None:
    with pytest.raises(EnvelopeError):
        list(get_source("greenhouse").parse(body))


def test_normalize_real_record(boards: dict[str, Board]) -> None:
    src = get_source("greenhouse")
    rec = next(iter(src.parse(fixture_bytes("greenhouse_board.json"))))
    pv = src.normalize(rec, boards["greenhouse"])
    assert pv.uid == "gh:anthropic:5186067008"
    assert pv.title == "Full-Stack Software Engineer, Reinforcement Learning"
    assert pv.company == "Anthropic"
    assert pv.locations == (
        "San Francisco, CA | New York City, NY",
        "San Francisco, California, United States",
    )
    assert pv.department == "AI Research & Engineering"
    assert pv.workplace_type is None and pv.is_remote is None
    assert pv.compensation is None
    assert pv.url == "https://job-boards.greenhouse.io/anthropic/jobs/5186067008"
    assert pv.source_created_at == datetime(2026, 4, 14, 10, 0, 34, tzinfo=UTC)
    assert pv.source_updated_at == datetime(2026, 8, 3, 22, 25, 22, tzinfo=UTC)
    assert pv.description_html.startswith('<div class="content-intro"><h2><strong>About Anthropic')


def test_normalize_missing_title_raises(boards: dict[str, Board]) -> None:
    rec = RawRecord("1", 0, {"id": 1, "content": "x"})
    with pytest.raises(NormalizeError):
        get_source("greenhouse").normalize(rec, boards["greenhouse"])


def test_get_source_unknown() -> None:
    with pytest.raises(KeyError):
        get_source("workday")
