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


#: The bundle `_seed_v3_profile` seeds and reads under, and its tuple. Named
#: once so a test asserting what partition a row landed in tracks the registry
#: instead of re-spelling a version string that the cutover is going to move.
SEED_BUNDLE = "v2"


def _seed_tuple() -> tuple[str, str, str]:
    from jobhunter.l2.bundles import get_bundle

    b = get_bundle(SEED_BUNDLE)
    return b.prompt_version, b.schema_version, b.validator_version


SEED_TUPLE = _seed_tuple()


def _seed_v3_profile(
    pg: psycopg.Connection[dict[str, Any]],
    dh: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str = "needs_review",
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One settled schema-3 row, written the way `runner.settle` writes it: the
    served blob plus the mention projection derived from the same record, filed
    under the ACTIVE BUNDLE's tuple.

    Selecting a bundle is how a corpus partition is chosen — for the write path
    since `l2/bundles.py` landed, and for the read path since
    `views.active_tuple`. So the seam these tests pull on is the environment
    (`JOB_HUNTER_L2_BUNDLE`), never a module constant: patching
    `l2.runner.SCHEMA_VERSION` would make the read surface look scoped while
    leaving the thing that actually scopes it untouched, which is exactly the
    defect this helper now exists to pin.

    The fixture records are schema-3 SHAPED (assembled at schema 3, so their
    statements carry `section_heading`) while the registered v2 bundle is still
    a schema-2 registration on this branch. `SEED_TUPLE` is therefore what the
    row is keyed by and what callers assert against, and the two line up of
    their own accord once the bundle's schema version is bumped.

    `record` takes one of the sibling serving fixtures — the multi-kind one, the
    headingless one — so a caller can seed a shape the default record has not
    got without rebuilding the write path around it.
    """
    from jobhunter.l2.state import DerivedState
    from jobhunter.l2.v2 import serve
    from jobhunter.store import extraction
    from tests.l2.v2.conftest import make_serving_record

    monkeypatch.setenv("JOB_HUNTER_L2_BUNDLE", SEED_BUNDLE)
    prompt_version, schema_version, validator_version = SEED_TUPLE
    if record is None:
        record = make_serving_record(lifecycle=status)
    profile = serve.profile_of(record)
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", prompt_version=prompt_version,
        schema_version=schema_version, validator_version=validator_version,
        state=DerivedState(status, None), profile=profile,
        mentions=serve.mention_rows(record), updated_at="2026-09-22T00:00:00Z",
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


def test_q_profile_serves_a_needs_review_row_with_its_quality_note(
    qenv: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parsing contract v3 §4: a review row is a verified extraction with a note
    against it, so `q profile` answers from it — labelled `needs_review`, with
    the `sample_notes` the samples produced."""
    dh = _doc_hash()
    profile = _seed_v3_profile(pg, dh, monkeypatch)
    body = _data(["q", "profile", "--doc", dh[:12]])
    data = body["data"]
    assert data["status"] == "needs_review"
    assert data["quality"]["sample_notes"] == {
        "k": 3, "f1": 0.82, "aligned_pairs": 7, "splits": {"kind": 2}
    }
    full = _data(["q", "profile", "--doc", dh[:12], "--full"])["data"]
    assert full["profile"] == profile
    assert full["profile"]["quality"]["sample_notes"]["splits"] == {"kind": 2}


def test_q_profile_table_reads_schema_3_headings_not_verdicts(
    qenv: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-3 statement carries no importance and no proficiency, so the
    human table prints the section it sits under and the modal phrase it quotes
    — never a verdict the parser no longer issues."""
    dh = _doc_hash()
    _seed_v3_profile(pg, dh, monkeypatch)
    r = runner.invoke(cli.app, ["q", "profile", "--doc", dh[:12], "-o", "table"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "Bachelor's degree" in r.stdout and "Requirements" in r.stdout
    assert "CPA certification" in r.stdout and "Nice to have" in r.stdout
    assert '"required"' in r.stdout  # the posting's own modal phrase, quoted
    assert "contextual" not in r.stdout  # the no-verdict sentinel is never printed
    assert "needs_review" in r.stdout
    assert "kind=2" in r.stdout  # what the samples split on


def test_sample_notes_line_says_how_many_samples_never_arrived() -> None:
    """An incomplete cohort settles validated under contract v3 and records
    `requested`/`arrived`; the human table must print that, or a reader sees
    `k=3` and assumes three samples were compared."""
    from jobhunter.cli_q import _sample_notes_lines

    short = {"sample_notes": {"k": 3, "requested": 3, "arrived": 1, "f1": None,
                              "aligned_pairs": 0, "splits": {}}}
    (line,) = _sample_notes_lines(short)
    assert "1 of 3 arrived" in line and "splits: none" in line
    full = {"sample_notes": {"k": 3, "f1": 1.0, "aligned_pairs": 3, "splits": {"kind": 2}}}
    (line,) = _sample_notes_lines(full)
    assert "arrived" not in line and "kind=2" in line


def test_q_profile_table_prints_no_verdict_for_a_headingless_schema_3_record(
    qenv: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape that tempts a renderer back into the sentinel: every statement
    schema-3, none of them under a heading, none quoting a modal phrase. The
    statement lines still read as statements, and no verdict is printed."""
    from jobhunter.l2.v2.types import NO_IMPORTANCE
    from tests.l2.v2.conftest import make_headingless_serving_record

    dh = _doc_hash()
    _seed_v3_profile(pg, dh, monkeypatch, record=make_headingless_serving_record())
    r = runner.invoke(cli.app, ["q", "profile", "--doc", dh[:12], "-o", "table"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "Bachelor's degree" in r.stdout and "CPA certification" in r.stdout
    assert NO_IMPORTANCE not in r.stdout


def test_q_claims_serves_a_review_rows_skills_with_heading_and_modality(
    qenv: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    dh = _doc_hash()
    _seed_v3_profile(pg, dh, monkeypatch)
    body = _data(["q", "claims", "--mention", "cpa"])
    assert body["meta"]["count"] == 1
    row = body["data"][0]
    assert row["document_hash"] == dh and row["mention"] == "CPA"
    assert row["extraction_status"] == "needs_review"
    assert row["section_heading"] == "Nice to have" and row["modality"] is None
    degree = _data(["q", "claims", "--mention", "bachelor's degree"])["data"][0]
    assert degree["section_heading"] == "Requirements" and degree["modality"] == "required"
    r = runner.invoke(cli.app, ["q", "claims", "--mention", "bachelor's degree", "-o", "table"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "Requirements" in r.stdout and '"required"' in r.stdout
    assert "contextual" not in r.stdout  # no verdict column for a schema-3 row


def test_q_claims_renders_each_kinds_own_heading_and_quote(
    qenv: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rows for one mention, one per statement kind: the table reads each
    row's own section and own quoted modal phrase, never the first statement's
    twice."""
    from tests.l2.v2.conftest import make_multi_kind_serving_record

    dh = _doc_hash()
    _seed_v3_profile(pg, dh, monkeypatch, record=make_multi_kind_serving_record())
    r = runner.invoke(cli.app, ["q", "claims", "--mention", "cpa", "-o", "table"])
    assert r.exit_code == 0, r.stdout + r.stderr
    lines = [line for line in r.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    qualification = next(ln for ln in lines if "qualification" in ln)
    responsibility = next(ln for ln in lines if "responsibility" in ln)
    assert qualification.startswith('Requirements  "required"')
    assert responsibility.startswith('Nice to have  "preferred"')


def test_q_claims_table_leaves_a_headingless_schema_3_row_blank(
    qenv: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-3 statement with a null heading and no quoted modal phrase has
    nothing to print but the absence: printing the `NO_IMPORTANCE` sentinel
    instead would be printing the verdict the contract stopped issuing."""
    from jobhunter.l2.v2.types import NO_IMPORTANCE
    from tests.l2.v2.conftest import make_headingless_serving_record

    dh = _doc_hash()
    _seed_v3_profile(pg, dh, monkeypatch, record=make_headingless_serving_record())
    r = runner.invoke(cli.app, ["q", "claims", "--mention", "cpa", "-o", "table"])
    assert r.exit_code == 0, r.stdout + r.stderr
    # the heading cell, empty (24 wide + the column separator) — the row's own
    # kind is the first thing that prints
    assert r.stdout.startswith(" " * 25 + "qualification")
    assert NO_IMPORTANCE not in r.stdout
    row = _data(["q", "claims", "--mention", "cpa"])["data"][0]
    # present and null: a schema-3 row with nothing to say, not a legacy one
    assert row["section_heading"] is None and row["modality"] is None
    assert row["schema_version"] == SEED_TUPLE[1]
    assert row["importance"] == NO_IMPORTANCE  # the sentinel, still in the column


def test_q_claims_importance_is_documented_as_a_legacy_filter(qenv: Path) -> None:
    # wide enough that the option table prints its help instead of eliding it
    r = runner.invoke(cli.app, ["q", "claims", "--help"], env={"COLUMNS": "200"})
    assert r.exit_code == 0
    assert "Legacy filter, schema-2 rows only" in r.stdout
    assert "required|preferred" in r.stdout  # and the sentinel is not among them
    # and why the third v1 word is missing: it is the no-verdict sentinel now
    assert "contextual" in r.stdout and "sentinel" in r.stdout


def test_q_claims_importance_refuses_the_no_verdict_sentinel(
    qenv: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--importance contextual` used to select every schema-3 row in the corpus
    alongside the legacy rows that really call a mention contextual, because
    `NO_IMPORTANCE` IS that string — a filter documented as legacy-only that in
    fact spanned both partitions. It is a usage error now, and says why."""
    from jobhunter.l2.v2.types import NO_IMPORTANCE

    dh = _doc_hash()
    _seed_v3_profile(pg, dh, monkeypatch)
    body = _data(["q", "claims", "--mention", "cpa", "--importance", NO_IMPORTANCE], code=2)
    assert body["error"]["kind"] == "usage"
    assert body["error"]["message"] == "contextual is not a verdict; schema-3 rows carry none"
    assert body["error"]["valid"] == ["required", "preferred"]
    # the legacy words still filter, and select nothing in the schema-3 partition
    assert _data(["q", "claims", "--mention", "cpa", "--importance", "required"])[
        "meta"]["count"] == 0
    assert _data(["q", "claims", "--mention", "cpa"])["meta"]["count"] == 1


def test_q_claims_across_the_corpus(qenv: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    dh = _doc_hash()
    _seed_profile(pg, dh)
    body = _data(["q", "claims", "--mention", "python"])
    assert body["meta"]["count"] == 1 and body["meta"]["truncated"] is False
    assert body["data"] == [{
        "document_hash": dh, "mention": "Python", "area_kind": "technical",
        "importance": "required", "extraction_status": "validated",
        # a v1 blob has no statements, so it has no heading or modal phrase to
        # offer: the two schema-3 keys are ABSENT rather than null, which is how
        # a renderer knows this row's verdict column is the one to read
        "schema_version": "1",
        "uid": "ab:ramp:x", "board": "ashby:ramp",
        "title": "Rust Engineer II", "company": "Ramp",
        "url": "https://jobs.ashbyhq.com/ramp/x",
    }]
    assert dh[:12] in body["meta"]["hint"]
    bad = _data(["q", "claims", "--mention", "Python", "--importance", "bogus"], code=2)
    assert bad["error"]["kind"] == "usage"
    assert bad["error"]["valid"] == ["required", "preferred"]
    assert _data(["q", "claims", "--mention", "Python", "--importance", "required"])[
        "meta"]["count"] == 1
    assert _data(["q", "claims", "--mention", "Python", "--board", "lever:palantir"])[
        "meta"]["count"] == 0
    assert _data(["q", "claims", "--mention", "Rust"])["data"] == []
    assert _data(["q", "claims", "--mention", "Python", "--fields", "uid"])["data"] == [
        {"uid": "ab:ramp:x"}
    ]
    r = runner.invoke(cli.app, ["q", "claims", "--mention", "Python", "-o", "table"])
    assert r.exit_code == 0 and "ab:ramp:x" in r.stdout


def test_q_claims_reports_only_the_engine_in_force(
    qenv: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retired prompt version still sits in `profile_mentions` after a rebuild
    replays it. `q claims` must not report the posting twice, nor let
    `--importance` match what the current extraction contradicts."""
    from jobhunter.l2.runner import SCHEMA_VERSION
    from jobhunter.l2.state import DerivedState
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.store import extraction

    dh = _doc_hash()
    profile = _seed_profile(pg, dh)  # the tuple in force: Python is required
    retired = json.loads(json.dumps(profile))
    for area in retired["demand_profile"]["areas"]:
        area["importance"] = "preferred"
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free",
        prompt_version="demand-profile/vOLD", schema_version=SCHEMA_VERSION,
        validator_version=VALIDATOR_VERSION, state=DerivedState("validated", None),
        profile=retired, updated_at="2026-08-01T00:00:00Z",
    )
    pg.commit()

    body = _data(["q", "claims", "--mention", "Python"])
    assert body["meta"]["count"] == 1  # one posting, not one per engine tuple
    assert body["data"][0]["importance"] == "required"
    assert _data(["q", "claims", "--mention", "Python", "--importance", "preferred"])[
        "data"] == []
    # a model outside JOB_HUNTER_L2_MODELS is another engine, not this corpus
    monkeypatch.setenv("JOB_HUNTER_L2_MODELS", "nvidia/*")
    assert _data(["q", "claims", "--mention", "Python"])["data"] == []


def test_q_stdout_stays_one_json_object_on_every_error_path(qenv: Path) -> None:
    for args in (["q", "postings", "--status", "bogus"], ["q", "posting", "nope"],
                 ["q", "document", "zz"], ["q", "profile", "--doc", "deadbeef"]):
        r = runner.invoke(cli.app, [*args, "-o", "json"])
        assert r.exit_code in (2, 4), (args, r.exit_code)
        assert json.loads(r.stdout)["ok"] is False


def test_q_postings_bad_since_is_an_envelope_error(qenv: Path) -> None:
    """A malformed --since must speak the contract: error envelope on stdout,
    exit 2 — never typer's usage box with an empty data stream."""
    r = runner.invoke(cli.app, ["q", "postings", "--since", "soon", "-o", "json"])
    assert r.exit_code == 2
    body = json.loads(r.stdout)
    assert body["ok"] is False
    assert body["error"]["kind"] == "usage"
    assert "soon" in body["error"]["message"]
