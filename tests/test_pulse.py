"""`pulse`: the delta an hourly agent reads, and the watermark that bounds it.

Real Postgres and a real archive, like the rest of the CLI suite — what pulse
has to get right is exactly which events a watermark does and does not return,
which is a property of the SQL, not of a mock.
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
from jobhunter.config import Settings
from jobhunter.cursors import Watermark, read_cursor
from jobhunter.models import Board
from jobhunter.pulse import build_pulse, profile_summary
from jobhunter.store.lifecycle import Ingestor
from jobhunter.timeutil import parse_iso
from tests.conftest import TEST_DSN
from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry

runner = CliRunner()

DAY0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
DAY1 = DAY0 + timedelta(days=1)
DAY2 = DAY0 + timedelta(days=2)
NOW = DAY2 + timedelta(hours=1)
ISO0, ISO1, ISO2 = "2026-08-18T06:00:00Z", "2026-08-19T06:00:00Z", "2026-08-20T06:00:00Z"


@pytest.fixture
def penv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pg: psycopg.Connection[dict[str, Any]],
) -> Path:
    """Three ingest days on one board — three opens at DAY0, two changes plus an
    open at DAY1, one close at DAY2 — plus a failed fetch of a second board so
    the attention block has something to report."""
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
    ing.ingest(make_manifest(
        store, "lever", "palantir", DAY2, None,
        registry_revision=rev, transport="http_error", http_status=500))
    pg.commit()
    (tmp_path / "companies.toml").write_text(
        '[[boards]]\ncompany="Ramp"\nsource="ashby"\nboard="ramp"\n'
    )
    monkeypatch.setenv("JOB_HUNTER_ARCHIVE_URL", f"file://{tmp_path / 'archive'}")
    monkeypatch.setenv("JOB_HUNTER_REGISTRY", str(tmp_path / "companies.toml"))
    monkeypatch.setenv("JOB_HUNTER_DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("JOB_HUNTER_STATE_DIR", str(tmp_path / "state"))
    row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert row is not None
    monkeypatch.setattr(cli, "_schema", str(row["s"]))
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    return tmp_path


def _event_ids(pg: psycopg.Connection[dict[str, Any]], at: datetime) -> list[int]:
    return [
        int(r["event_id"])
        for r in pg.execute(
            "SELECT event_id FROM posting_events WHERE at = %s ORDER BY event_id", (at,)
        ).fetchall()
    ]


def _build(
    pg: psycopg.Connection[dict[str, Any]],
    *,
    wm: Watermark | None = None,
    limit: int = 200,
    boards: tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], Watermark | None]:
    return build_pulse(pg, Settings.load(), wm=wm, limit=limit, boards=boards, now=NOW)


# -- profile_summary --------------------------------------------------------


def _fixture_profile() -> dict[str, Any]:
    record = json.loads(
        (Path(__file__).parent / "l2" / "fixtures" / "anthropic.extraction.json").read_text()
    )
    return {"facts": record["facts"], "demand_profile": record["demand_profile"]}


def test_profile_summary_is_the_documented_shape() -> None:
    summary = profile_summary(_fixture_profile())
    assert set(summary) == {"areas", "mentions", "facts"}
    assert summary["areas"] == [{"name": "Full-stack product engineering", "kind": "technical",
                                 "importance": "required", "level": None}]
    assert summary["mentions"] == ["Python", "React", "TypeScript"]
    assert set(summary["facts"]) == {"compensation", "experience_months", "deadline"}
    assert summary["facts"]["compensation"] == [
        {"min": 300000, "max": 405000, "currency": "USD", "period": None}
    ]


def test_profile_summary_caps_mentions_and_dedupes_across_areas() -> None:
    profile = {
        "facts": {},
        "demand_profile": {"areas": [
            {"name": "a", "kind": "technical", "importance": "required", "level": None,
             "mentions": [f"m{i}" for i in range(6)]},
            {"name": "b", "kind": "technical", "importance": "preferred", "level": "senior",
             "mentions": ["m0", "m1", *[f"n{i}" for i in range(6)]]},
        ]},
    }
    summary = profile_summary(profile)
    assert summary["mentions"] == ["m0", "m1", "m2", "m3", "m4", "m5", "n0", "n1"]
    assert summary["facts"] == {"compensation": [], "experience_months": None, "deadline": None}


# -- build_pulse ------------------------------------------------------------


def test_first_run_covers_the_last_day_and_says_so(
    penv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    payload, wm = _build(pg)
    assert payload["first_run"] is True
    assert payload["window"] == {"from": "2026-08-19T07:00:00Z", "to": "2026-08-20T07:00:00Z"}
    assert [(e["kind"], e["uid"]) for e in payload["events"]] == [("closed", "ab:ramp:y")]
    closed = payload["events"][0]
    assert closed["closed_between"] == [ISO1, ISO2] and closed["at"] == ISO2
    assert closed["board"] == "ashby:ramp" and closed["title"] == "Data Scientist II"
    assert "profile" not in closed  # a close carries no demand to report
    assert payload["_truncated"] is False
    assert wm is not None and parse_iso(wm.at) == DAY2
    assert wm.event_ids_at == tuple(_event_ids(pg, DAY2))


def test_watermark_returns_the_same_instant_minus_what_was_reported(
    penv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    ids0 = _event_ids(pg, DAY0)
    assert len(ids0) == 3
    payload, wm = _build(pg, wm=Watermark(DAY0.isoformat(), tuple(ids0[:2])))
    ids = [e["event_id"] for e in payload["events"]]
    assert ids[0] == ids0[2] and ids0[0] not in ids and ids0[1] not in ids
    assert len(ids) == 5  # the third open at DAY0, three DAY1 events, one DAY2 close
    assert payload["first_run"] is False
    assert payload["window"]["from"] == ISO0
    assert wm is not None and parse_iso(wm.at) == DAY2


def test_truncation_advances_only_to_the_last_emitted_event(
    penv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    ids0, ids1 = _event_ids(pg, DAY0), _event_ids(pg, DAY1)
    start = Watermark((DAY0 - timedelta(seconds=1)).isoformat(), ())
    page1, wm1 = _build(pg, wm=start, limit=2)
    assert [e["event_id"] for e in page1["events"]] == ids0[:2]
    assert page1["_truncated"] is True
    assert wm1 is not None and parse_iso(wm1.at) == DAY0
    assert wm1.event_ids_at == tuple(ids0[:2])  # not the whole instant, only what was emitted
    page2, wm2 = _build(pg, wm=wm1, limit=2)
    assert [e["event_id"] for e in page2["events"]] == [ids0[2], ids1[0]]
    assert wm2 is not None and parse_iso(wm2.at) == DAY1 and wm2.event_ids_at == (ids1[0],)


def test_a_second_page_at_the_same_instant_keeps_the_earlier_ids(
    penv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    """Two calls that both stop inside DAY0 must not re-report page one: the new
    watermark carries the ids from the old one when the instant has not moved."""
    ids0 = _event_ids(pg, DAY0)
    _, wm1 = _build(pg, wm=Watermark((DAY0 - timedelta(seconds=1)).isoformat(), ()), limit=2)
    assert wm1 is not None
    page2, wm2 = _build(pg, wm=wm1, limit=1)
    assert [e["event_id"] for e in page2["events"]] == [ids0[2]]
    assert wm2 is not None and wm2.event_ids_at == tuple(ids0)
    page3, _ = _build(pg, wm=wm2, limit=2)
    assert all(e["event_id"] not in ids0 for e in page3["events"])


def test_opened_and_changed_events_carry_a_validated_profile_summary(
    penv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.runner import SCHEMA_VERSION
    from jobhunter.l2.state import DerivedState
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.store import extraction

    row = pg.execute(
        "SELECT d.document_hash FROM postings p"
        " JOIN documents d ON d.version_hash = p.current_version_hash"
        " WHERE p.uid = 'ab:ramp:x'"
    ).fetchone()
    assert row is not None
    dh = str(row["document_hash"])
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION, validator_version=VALIDATOR_VERSION,
        state=DerivedState("validated", None), profile=_fixture_profile(),
        updated_at="2026-08-27T00:00:00Z",
    )
    pg.commit()
    payload, _ = _build(pg, wm=Watermark((DAY1 - timedelta(seconds=1)).isoformat(), ()))
    by_uid = {(e["uid"], e["kind"]): e for e in payload["events"]}
    changed = by_uid[("ab:ramp:x", "changed")]
    assert changed["document_hash"] == dh
    assert set(changed["profile"]) == {"areas", "mentions", "facts"}
    assert changed["profile"]["mentions"] == ["Python", "React", "TypeScript"]
    # y changed too, but nothing extracted it: the key is there, the value is honest
    assert by_uid[("ab:ramp:y", "changed")]["profile"] is None
    assert by_uid[("ab:ramp:w", "opened")]["profile"] is None
    assert "profile" not in by_uid[("ab:ramp:y", "closed")]


def test_attention_reports_unhealthy_boards_and_the_extraction_block(
    penv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    payload, _ = _build(pg)
    attention = payload["attention"]
    assert [b["board"] for b in attention["unhealthy_boards"]] == ["lever:palantir"]
    assert attention["unhealthy_boards"][0]["health"] == "error"
    assert attention["extraction"]["queue_depth"] >= 1
    assert "spend_today_usd" in attention["extraction"]


def test_boards_filter_narrows_the_report_but_still_advances(
    penv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    """A board filter must never livelock the cursor: the events it drops were
    still read, so the watermark passes them."""
    payload, wm = _build(pg, wm=Watermark(DAY0.isoformat(), ()), boards=("lever:palantir",))
    assert payload["events"] == []
    assert wm is not None and parse_iso(wm.at) == DAY2
    assert [b["board"] for b in payload["attention"]["unhealthy_boards"]] == ["lever:palantir"]
    ramp, _ = _build(pg, wm=Watermark(DAY0.isoformat(), ()), boards=("ashby:ramp",))
    assert len(ramp["events"]) == 7
    assert ramp["attention"]["unhealthy_boards"] == []


def test_no_events_leaves_the_watermark_where_it_was(
    penv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    payload, wm = _build(pg, wm=Watermark(NOW.isoformat(), ()))
    assert payload["events"] == [] and wm is None


# -- the command ------------------------------------------------------------


def _pulse(args: list[str], code: int = 0) -> Any:
    r = runner.invoke(cli.app, ["pulse", *args, "-o", "json"])
    assert r.exit_code == code, r.stdout + r.stderr
    return json.loads(r.stdout)


def test_peek_reports_without_advancing_then_the_cursor_moves(penv: Path) -> None:
    state = penv / "state"
    first = _pulse(["--peek"])
    assert first["meta"]["first_run"] is True and first["meta"]["cursor"] == "default"
    assert [e["uid"] for e in first["data"]["events"]] == ["ab:ramp:y"]
    assert _pulse(["--peek"])["data"]["events"] == first["data"]["events"]
    assert read_cursor(state, "default") is None
    body = _pulse([])
    assert [e["uid"] for e in body["data"]["events"]] == ["ab:ramp:y"]
    assert read_cursor(state, "default") is not None
    again = _pulse([])
    assert again["data"]["events"] == [] and again["meta"]["first_run"] is False
    assert again["meta"]["count"] == 0


def test_named_cursors_are_independent(penv: Path) -> None:
    _pulse(["--cursor", "hourly"])
    assert _pulse(["--cursor", "hourly"])["data"]["events"] == []
    assert len(_pulse(["--cursor", "other"])["data"]["events"]) == 1


def test_a_failed_cursor_write_re_reports_next_time(
    penv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("read-only file system")

    real = cli.write_cursor
    monkeypatch.setattr(cli, "write_cursor", boom)
    body = _pulse([], code=5)  # the data was delivered; the cursor was not advanced
    assert body["ok"] is True and [e["uid"] for e in body["data"]["events"]] == ["ab:ramp:y"]
    assert read_cursor(penv / "state", "default") is None
    monkeypatch.setattr(cli, "write_cursor", real)
    assert [e["uid"] for e in _pulse([])["data"]["events"]] == ["ab:ramp:y"]


def test_since_bypasses_the_cursor_entirely(penv: Path) -> None:
    body = _pulse(["--since", ISO0])
    assert len(body["data"]["events"]) == 7 and body["data"]["first_run"] is False
    assert body["meta"]["cursor"] is None
    assert read_cursor(penv / "state", "default") is None
    assert len(_pulse(["--since", "3d"])["data"]["events"]) == 7
    bad = _pulse(["--since", "yesterday"], code=2)
    assert bad["error"]["kind"] == "usage"


def test_limit_truncates_and_says_to_call_again(penv: Path) -> None:
    body = _pulse(["--since", ISO0, "--limit", "3"])
    assert body["meta"]["truncated"] is True and body["meta"]["count"] == 3
    assert len(body["data"]["events"]) == 3


def test_bad_board_shape_is_a_usage_error(penv: Path) -> None:
    body = _pulse(["--boards", "ashby:ramp,palantir"], code=2)
    assert "source:board" in body["error"]["message"]


def test_human_mode_keeps_data_on_stdout_and_hints_on_stderr(penv: Path) -> None:
    r = runner.invoke(cli.app, ["pulse", "--peek", "-o", "table"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "closed" in r.stdout and "Data Scientist II" in r.stdout
    assert "lever:palantir" in r.stdout
    assert "q posting" in r.stderr and "q posting" not in r.stdout
