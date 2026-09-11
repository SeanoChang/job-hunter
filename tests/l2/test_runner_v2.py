"""Integration: the drain loop under the v2 bundle, over Postgres + LocalFS.

The loop itself is not re-tested here — `test_runner.py` owns ladder, breaker,
caps and catch-up. What this file proves is that selecting `v2` swaps every one
of the six engine-tuple pieces at once: the v6 prompt is what gets archived, the
schema-2 emit is what gets validated, `l2/v2/assemble` is what binds it,
`l2/v2/verify` is what judges it, and the two `l2/v2/serve` projections are what
reach `extractions.profile` and `profile_mentions`.

Recorded emits only — the same hand-authored case fixtures `tests/l2/v2/` runs
its contracts over. No model is ever called.
"""

from __future__ import annotations

import copy
import json
import pathlib
from datetime import UTC, datetime
from typing import Any

import psycopg

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.hashing import sha256_hex
from jobhunter.l2.attempts import from_bytes, to_bytes
from jobhunter.l2.bundles import get_bundle
from jobhunter.l2.engines import EngineResult
from jobhunter.l2.runner import run, settle
from jobhunter.l2.v2.assemble import assemble
from jobhunter.l2.v2.quality import assess
from jobhunter.store import extraction
from tests.l2.test_attempts import _attempt
from tests.l2.test_runner import FakeEngine, _seed_doc, _settings, store  # noqa: F401

Conn = psycopg.Connection[dict[str, Any]]

CASES = pathlib.Path(__file__).parent / "v2" / "cases"
GLOBS = ("z-ai/*",)
MODEL = "z-ai/glm-5.2:free"
V2_TUPLE = ("demand-profile/v8", "2", "14")


def source(case: str) -> str:
    return (CASES / f"{case}.source.md").read_text(encoding="utf-8")


def emit_of(case: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((CASES / f"{case}.emit.json").read_text(encoding="utf-8"))
    body: dict[str, Any] = loaded["emit"]
    return body


def v2_settings(**env: str) -> Any:
    return _settings(JOB_HUNTER_L2_BUNDLE="v2", **env)


def seed_case(pg: Conn, case: str) -> str:
    markdown = source(case)
    dh = sha256_hex(markdown.encode("utf-8"))
    _seed_doc(pg, dh=dh, markdown=markdown, uid=f"gh:x:{case}")
    return dh


def result(emit: dict[str, Any]) -> EngineResult:
    return EngineResult(json.dumps(emit), MODEL, 40, 9, 0.0)


def attempts_in(archive: ArchiveStore) -> list[Any]:
    return [from_bytes(archive.get(k)) for k in sorted(archive.list(keys.X_ATTEMPTS_PREFIX))]


def mention_rows_in(pg: Conn) -> list[tuple[str, str, str]]:
    return [
        (r["mention"], r["area_kind"], r["importance"])
        for r in pg.execute(
            "SELECT mention, area_kind, importance FROM profile_mentions"
            " ORDER BY mention, area_kind, importance"
        ).fetchall()
    ]


def test_a_v2_document_settles_validated_with_a_schema_2_blob(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """C01 end to end: v6 prompt in, schema-2 emit back, v2 record assembled and
    verified, the served slice stored under the v6/2/10 configuration."""
    dh = seed_case(pg, "C01")
    summary = run(v2_settings(), pg, store, engine=FakeEngine([result(emit_of("C01"))]),
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1 and summary.docs_attempted == 1

    row = pg.execute("SELECT * FROM extractions").fetchone()
    assert row is not None
    assert row["status"] == "validated" and row["document_hash"] == dh
    assert (row["prompt_version"], row["schema_version"], row["validator_version"]) == V2_TUPLE

    profile = row["profile"]
    assert profile["schema"] == "2"
    assert [s["id"] for s in profile["statements"]] == ["s_sales_experience"]
    # the C01 contract survives the round trip: a floor, not a bounded range
    assert profile["facts"]["entries"][0]["derived"]["quantity"] == {
        "dimension": "duration", "comparison": "gte", "min_value": 96, "max_value": None,
        "inclusive_min": True, "inclusive_max": None, "unit": "month",
    }
    # ... and the bulk of the record stayed in the archive
    assert "block_accounting" not in profile and "extraction" not in profile


def test_the_v2_prompt_and_schema_are_archived_write_once(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    seed_case(pg, "C01")
    run(v2_settings(), pg, store, engine=FakeEngine([result(emit_of("C01"))]),
        max_docs=10, max_usd=5.0)
    assert store.exists(keys.x_prompt_key("demand-profile/v8"))
    assert store.exists(keys.x_schema_key("2"))
    attempt = attempts_in(store)[0]
    assert attempt.outcome == "ok"
    assert (attempt.prompt_version, attempt.schema_version, attempt.validator_version) == V2_TUPLE
    assert attempt.record is not None
    assert attempt.record["extraction"]["schema_version"] == "2"
    assert attempt.record["block_accounting"]  # the archive keeps what the blob drops


def test_an_unaudited_record_stores_its_profile_but_indexes_no_mentions(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The quality gate at the write path: C04 names three certifications, and
    none of them enters the aggregate until an audit clears the record. The
    profile blob still serves."""
    seed_case(pg, "C04")
    summary = run(v2_settings(), pg, store, engine=FakeEngine([result(emit_of("C04"))]),
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1
    row = pg.execute("SELECT profile FROM extractions").fetchone()
    assert row is not None
    assert row["profile"]["quality"]["search_eligible"] is False
    assert [m["surface"] for m in row["profile"]["mentions"]] == ["CPA", "ACCA", "ACA"]
    assert mention_rows_in(pg) == []


def test_settle_writes_statement_derived_importance_into_the_aggregate(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The C04 fix where it lands: an audited record's mentions carry the
    importance of the statement each one supports, not of the `credential` area
    they share with a required qualification.

    The auditor that clears `semantics`/`completeness` is a fast follow, so the
    record is cleared here through the frozen `quality.assess` policy and fed to
    `settle` the way the runner feeds it — an archived ok attempt.
    """
    markdown = source("C04")
    dh = seed_case(pg, "C04")
    record = assemble(emit_of("C04"), markdown, document_hash=dh,
                      observed_model=MODEL, at="2026-09-10T00:00:00+00:00")
    record["quality"] = assess(source="usable", evidence="pass",
                               semantics="no_findings", completeness="no_findings")
    started = datetime(2026, 9, 10, 6, 12, 4, tzinfo=UTC)
    attempt = _attempt(
        attempt_key=keys.x_attempt_key(started, dh, 1, 1), document_hash=dh,
        prompt_version=V2_TUPLE[0], schema_version=V2_TUPLE[1], validator_version=V2_TUPLE[2],
        requested_model=MODEL, observed_model=MODEL, record=record,
        started_at="2026-09-10T06:12:04Z", finished_at="2026-09-10T06:12:09Z",
    )
    store.put(attempt.attempt_key, to_bytes(attempt))
    extraction.record_attempt(pg, attempt, None)

    state = settle(pg, store, dh, GLOBS, "2026-09-10T06:13:00Z", bundle=get_bundle("v2"))
    assert state.status == "validated"
    assert mention_rows_in(pg) == [
        ("ACA", "qualification", "preferred"),
        ("ACCA", "qualification", "preferred"),
        ("CPA", "qualification", "preferred"),
    ]


def test_disagreeing_samples_demote_a_v2_document_to_needs_review(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The k-sampling gate has teeth under v2.

    The audit slot takes three samples; two of them cite a different span for
    the same requirement, so `agreement` demotes the document instead of
    certifying it. The gate reads one place — `demand_profile.areas[].claims[]`
    of whatever `bundle.profile_of` returns — so before the served slice carried
    a claim index it scored every v2 cohort a vacuous 1.0 and every audited or
    reprompted document validated itself.
    """
    seed_case(pg, "C01")
    divergent = copy.deepcopy(emit_of("C01"))
    divergent["statements"][0]["evidence"] = [
        {"block_id": "b000002", "occurrence": 0,
         "text": "a proven track record of exceeding sales targets"}
    ]
    engine = FakeEngine([result(emit_of("C01")), result(divergent), result(divergent)])
    summary = run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 0

    row = pg.execute("SELECT status, k, agreement, chosen_attempt FROM extractions").fetchone()
    assert row is not None
    assert row["status"] == "needs_review" and row["k"] == 3
    assert "f1" in row["agreement"]["failures"] and row["agreement"]["mean_f1"] < 0.8
    # the medoid is the majority reading, and a demoted document indexes nothing
    chosen = {a.attempt_key: a for a in attempts_in(store)}[row["chosen_attempt"]]
    assert chosen.sample_slot in (2, 3)
    assert mention_rows_in(pg) == []


def test_agreeing_samples_still_certify(pg: Conn, store: ArchiveStore) -> None:  # noqa: F811
    """The other half of the gate: three samples that say the same thing settle
    validated, so the claim index demotes disagreement rather than everything."""
    seed_case(pg, "C01")
    engine = FakeEngine([result(emit_of("C01"))] * 3)
    summary = run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1
    row = pg.execute("SELECT status, k, agreement FROM extractions").fetchone()
    assert row is not None
    assert row["status"] == "validated" and row["k"] == 3
    assert row["agreement"]["mean_f1"] == 1.0 and row["agreement"]["failures"] == []


def test_a_broken_reference_quarantines_after_the_ladder(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """A schema-valid emit citing a block that does not exist is an attribution
    failure, and the binding error is fed into the next attempt verbatim."""
    seed_case(pg, "C01")
    broken = copy.deepcopy(emit_of("C01"))
    broken["statements"][0]["evidence"][0]["block_id"] = "b000009"
    summary = run(v2_settings(), pg, store, engine=FakeEngine([result(broken)] * 3),
                  max_docs=10, max_usd=5.0)
    assert summary.quarantined == 1 and summary.validated == 0

    row = pg.execute("SELECT status FROM extractions").fetchone()
    assert row is not None and row["status"] == "quarantined"
    archived = attempts_in(store)
    assert [a.outcome for a in archived] == ["attribution_failed"] * 3
    assert archived[-1].ladder_exhausted is True
    assert archived[0].prior_errors == []
    assert any("b000009" in error for error in archived[1].prior_errors)
    assert mention_rows_in(pg) == []
