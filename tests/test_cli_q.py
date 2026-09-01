"""The `q` namespace: envelope shape, bounds and cursors, field selection.

Real Postgres and a real local archive, like every other CLI test — the point
of these verbs is the SQL they run, so a mocked store would prove nothing.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from jobhunter import cli
from jobhunter.archive.local import LocalFS
from jobhunter.models import Board
from jobhunter.store import queries
from jobhunter.store.lifecycle import Ingestor
from tests.conftest import TEST_DSN
from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry

runner = CliRunner()

DAY0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
DAY1 = DAY0 + timedelta(days=1)
DAY2 = DAY0 + timedelta(days=2)
ISO0, ISO1, ISO2 = "2026-08-18T06:00:00Z", "2026-08-19T06:00:00Z", "2026-08-20T06:00:00Z"


@pytest.fixture
def qenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pg: psycopg.Connection[dict[str, Any]],
) -> Path:
    """Three ingest days on one board: three opens at DAY0, two changes plus an
    open at DAY1, one close at DAY2 — the corpus `tests/store/test_queries.py`
    reads, seen through the CLI."""
    store = LocalFS(tmp_path / "archive")
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    ing = Ingestor(pg, store)
    ing.ingest(make_manifest(
        store, "ashby", "ramp", DAY0,
        board_payload("ashby", [
            ab_record("x", "Rust Engineer", "<p>x</p>"),
            ab_record("y", "Data Scientist", "<p>y</p>"),
            ab_record("z", "Designer", "<p>z</p>"),
        ]),
        registry_revision=rev))
    ing.ingest(make_manifest(
        store, "ashby", "ramp", DAY1,
        board_payload("ashby", [
            ab_record("x", "Rust Engineer II", "<p>x2</p>"),
            ab_record("y", "Data Scientist II", "<p>y2</p>"),
            ab_record("z", "Designer", "<p>z</p>"),
            ab_record("w", "Recruiter", "<p>w</p>"),
        ]),
        registry_revision=rev))
    ing.ingest(make_manifest(
        store, "ashby", "ramp", DAY2,
        board_payload("ashby", [
            ab_record("x", "Rust Engineer II", "<p>x2</p>"),
            ab_record("z", "Designer", "<p>z</p>"),
            ab_record("w", "Recruiter", "<p>w</p>"),
        ]),
        registry_revision=rev))
    pg.commit()
    (tmp_path / "companies.toml").write_text(
        '[[boards]]\ncompany="Ramp"\nsource="ashby"\nboard="ramp"\n'
    )
    monkeypatch.setenv("JOB_HUNTER_ARCHIVE_URL", f"file://{tmp_path / 'archive'}")
    monkeypatch.setenv("JOB_HUNTER_REGISTRY", str(tmp_path / "companies.toml"))
    monkeypatch.setenv("JOB_HUNTER_DATABASE_URL", TEST_DSN)
    row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert row is not None
    monkeypatch.setattr(cli, "_schema", str(row["s"]))
    monkeypatch.setattr(cli, "_now", lambda: DAY2 + timedelta(hours=1))
    return tmp_path


def _data(args: list[str], code: int = 0) -> Any:
    r = runner.invoke(cli.app, [*args, "-o", "json"])
    assert r.exit_code == code, r.stdout + r.stderr
    return json.loads(r.stdout)


def _doc_hash() -> str:
    return str(_data(["q", "posting", "ab:ramp:x"])["data"]["document_hash"])


def test_q_postings_envelope(qenv: Path) -> None:
    body = _data(["q", "postings"])
    assert body["ok"] is True
    assert body["meta"]["count"] == 4 and body["meta"]["truncated"] is False
    rows = {r["uid"]: r for r in body["data"]}
    assert [r["uid"] for r in body["data"]] == [
        "ab:ramp:w", "ab:ramp:z", "ab:ramp:y", "ab:ramp:x",
    ]
    w = rows["ab:ramp:w"]
    assert w["title"] == "Recruiter" and w["company"] == "Ramp" and w["status"] == "open"
    # the board is printed the way --board accepts it back (spec §2, identifiers)
    assert w["board"] == "ashby:ramp" and "source" not in w and w["version_count"] == 1
    assert w["first_seen_at"] == ISO1 and w["last_seen_at"] == ISO2
    assert w["closed_between"] is None
    assert "closed_lower_at" not in w and "closed_upper_at" not in w
    assert rows["ab:ramp:y"]["closed_between"] == [ISO1, ISO2]
    assert "q posting" in body["meta"]["hint"]


def test_q_postings_human_table_and_stderr_hint(qenv: Path) -> None:
    r = runner.invoke(cli.app, ["q", "postings", "-o", "table"])
    assert r.exit_code == 0, r.stdout
    assert "ab:ramp:w" in r.stdout and "Recruiter" in r.stdout
    assert "q posting" in r.stderr and "q posting" not in r.stdout


def test_q_postings_status_is_enumerated(qenv: Path) -> None:
    body = _data(["q", "postings", "--status", "bogus"], code=2)
    assert body["ok"] is False and body["error"]["kind"] == "usage"
    assert body["error"]["valid"] == ["open", "closed"]
    assert [r["uid"] for r in _data(["q", "postings", "--status", "closed"])["data"]] == [
        "ab:ramp:y"
    ]


def test_q_postings_limit_is_clamped_to_the_hard_cap(
    qenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = queries.postings_page

    def spy(conn: Any, **kw: Any) -> Any:
        seen.update(kw)
        return real(conn, **kw)

    monkeypatch.setattr(queries, "postings_page", spy)
    _data(["q", "postings", "--limit", "9999"])
    assert seen["limit"] == 500
    _data(["q", "postings", "--limit", "0"])
    assert seen["limit"] == 1


def test_q_postings_truncation_pages_with_after(qenv: Path) -> None:
    first = _data(["q", "postings", "--limit", "2"])
    assert first["meta"]["truncated"] is True and first["meta"]["count"] == 2
    cursor = first["meta"]["next_cursor"]
    assert cursor
    second = _data(["q", "postings", "--limit", "2", "--after", cursor])
    assert second["meta"]["truncated"] is False
    assert second["meta"].get("next_cursor") is None
    page1 = [r["uid"] for r in first["data"]]
    page2 = [r["uid"] for r in second["data"]]
    assert set(page1).isdisjoint(page2)
    assert page1 + page2 == ["ab:ramp:w", "ab:ramp:z", "ab:ramp:y", "ab:ramp:x"]
    hand_made = _data(["q", "postings", "--after", "page-2-please"], code=2)
    assert hand_made["error"]["kind"] == "usage"


def test_q_postings_fields_selection(qenv: Path) -> None:
    body = _data(["q", "postings", "--fields", "uid,title"])
    assert all(set(r) == {"uid", "title"} for r in body["data"])
    bad = _data(["q", "postings", "--fields", "uid,nope"], code=2)
    assert "nope" in bad["error"]["message"] and "uid" in bad["error"]["valid"]


def test_q_postings_search_and_board_filters(qenv: Path) -> None:
    assert [r["uid"] for r in _data(["q", "postings", "--search", "rUsT"])["data"]] == [
        "ab:ramp:x"
    ]
    assert _data(["q", "postings", "--board", "ashby:ramp"])["meta"]["count"] == 4
    assert _data(["q", "postings", "--board", "greenhouse:x"])["meta"]["count"] == 0
    body = _data(["q", "postings", "--board", "ramp"], code=2)
    assert "source:board" in body["error"]["message"]
    assert [r["uid"] for r in _data(["q", "postings", "--since", "36h"])["data"]] == [
        "ab:ramp:w"
    ]


def test_q_posting_detail_and_unknown_uid(qenv: Path) -> None:
    body = _data(["q", "posting", "ab:ramp:x"])
    d = body["data"]
    assert d["status"] == "open" and d["version_count"] == 2 and d["board"] == "ashby:ramp"
    assert d["first_seen_at"] == ISO0 and d["last_seen_at"] == ISO2
    assert [(v["title"], v["at"]) for v in d["versions"]] == [
        ("Rust Engineer", ISO0), ("Rust Engineer II", ISO1),
    ]
    assert [(e["kind"], e["at"]) for e in d["events"]] == [
        ("opened", ISO0), ("changed", ISO1),
    ]
    assert len(d["document_hash"]) == 64
    assert d["document_hash"][:12] in body["meta"]["hint"]
    closed = _data(["q", "posting", "ab:ramp:y"])["data"]
    assert closed["status"] == "closed" and closed["closed_between"] == [ISO1, ISO2]
    miss = _data(["q", "posting", "ab:ramp:nope"], code=4)
    assert miss["error"]["kind"] == "not_found"


def test_q_events_filters_kinds_and_pages(qenv: Path) -> None:
    body = _data(["q", "events", "--kind", "closed"])
    assert [e["uid"] for e in body["data"]] == ["ab:ramp:y"]
    e = body["data"][0]
    assert e["title"] == "Data Scientist II" and e["board"] == "ashby:ramp"
    assert e["at"] == ISO2 and e["closed_between"] == [ISO1, ISO2]
    bad = _data(["q", "events", "--kind", "vanished"], code=2)
    assert bad["error"]["valid"] == ["opened", "changed", "closed", "reopened"]
    assert _data(["q", "events", "--since", "1d"])["meta"]["count"] == 1
    assert _data(["q", "events", "--uid", "ab:ramp:w"])["meta"]["count"] == 1
    assert _data(["q", "events", "--board", "lever:palantir"])["meta"]["count"] == 0
    first = _data(["q", "events", "--limit", "2"])
    assert first["meta"]["truncated"] is True
    second = _data(["q", "events", "--limit", "2", "--after", first["meta"]["next_cursor"]])
    ids1 = [x["event_id"] for x in first["data"]]
    ids2 = [x["event_id"] for x in second["data"]]
    assert set(ids1).isdisjoint(ids2) and min(ids2) > max(ids1)
    assert _data(["q", "events", "--after", "not-an-id"], code=2)["error"]["kind"] == "usage"


def test_q_boards(qenv: Path) -> None:
    rows = _data(["q", "boards"])["data"]
    assert rows == [{"board": "ashby:ramp", "health": "ok", "open": 3, "error": None,
                     "started_at": ISO2}]
    assert _data(["q", "boards", "--unhealthy"])["data"] == []


def test_q_document_slice_and_prefix_resolution(qenv: Path) -> None:
    dh = _doc_hash()
    body = _data(["q", "document", dh[:12]])
    assert body["data"]["document_hash"] == dh
    assert body["data"]["markdown"] == "x2"
    sliced = _data(["q", "document", dh[:12], "--slice", "0:1"])
    assert sliced["data"]["markdown"] == "x"
    assert _data(["q", "document", dh[:12], "--slice", "1:x"], code=2)["error"]["kind"] == "usage"
    assert _data(["q", "document", "deadbeef"], code=4)["error"]["kind"] == "not_found"


def test_q_document_ambiguous_prefix_teaches_the_fix(
    qenv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    pg.execute(
        "INSERT INTO documents (version_hash, normalizer_version, document_hash, markdown)"
        " VALUES ('vq1','md/1','abcd0001','a'), ('vq2','md/1','abcd0002','b')"
    )
    pg.commit()
    body = _data(["q", "document", "abcd"], code=4)
    assert "ambiguous" in body["error"]["message"]
    assert "lengthen" in body["error"]["hint"]


def _seed_profile(pg: psycopg.Connection[dict[str, Any]], dh: str) -> dict[str, Any]:
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.runner import SCHEMA_VERSION
    from jobhunter.l2.state import DerivedState
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.store import extraction

    record = json.loads(
        (Path(__file__).parent / "l2" / "fixtures" / "anthropic.extraction.json").read_text()
    )
    profile = {"facts": record["facts"], "demand_profile": record["demand_profile"]}
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION, validator_version=VALIDATOR_VERSION,
        state=DerivedState("validated", None), profile=profile,
        updated_at="2026-08-27T00:00:00Z",
    )
    pg.commit()
    return profile


def test_q_profile_summary_then_full(
    qenv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    dh = _doc_hash()
    profile = _seed_profile(pg, dh)
    body = _data(["q", "profile", "--doc", dh[:12]])
    data = body["data"]
    assert data["document_hash"] == dh and data["status"] == "validated"
    summary = data["profile"]
    assert set(summary) == {"areas", "mentions", "facts"}
    assert summary["areas"] == [{"name": "Full-stack product engineering", "kind": "technical",
                                 "importance": "required", "level": None}]
    assert summary["mentions"] == ["Python", "React", "TypeScript"]
    assert summary["facts"]["compensation"] == [
        {"min": 300000, "max": 405000, "currency": "USD", "period": None}
    ]
    assert summary["facts"]["experience_months"] is None
    assert summary["facts"]["deadline"] is None
    assert "--full" in body["meta"]["hint"]
    full = _data(["q", "profile", "--doc", dh[:12], "--full"])["data"]
    assert full["profile"] == profile  # verbatim, quotes and spans included


def test_q_profile_without_an_extraction_is_not_found(qenv: Path) -> None:
    dh = _doc_hash()
    body = _data(["q", "profile", "--doc", dh[:12]], code=4)
    assert body["error"]["kind"] == "not_found"
    assert "extract run" in body["error"]["hint"]


def test_q_profile_unvalidated_row_says_so(
    qenv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.runner import SCHEMA_VERSION
    from jobhunter.l2.state import DerivedState
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.store import extraction

    dh = _doc_hash()
    _seed_profile(pg, dh)
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION, validator_version=VALIDATOR_VERSION,
        state=DerivedState("needs_review", None), profile=None,
        updated_at="2026-08-28T00:00:00Z",
    )
    pg.commit()
    body = _data(["q", "profile", "--doc", dh[:12]], code=4)
    assert "needs_review" in body["error"]["message"]
    assert "review show" in body["error"]["hint"]


def test_q_stdout_stays_one_json_object_on_every_error_path(qenv: Path) -> None:
    for args in (["q", "postings", "--status", "bogus"], ["q", "posting", "nope"],
                 ["q", "document", "zz"], ["q", "profile", "--doc", "deadbeef"]):
        r = runner.invoke(cli.app, [*args, "-o", "json"])
        assert r.exit_code in (2, 4), (args, r.exit_code)
        assert json.loads(r.stdout)["ok"] is False
