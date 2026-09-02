"""`views.py`: the payload assembly the CLI and the MCP wrapper share.

Every test here is a parity test — call the view against the fixture corpus,
invoke the `q` verb that used to assemble the same payload inside its command
body, and demand the two `data` payloads be equal. That equality is the whole
claim of the refactor: one payload with two faces, not two payloads that drift.

The envelope serialises through `json.dumps(default=str)`, so the comparison
goes through the same conversion — a view may hand back a datetime the CLI
would have stringified, and that would be a real difference, not a formatting
one.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jobhunter import views
from jobhunter.config import Settings
from jobhunter.timeutil import parse_iso
from tests.test_cli_q import (  # noqa: F401  -- qenv is a fixture, used by name
    DAY2,
    ISO0,
    _data,
    _doc_hash,
    _seed_profile,
    qenv,
)

NOW = DAY2 + timedelta(hours=1)  # the clock `qenv` pins the CLI to


@pytest.fixture
def corpus(qenv: Path) -> Path:  # noqa: F811  -- the imported fixture, under a free name
    """`test_cli_q`'s three ingest days, requested under a name the tests below
    do not shadow — one corpus, read through both faces."""
    return qenv


def _as_json(data: Any) -> Any:
    return json.loads(json.dumps(data, default=str))


def test_postings_view_is_what_q_postings_emits(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    page = views.postings_view(pg)
    assert _as_json(page.data) == _data(["q", "postings"])["data"]
    assert page.truncated is False and page.next_cursor is None
    first = views.postings_view(pg, limit=2)
    body = _data(["q", "postings", "--limit", "2"])
    assert _as_json(first.data) == body["data"]
    assert first.truncated is True and first.next_cursor == body["meta"]["next_cursor"]
    # the filters reach the query, not only the shaping
    assert [r["uid"] for r in views.postings_view(pg, status="closed").rows()] == ["ab:ramp:y"]
    assert views.postings_view(pg, source="ashby", board="ramp").rows() != []
    assert views.postings_view(pg, search="rUsT").rows()[0]["uid"] == "ab:ramp:x"


def test_posting_view_matches_and_reports_a_miss_as_none(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    page = views.posting_view(pg, "ab:ramp:x")
    assert page is not None
    assert _as_json(page.data) == _data(["q", "posting", "ab:ramp:x"])["data"]
    closed = views.posting_view(pg, "ab:ramp:y")
    assert closed is not None
    assert _as_json(closed.data) == _data(["q", "posting", "ab:ramp:y"])["data"]
    assert views.posting_view(pg, "ab:ramp:nope") is None


def test_events_view_is_what_q_events_emits(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    assert _as_json(views.events_view(pg).data) == _data(["q", "events"])["data"]
    assert _as_json(views.events_view(pg, kinds=("closed",)).data) == _data(
        ["q", "events", "--kind", "closed"])["data"]
    first = views.events_view(pg, limit=2)
    body = _data(["q", "events", "--limit", "2"])
    assert _as_json(first.data) == body["data"]
    assert first.truncated is True and first.next_cursor == body["meta"]["next_cursor"]
    after = views.events_view(pg, limit=2, after_event_id=int(first.next_cursor or 0))
    assert _as_json(after.data) == _data(
        ["q", "events", "--limit", "2", "--after", body["meta"]["next_cursor"]])["data"]
    assert views.events_view(pg, uid="ab:ramp:w").rows() != []
    assert views.events_view(pg, source="lever", board="palantir").rows() == []


def test_boards_view_is_what_q_boards_emits(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    page = views.boards_view(pg)
    assert _as_json(page.data) == _data(["q", "boards"])["data"]
    assert page.truncated is False
    assert views.boards_view(pg, unhealthy_only=True).rows() == []


def test_document_view_slices_and_reports_a_miss_as_none(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    dh = _doc_hash()
    page = views.document_view(pg, dh)
    assert page is not None
    assert _as_json(page.data) == _data(["q", "document", dh[:12]])["data"]
    sliced = views.document_view(pg, dh, slice_="0:1")
    assert sliced is not None
    assert _as_json(sliced.data) == _data(["q", "document", dh[:12], "--slice", "0:1"])["data"]
    assert views.document_view(pg, "0" * 64) is None
    assert views.parse_slice("0:1") == (0, 1)
    assert views.parse_slice("500:") == (500, None) and views.parse_slice(":500") == (None, 500)
    for bad in ("1:x", "nope"):  # a usage error the caller renders; never a database one
        with pytest.raises(ValueError):
            views.parse_slice(bad)


def test_profile_view_matches_summary_and_full(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    dh = _doc_hash()
    profile = _seed_profile(pg, dh)
    page = views.profile_view(pg, dh)
    assert page is not None
    assert _as_json(page.data) == _data(["q", "profile", "--doc", dh[:12]])["data"]
    full = views.profile_view(pg, dh, full=True)
    assert full is not None
    assert _as_json(full.data) == _data(["q", "profile", "--doc", dh[:12], "--full"])["data"]
    assert full.record()["profile"] == profile
    # the two reasons a profile is absent stay distinguishable: the row says which
    assert views.profile_view(pg, "0" * 64) is None
    assert views.profile_row(pg, "0" * 64) is None
    assert views.profile_row(pg, dh) is not None


def test_claims_view_is_what_q_claims_emits(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    dh = _doc_hash()
    _seed_profile(pg, dh)
    settings = Settings.load()
    page = views.claims_view(pg, settings, mention="python")
    assert _as_json(page.data) == _data(["q", "claims", "--mention", "python"])["data"]
    assert page.truncated is False
    assert views.claims_view(pg, settings, mention="Python", importance="required").rows() != []
    assert views.claims_view(pg, settings, mention="Python", importance="preferred").rows() == []
    assert views.claims_view(
        pg, settings, mention="Python", source="lever", board="palantir").rows() == []


def test_pulse_view_is_what_the_pulse_command_emits(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOB_HUNTER_STATE_DIR", str(corpus / "state"))  # no cursor on disk yet
    settings = Settings.load()
    page, wm = views.pulse_view(
        pg, settings, wm=None, since_iso=None, limit=200, boards=None, now=NOW
    )
    body = _data(["pulse", "--peek"])
    assert _as_json(page.data) == body["data"]
    assert page.truncated is body["meta"]["truncated"]
    assert page.record()["first_run"] is True
    assert wm is not None  # the watermark the caller stores once the payload is out
    # --since bypasses the watermark the same way through both faces
    since, _ = views.pulse_view(
        pg, settings, wm=None, since_iso=parse_iso(ISO0).isoformat(), limit=200,
        boards=None, now=NOW,
    )
    assert _as_json(since.data) == _data(["pulse", "--since", ISO0])["data"]
    assert since.record()["first_run"] is False
    bounded, _ = views.pulse_view(
        pg, settings, wm=None, since_iso=parse_iso(ISO0).isoformat(), limit=3,
        boards=None, now=NOW,
    )
    assert bounded.truncated is True and len(bounded.record()["events"]) == 3
    assert "_truncated" not in bounded.record()  # the flag is a field of the page, not the payload
