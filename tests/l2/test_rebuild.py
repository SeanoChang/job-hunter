"""extract rebuild must reproduce the incrementally-built surface row for row —
the increment's recomputability assertion."""

import json
from typing import Any

import psycopg
import pytest

from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.keys import x_review_key
from jobhunter.l2.rebuild import rebuild_extractions
from jobhunter.l2.runner import run
from jobhunter.store import extraction
from jobhunter.timeutil import utcnow_precise
from tests.l2.test_assemble import EMIT
from tests.l2.test_runner import DH, GOOD, FakeEngine, _seed_doc, _settings, store  # noqa: F401

Conn = psycopg.Connection[dict[str, Any]]


def _dump(pg: Conn) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    out["attempts"] = pg.execute(
        "SELECT * FROM extraction_attempts ORDER BY attempt_key"
    ).fetchall()
    out["reviews"] = pg.execute("SELECT * FROM extraction_reviews ORDER BY review_key").fetchall()
    rows = pg.execute("SELECT * FROM extractions ORDER BY document_hash, model").fetchall()
    for r in rows:
        r.pop("updated_at")  # settle time differs between live run and replay
    out["extractions"] = rows
    return out


def test_rebuild_reproduces_incremental_state(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    _seed_doc(pg)
    settings = _settings()
    summary = run(settings, pg, store, engine=FakeEngine([GOOD]), max_docs=10, max_usd=5.0)
    assert summary.validated == 1

    # a human flag, archived first like the CLI does, then applied
    at = utcnow_precise()
    row = pg.execute("SELECT * FROM extractions").fetchone()
    assert row is not None
    event = {
        "review_key": x_review_key(at, DH, "flag", 1),
        "document_hash": DH,
        "model": row["model"],
        "prompt_version": row["prompt_version"],
        "schema_version": row["schema_version"],
        "validator_version": row["validator_version"],
        "verb": "flag",
        "payload": None,
        "actor": "human",
        "at": at.isoformat(),
    }
    store.put(event["review_key"], json.dumps(event).encode("utf-8"))
    extraction.record_review(pg, **event)
    from jobhunter.l2.runner import settle

    state = settle(
        pg, store, DH, settings.l2_models, at.isoformat(),
        prompt_version=row["prompt_version"], schema_version=row["schema_version"],
        validator_version=row["validator_version"],
    )
    assert state.status == "needs_review"
    pg.commit()

    before = _dump(pg)
    assert before["extractions"][0]["status"] == "needs_review"
    assert before["extractions"][0]["profile"] is not None  # review kept the profile

    attempts, reviews = rebuild_extractions(pg, store, settings.l2_models)
    pg.commit()
    assert attempts >= 1 and reviews == 1
    assert _dump(pg) == before


def test_rebuild_repopulates_profile_mentions(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """profile_mentions is derived from extractions.profile, so replay owes it the
    same reconstruction the extraction rows get — nothing about it is authored."""
    _seed_doc(pg)
    settings = _settings()
    run(settings, pg, store, engine=FakeEngine([GOOD]), max_docs=10, max_usd=5.0)
    pg.commit()
    expected = [("Python", "technical", "required")]
    rows = pg.execute("SELECT mention, area_kind, importance FROM profile_mentions").fetchall()
    assert [(r["mention"], r["area_kind"], r["importance"]) for r in rows] == expected

    pg.execute("DELETE FROM profile_mentions")  # as a store rebuilt from the archive starts
    rebuild_extractions(pg, store, settings.l2_models)
    pg.commit()
    rows = pg.execute("SELECT mention, area_kind, importance FROM profile_mentions").fetchall()
    assert [(r["mention"], r["area_kind"], r["importance"]) for r in rows] == expected


def test_rebuild_rejudges_raw_responses_under_current_validators(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Spec §4.3 step 2: the derived row comes from re-running today's
    validators over the archived raw response — a validator bugfix (or bump)
    re-judges the corpus for $0, without an LLM call."""
    _seed_doc(pg)
    from tests.l2.test_attempts import _attempt

    wrongly_failed = _attempt(
        document_hash=DH,
        outcome="attribution_failed",  # archived verdict from a buggy validator
        raw_response=json.dumps(EMIT),  # ...but the raw response is actually valid
        validation=[{"error": "phantom finding"}],
        attempt_no=1,
        ladder_exhausted=True,
    )
    from jobhunter.l2.attempts import to_bytes

    store.put(wrongly_failed.attempt_key, to_bytes(wrongly_failed))
    attempts, _ = rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    assert attempts == 1
    row = pg.execute("SELECT status, profile FROM extractions").fetchone()
    assert row and row["status"] == "validated"  # re-judged, not restored
    assert row["profile"]["demand_profile"]["areas"][0]["id"] == "a1"
    prov = pg.execute("SELECT outcome FROM extraction_attempts").fetchone()
    assert prov and prov["outcome"] == "attribution_failed"  # provenance untouched


def test_rebuild_preserves_the_cohort_verdict(
    pg: psycopg.Connection[dict[str, Any]], store: ArchiveStore  # noqa: F811
) -> None:
    """Review P0-1 acceptance: the disagreeing three-sample case stays
    needs_review after replay, with the same k, agreement and chosen record —
    rebuild must never promote what the live gate demoted."""
    import copy

    from jobhunter.l2.engines import EngineResult

    _seed_doc(pg)
    divergent = copy.deepcopy(EMIT)
    del divergent["demand_profile"]["areas"][0]["claims"][1]
    divergent["demand_profile"]["areas"][0]["structure"] = None
    div = EngineResult(json.dumps(divergent), "z-ai/glm-5.2:free", 40, 9, 0.0)
    run(_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store,
        engine=FakeEngine([GOOD, div, div]), max_docs=10, max_usd=5.0)
    live = pg.execute("SELECT * FROM extractions").fetchone()
    assert live and live["status"] == "needs_review" and live["k"] == 3
    assert live["agreement"] and live["agreement"]["failures"]

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    replayed = pg.execute("SELECT * FROM extractions").fetchone()
    assert replayed is not None
    assert replayed["status"] == "needs_review"
    assert replayed["k"] == 3
    assert replayed["agreement"] and replayed["agreement"]["failures"] == \
        live["agreement"]["failures"]
    assert replayed["chosen_attempt"] == live["chosen_attempt"]
    assert replayed["profile"] == live["profile"]


# --- bundle-aware replay (the re-settle campaign, validator/18) -------------


def test_rebuild_replays_a_v2_document_with_its_audit_and_repair(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Replay folds each archived tuple under the bundle that CLAIMS it.

    A schema-2 record has no v1 shapes to fold through — no `demand_profile`
    key, no v1 grammar behind its facts — so a replay that assumed v1 could not
    reproduce the v2 surface at all. Under its own bundle it reproduces all of
    it: the audit artifact and the repair the archive holds, the repaired
    candidate in `extractions.profile`, and its mentions in the aggregate.
    """
    from tests.l2 import test_runner_v2 as v2

    v2.seed_case(pg, "C04")
    engine = v2.AuditingEngine([v2.result(v2.polarity_split_c04())],
                               v2.scripted(v2.polarity_audit, v2.clean_audit),
                               repair=v2.polarity_repair)
    run(v2.v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    pg.commit()
    assert v2.mention_rows_in(pg) == v2.C04_ROWS
    before = _dump(pg)
    assert before["extractions"][0]["profile"]["quality"]["search_eligible"] is True

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    assert _dump(pg) == before
    assert v2.mention_rows_in(pg) == v2.C04_ROWS


def test_rebuild_settles_validator_17_attempts_under_the_current_policy(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The re-settle campaign, without one extraction call: a validator-17
    attempt replays under validator 20, its candidate re-derives identically, and
    the audit the archive already holds — joined by the CANDIDATE HASH it names,
    never by an attempt key — is the verdict it settles with.

    This is the FROZEN `(demand-profile/v10, 2)` partition — the shape the whole
    archived corpus is in — so the attempt, its record and its raw response are
    all schema 2, and `get_bundle_for_tuple` is what has to find the judge.
    """
    from datetime import UTC, datetime

    from jobhunter.archive import keys
    from jobhunter.l2.attempts import to_bytes
    from jobhunter.l2.v2.assemble import assemble, candidate_hash
    from tests.l2 import test_runner_v2 as v2
    from tests.l2.test_attempts import _attempt

    markdown = v2.source("C04")
    dh = v2.seed_case(pg, "C04")
    record = assemble(v2.emit2_of("C04"), markdown, document_hash=dh,
                      observed_model=v2.MODEL, at="2026-09-12T06:12:04Z",
                      schema_version="2")
    # the candidate as validator 17 sealed it, hash and all
    record["extraction"]["validator_version"] = "17"
    record["extraction"]["candidate_hash"] = candidate_hash(record)
    started = datetime(2026, 9, 12, 6, 12, 4, tzinfo=UTC)
    attempt = _attempt(
        attempt_key=keys.x_attempt_key(started, dh, 1, 1), document_hash=dh,
        prompt_version=v2.V2_SCHEMA2_TUPLE[0], schema_version="2", validator_version="17",
        requested_model=v2.MODEL, observed_model=v2.MODEL, record=record,
        raw_response=json.dumps(v2.emit2_of("C04")),
        started_at="2026-09-12T06:12:04Z", finished_at="2026-09-12T06:12:09Z",
    )
    store.put(attempt.attempt_key, to_bytes(attempt))
    v2.archive_audit(store, attempt.attempt_key,
                     candidate_hash=record["extraction"]["candidate_hash"])

    attempts, _ = rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    assert attempts == 1
    # scoped to the frozen partition: the same replay now also derives a
    # schema-3 row under the active tuple (the v20 migration, below)
    row = _row(pg, v2.V2_SCHEMA2_TUPLE)
    assert row is not None
    assert row["status"] == "validated" and row["chosen_attempt"] == attempt.attempt_key
    quality = row["profile"]["quality"]
    assert (quality["semantics"], quality["completeness"]) == ("no_findings", "no_findings")
    assert quality["search_eligible"] is True  # the carried audit is what clears it
    # a schema-2 record still carries the statement's verdict, and replay must
    # project the shape it replayed, not the shape the live path writes today
    assert _mentions(pg, v2.V2_SCHEMA2_TUPLE) == v2.C04_ROWS_V2
    prov = pg.execute("SELECT validator_version FROM extraction_attempts").fetchone()
    assert prov is not None and prov["validator_version"] == "17"  # provenance untouched


def test_rebuild_drops_an_audit_that_does_not_describe_the_replayed_candidate(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The join is the candidate hash, and that is what makes it safe: when
    today's validators re-judge an archived response into a DIFFERENT candidate,
    the audit of the old one is not the new one's verdict. A record nothing
    audited is `not_checked`, never a pass — and never eligible.

    Driven on the frozen schema-2 partition, like the replay above.
    """
    from datetime import UTC, datetime

    from jobhunter.archive import keys
    from jobhunter.l2.attempts import to_bytes
    from jobhunter.l2.v2.assemble import assemble
    from tests.l2 import test_runner_v2 as v2
    from tests.l2.test_attempts import _attempt

    markdown = v2.source("C04")
    dh = v2.seed_case(pg, "C04")
    audited = assemble(v2.emit2_of("C04"), markdown, document_hash=dh,
                       observed_model=v2.MODEL, at="2026-09-12T06:12:04Z",
                       schema_version="2")
    started = datetime(2026, 9, 12, 6, 12, 4, tzinfo=UTC)
    attempt = _attempt(
        attempt_key=keys.x_attempt_key(started, dh, 1, 1), document_hash=dh,
        prompt_version=v2.V2_SCHEMA2_TUPLE[0], schema_version="2",
        validator_version=v2.V2_SCHEMA2_TUPLE[2],
        requested_model=v2.MODEL, observed_model=v2.MODEL, record=audited,
        # what the re-judge produces is not what was audited
        raw_response=json.dumps(v2.polarity_split_c04_v2()),
        started_at="2026-09-12T06:12:04Z", finished_at="2026-09-12T06:12:09Z",
    )
    store.put(attempt.attempt_key, to_bytes(attempt))
    v2.archive_audit(store, attempt.attempt_key,
                     candidate_hash=audited["extraction"]["candidate_hash"])

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    for tup in (v2.V2_SCHEMA2_TUPLE, v2.V2_TUPLE):
        # both the replayed partition and the derived one: the audit names a
        # candidate neither of them holds, so neither borrows its verdict
        row = _row(pg, tup)
        assert row is not None and row["status"] == "validated"
        quality = row["profile"]["quality"]
        assert (quality["semantics"], quality["completeness"]) == ("not_checked", "not_checked")
        assert quality["search_eligible"] is False
        assert sorted({m for m, _, _ in _mentions(pg, tup)}) == ["ACA", "ACCA", "CPA"]


# --- the v20 migration replay: schema 2 -> schema 3, offline (T-EBFY ac-1) --


def _archive_schema2_attempt(
    pg: Conn, store: ArchiveStore, case: str  # noqa: F811
) -> tuple[str, Any, dict[str, Any], str]:
    """One archived `(demand-profile/v10, 2)` attempt — the shape the whole live
    corpus is in — with its markdown seeded. Returns (dh, attempt, record2, md)."""
    from datetime import UTC, datetime

    from jobhunter.archive import keys
    from jobhunter.l2.attempts import to_bytes
    from jobhunter.l2.v2.assemble import assemble
    from tests.l2 import test_runner_v2 as v2
    from tests.l2.test_attempts import _attempt

    markdown = v2.source(case)
    dh = v2.seed_case(pg, case)
    record = assemble(v2.emit2_of(case), markdown, document_hash=dh,
                      observed_model=v2.MODEL, at="2026-09-12T06:12:04Z",
                      schema_version="2")
    started = datetime(2026, 9, 12, 6, 12, 4, tzinfo=UTC)
    attempt = _attempt(
        attempt_key=keys.x_attempt_key(started, dh, 1, 1), document_hash=dh,
        prompt_version=v2.V2_SCHEMA2_TUPLE[0], schema_version="2",
        validator_version=v2.V2_SCHEMA2_TUPLE[2],
        requested_model=v2.MODEL, observed_model=v2.MODEL, record=record,
        raw_response=json.dumps(v2.emit2_of(case)),
        started_at="2026-09-12T06:12:04Z", finished_at="2026-09-12T06:12:09Z",
    )
    store.put(attempt.attempt_key, to_bytes(attempt))
    return dh, attempt, record, markdown


def _no_engine(monkeypatch: Any) -> None:
    """Any engine call at all is a failed migration: the whole point of the v20
    replay is that 17k documents reach schema 3 for $0."""
    from jobhunter.l2 import engines

    def boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("the migration replay called an extraction engine")

    for cls in (engines.OpenAICompat, engines.CodexCli, engines.ClaudeCli):
        monkeypatch.setattr(cls, "complete", boom)


def _row(pg: Conn, tup: tuple[str, str, str]) -> dict[str, Any] | None:  # noqa: F811
    return pg.execute(
        "SELECT * FROM extractions WHERE prompt_version=%s AND schema_version=%s"
        " AND validator_version=%s",
        tup,
    ).fetchone()


def _mentions(pg: Conn, tup: tuple[str, str, str]) -> list[tuple[str, str, str]]:  # noqa: F811
    """`profile_mentions` under ONE engine tuple — the aggregate is homogeneous
    in the tuple, and the migration puts two partitions in the table at once."""
    return [
        (r["mention"], r["area_kind"], r["importance"])
        for r in pg.execute(
            "SELECT mention, area_kind, importance FROM profile_mentions"
            " WHERE prompt_version=%s AND schema_version=%s AND validator_version=%s"
            " ORDER BY mention, area_kind, importance",
            tup,
        ).fetchall()
    ]


def _record_keys(profile: dict[str, Any]) -> set[str]:
    """Every object key in the stored record, minus the legacy claim index.

    `demand_profile` is v1's shape by contract (`serve.claim_index`): its areas
    carry `importance: contextual` even for a schema-3 record, because the
    agreement gate and the legacy renderers read that shape. Everything else in
    the blob IS the schema-3 record, and that is where no verdict may survive.
    """
    return _keys_anywhere({k: v for k, v in profile.items() if k != "demand_profile"})


def _keys_anywhere(value: Any) -> set[str]:
    """Every object key in a JSON tree — what `no importance anywhere` means."""
    if isinstance(value, dict):
        found = set(value)
        for nested in value.values():
            found |= _keys_anywhere(nested)
        return found
    if isinstance(value, list):
        found = set()
        for nested in value:
            found |= _keys_anywhere(nested)
        return found
    return set()


def test_rebuild_files_a_schema3_row_for_a_schema2_attempt_with_no_engine_call(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """The v20 migration: a `(demand-profile/v10, 2)` archived attempt lands as a
    SCHEMA-3 candidate under the active tuple, derived offline, and the frozen
    schema-2 partition it was replayed from is still there beside it."""
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    _archive_schema2_attempt(pg, store, "C03")

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()

    migrated = _row(pg, v2.V2_TUPLE)
    assert migrated is not None, "no row under the active (v11, 3, 20) tuple"
    assert migrated["status"] == "validated"
    assert migrated["profile"]["schema"] == "3"
    # the replayed schema-2 fold is untouched: the archive is truth and its own
    # partition keeps folding
    frozen = _row(pg, v2.V2_SCHEMA2_TUPLE)
    assert frozen is not None and frozen["profile"]["schema"] == "2"


def test_the_schema3_derivation_computes_headings_and_keeps_only_modal_quotes(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """Spec §7, read off the stored row: `section_heading` is code-derived, a
    modal quote becomes `modality_evidence`, a descriptor quote derives null,
    and no statement carries a verdict of any kind."""
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    _archive_schema2_attempt(pg, store, "C03")
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()

    row = _row(pg, v2.V2_TUPLE)
    assert row is not None
    statements = {s["id"]: s for s in row["profile"]["statements"]}
    assert statements["s_degree"]["section_heading"] == "Education & Experience"
    assert statements["s_sql"]["section_heading"] == "Technical Expertise"
    # "welcome but not required" carries a modal term: the quote survives
    modality = statements["s_advanced_degree"]["modality_evidence"]
    assert modality is not None and modality[0]["text"] == "welcome but not required"
    # "## What We Are Looking For" is a descriptor the model read a verdict off;
    # it is not a modality, so it derives null
    assert statements["s_degree"]["modality_evidence"] is None
    assert statements["s_sql"]["modality_evidence"] is None
    # the schema-2 record said `proficiency: proficient` for s_sql
    assert "proficiency" not in statements["s_sql"]
    assert "importance" not in _record_keys(row["profile"])


def test_the_migrated_schema3_record_verifies_clean_under_validator_20(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    from jobhunter.l2.bundles import get_bundle
    from jobhunter.l2.rebuild import derive_schema3
    from jobhunter.l2.schemas import validate_record
    from jobhunter.l2.v2.source import annotate

    _no_engine(monkeypatch)
    _, _, record2, markdown = _archive_schema2_attempt(pg, store, "C03")

    derived = derive_schema3(record2, annotate(markdown))
    assert derived["extraction"]["schema_version"] == "3"
    assert derived["extraction"]["validator_version"] == "20"
    # the row key is the active tuple's; the record's prompt stamp is never
    # restamped, so no migrated record ever claims v11 produced it
    assert derived["extraction"]["prompt_version"] \
        == record2["extraction"]["prompt_version"] != "demand-profile/v11"
    assert validate_record(derived, "3") == []
    report = get_bundle("v2").verify(derived, markdown)
    assert report.status != "fail", [f.code for f in report.findings]
    # what actually ran is the ATTEMPT's prompt version, and replay restores it
    # from the archive untouched
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    prov = pg.execute("SELECT DISTINCT prompt_version FROM extraction_attempts").fetchall()
    assert [r["prompt_version"] for r in prov] == ["demand-profile/v10"]


def test_the_migrated_schema3_candidate_hash_is_identical_across_two_rebuilds(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """Determinism is what makes the migration re-runnable: the same archive
    derives the same candidate, so a second replay orphans no artifact."""
    from jobhunter.l2.rebuild import derive_schema3
    from jobhunter.l2.v2.source import annotate
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    _, _, record2, markdown = _archive_schema2_attempt(pg, store, "C03")
    first = derive_schema3(record2, annotate(markdown))
    second = derive_schema3(record2, annotate(markdown))
    assert first["extraction"]["candidate_hash"] == second["extraction"]["candidate_hash"]

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    once = _row(pg, v2.V2_TUPLE)
    assert once is not None
    once.pop("updated_at")
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    twice = _row(pg, v2.V2_TUPLE)
    assert twice is not None
    twice.pop("updated_at")
    assert twice == once


def test_the_schema3_row_settles_with_the_audit_the_archive_already_holds(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """The derived candidate is a new candidate with a new hash, but the audit
    describes the EXTRACTION, and the derivation changed no bound span. An
    artifact that cleared the schema-2 candidate clears the derived one too —
    otherwise the migration would owe a re-audit on every one of 17k documents."""
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    _, attempt, record2, _ = _archive_schema2_attempt(pg, store, "C03")
    v2.archive_audit(store, attempt.attempt_key,
                     candidate_hash=record2["extraction"]["candidate_hash"])

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    row = _row(pg, v2.V2_TUPLE)
    assert row is not None
    quality = row["profile"]["quality"]
    assert (quality["semantics"], quality["completeness"]) == ("no_findings", "no_findings")
    assert quality["search_eligible"] is True


def test_a_schema3_archive_is_never_re_derived_by_the_replay(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """The migration reads schema 2 only. An attempt already archived under the
    ACTIVE tuple replays as itself — deriving it again would strip the modality
    it carries and produce a verifiably-clean lie."""
    from datetime import UTC, datetime

    from jobhunter.archive import keys
    from jobhunter.l2.attempts import to_bytes
    from jobhunter.l2.v2.assemble import assemble
    from tests.l2 import test_runner_v2 as v2
    from tests.l2.test_attempts import _attempt

    _no_engine(monkeypatch)
    markdown = v2.source("C03")
    dh = v2.seed_case(pg, "C03")
    record = assemble(v2.emit_of("C03"), markdown, document_hash=dh,
                      observed_model=v2.MODEL, at="2026-09-22T06:12:04Z",
                      schema_version="3")
    started = datetime(2026, 9, 22, 6, 12, 4, tzinfo=UTC)
    attempt = _attempt(
        attempt_key=keys.x_attempt_key(started, dh, 1, 1), document_hash=dh,
        prompt_version=v2.V2_TUPLE[0], schema_version="3",
        validator_version=v2.V2_TUPLE[2], requested_model=v2.MODEL,
        observed_model=v2.MODEL, record=record,
        raw_response=json.dumps(v2.emit_of("C03")),
        started_at="2026-09-22T06:12:04Z", finished_at="2026-09-22T06:12:09Z",
    )
    store.put(attempt.attempt_key, to_bytes(attempt))

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    rows = pg.execute("SELECT * FROM extractions").fetchall()
    assert len(rows) == 1
    statements = {s["id"]: s for s in rows[0]["profile"]["statements"]}
    assert statements["s_advanced_degree"]["modality_evidence"] is not None


# --- the migrated partition is serviceable, not a dead projection -----------


def _review(pg: Conn, store: ArchiveStore, dh: str, verb: str,  # noqa: F811
            tup: tuple[str, str, str]) -> Any:
    """`cli._review_verb` over one named tuple: archive the event, record it,
    settle. What a reviewer does, minus the row selection."""
    from jobhunter.l2.runner import settle

    at = utcnow_precise()
    row = _row(pg, tup)
    assert row is not None
    event = {
        "review_key": x_review_key(at, dh, verb, 1), "document_hash": dh,
        "model": row["model"], "prompt_version": tup[0], "schema_version": tup[1],
        "validator_version": tup[2], "verb": verb, "payload": None,
        "actor": "human", "at": at.isoformat(),
    }
    store.put(event["review_key"], json.dumps(event).encode("utf-8"))
    extraction.record_review(pg, **event)
    return settle(pg, store, dh, ("z-ai/*",), at.isoformat(),
                  prompt_version=tup[0], schema_version=tup[1], validator_version=tup[2])


def test_the_migrated_row_is_the_one_a_reviewers_verb_lands_on(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """`extract review` picks the document's row with `ORDER BY updated_at DESC
    LIMIT 1` (cli.py). The migration puts two rows behind one document, so that
    selector decides which partition a human verb addresses — and the answer has
    to be the one the read surface answers from, not a coin flip on a tie."""
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    dh, _, _, _ = _archive_schema2_attempt(pg, store, "C03")
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    picked = pg.execute(
        "SELECT prompt_version, schema_version, validator_version FROM extractions"
        " WHERE document_hash=%s ORDER BY updated_at DESC LIMIT 1", (dh,),
    ).fetchone()
    assert picked is not None
    assert (picked["prompt_version"], picked["schema_version"],
            picked["validator_version"]) == v2.V2_TUPLE


def test_a_review_of_a_migrated_document_moves_the_served_row(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """A human flag on the migrated partition must reach the row the read
    surface publishes. Before the fold could reach the migrated attempts this
    settled nothing at all: the row went on publishing what a reviewer had just
    parked."""
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    dh, _, _, _ = _archive_schema2_attempt(pg, store, "C03")
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()

    state = _review(pg, store, dh, "flag", v2.V2_TUPLE)
    pg.commit()
    assert state.status == "needs_review"
    row = _row(pg, v2.V2_TUPLE)
    assert row is not None and row["status"] == "needs_review"
    assert row["reviewed_by"] == "human"
    assert row["profile"]["schema"] == "3"  # still the migrated shape


def test_a_review_of_a_migrated_document_survives_a_full_rebuild(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """Recomputability: a ruling archived under the ACTIVE tuple is folded by
    the replay that owns that partition, or `extract rebuild` silently
    re-publishes content a reviewer rejected."""
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    dh, _, _, _ = _archive_schema2_attempt(pg, store, "C03")
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    _review(pg, store, dh, "flag", v2.V2_TUPLE)
    pg.commit()

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    row = _row(pg, v2.V2_TUPLE)
    assert row is not None and row["status"] == "needs_review"
    assert row["reviewed_by"] == "human"


def test_a_drain_finishes_the_re_audit_a_migrated_row_asks_for(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The migrated row lands `audit_retry` owed — the whole archived corpus's
    artifacts are a retired audit version — so the re-audit pass takes it. It
    has to be able to FINISH it: a row whose audit can never complete never
    leaves the window, and its frozen `updated_at` sorts it ahead of everything
    the pass could still do something for (`_reaudit_queue`)."""
    from jobhunter.l2.bundles import get_bundle
    from tests.l2 import test_runner_v2 as v2

    _archive_schema2_attempt(pg, store, "C03")
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    row = _row(pg, v2.V2_TUPLE)
    assert row is not None and row["flags"]["audit_retry"] is True
    assert len(v2.reaudit_window(pg, limit=5)) == 1

    engine = v2.AuditingEngine([], v2.clean_audit)
    run(v2.v2_settings(), pg, store, engine=engine, max_docs=5, max_usd=5.0,
        bundle=get_bundle("v2"))
    pg.commit()
    assert len(engine.audits) == 1, "the migrated candidate was never audited"
    assert v2.reaudit_window(pg, limit=5) == []
    row = _row(pg, v2.V2_TUPLE)
    assert row is not None
    assert row["flags"]["audit"] == "ok" and "audit_retry" not in row["flags"]
    assert row["profile"]["quality"]["search_eligible"] is True


def test_the_audit_a_drain_writes_on_a_migrated_row_survives_the_next_rebuild(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The live pass audits the DERIVED candidate, so the artifact names the
    derived hash. Replay joins artifacts by hash, so it has to look for that one
    first — otherwise every replay after a re-audit campaign throws the campaign
    away and bills it again."""
    from jobhunter.l2.bundles import get_bundle
    from tests.l2 import test_runner_v2 as v2

    _archive_schema2_attempt(pg, store, "C03")
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    run(v2.v2_settings(), pg, store, engine=v2.AuditingEngine([], v2.clean_audit),
        max_docs=5, max_usd=5.0, bundle=get_bundle("v2"))
    pg.commit()

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    row = _row(pg, v2.V2_TUPLE)
    assert row is not None
    quality = row["profile"]["quality"]
    assert (quality["semantics"], quality["completeness"]) == ("no_findings", "no_findings")
    assert quality["search_eligible"] is True


def test_a_repair_round_on_a_migrated_row_repairs_the_candidate_it_serves(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The one repair round a candidate gets is write-once, so the shape the
    round names has to be the shape it is repairing. A migrated row serves the
    DERIVED candidate, and the repair contract advertises the ACTIVE schema, so
    rendering the archived schema-2 record would spend the round on an answer
    nothing could apply."""
    import gzip

    from jobhunter.archive import keys
    from jobhunter.l2.bundles import get_bundle
    from jobhunter.l2.rebuild import derive_schema3
    from jobhunter.l2.v2.source import annotate
    from tests.l2 import test_runner_v2 as v2

    _, attempt, record2, markdown = _archive_schema2_attempt(pg, store, "C03")
    derived_hash = derive_schema3(record2, annotate(markdown))["extraction"]["candidate_hash"]
    store.put(
        v2.audit_key(attempt.attempt_key),
        gzip.compress(json.dumps({
            "audit_version": v2.AUDIT_VERSION, "outcome": "ok",
            "attempt_key": attempt.attempt_key, "audit_pass": 1,
            "candidate_hash": derived_hash, "semantics": "findings",
            "completeness": "no_findings", "blocking": 1,
            "findings": [{"code": "unsupported_statement", "severity": "error",
                          "dimension": "semantics", "targets": ["s_sql"],
                          "explanation": "the cited block does not say this"}],
            "unresolved": [],
        }, sort_keys=True).encode("utf-8"), mtime=0),
    )
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    row = _row(pg, v2.V2_TUPLE)
    assert row is not None and row["flags"].get("repair"), row["flags"]

    # no repairer scripted: the call is recorded, nothing is archived, and the
    # round stays unspent — all this test needs is the prompt it was given
    engine = v2.AuditingEngine([], v2.clean_audit)
    run(v2.v2_settings(), pg, store, engine=engine, max_docs=5, max_usd=5.0,
        bundle=get_bundle("v2"))
    pg.commit()
    assert engine.repairs and len(set(engine.repairs)) == 1  # one round, one transport retry
    assert v2.base_hash_in(engine.repairs[0]) == derived_hash
    shown = v2.candidate_in(engine.repairs[0])["statements"][0]
    assert "section_heading" in shown and "importance" not in shown
    assert keys.x_repair_key(attempt.attempt_key) not in list(store.list(keys.X_REPAIRS_PREFIX))


# --- what the derivation refuses (the failure branches) ---------------------


def _derived_attempt(store: ArchiveStore, record: dict[str, Any] | None,  # noqa: F811
                     markdown: str, outcome: str = "ok") -> Any:
    from jobhunter.l2.attempts import Attempt
    from jobhunter.l2.bundles import get_bundle
    from jobhunter.l2.rebuild import _derive3
    from jobhunter.l2.v2.source import annotate
    from tests.l2.test_attempts import _attempt

    attempt: Attempt = _attempt(outcome=outcome, record=record)
    return _derive3(attempt, annotate(markdown), markdown, get_bundle("v2"), {})


def test_a_record_the_derivation_refuses_is_an_attribution_failure(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """`migrate.record3_of` refuses anything that is not a schema-2 record, and
    a refusal must never publish: the document keeps its frozen partition and
    files nothing servable under the active one."""
    _, _, record2, markdown = _archive_schema2_attempt(pg, store, "C03")
    broken = json.loads(json.dumps(record2))
    broken["extraction"]["schema_version"] = "9"
    event = _derived_attempt(store, broken, markdown)
    assert event.outcome == "attribution_failed" and event.record is None
    assert "derivation refused" in event.validation[0]["error"]


def test_a_derived_record_that_will_not_verify_is_never_published(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """Validator 20 judges the migrated corpus exactly like a fresh document."""
    from jobhunter.l2 import bundles
    from jobhunter.l2.report import Report
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    _archive_schema2_attempt(pg, store, "C03")
    real = bundles._verify_v2

    def _refuse_schema3(record: Any, md: str, schema_version: str) -> Report:
        if schema_version != "3":
            return real(record, md, schema_version=schema_version)
        report = Report(validator_version="20")
        report.error("binding", "/statements/0", "span_moved")
        return report

    monkeypatch.setattr(bundles, "_verify_v2", _refuse_schema3)
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    frozen = _row(pg, v2.V2_SCHEMA2_TUPLE)
    assert frozen is not None and frozen["status"] == "validated"
    assert frozen["profile"]["schema"] == "2"
    # nothing servable under the active tuple: the read surface never answers
    # with a record validator 20 refused
    migrated = _row(pg, v2.V2_TUPLE)
    assert migrated is not None and migrated["profile"] is None


def test_a_document_the_derivation_refuses_is_quarantined_never_erased(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """A migration has no ladder, so a refusal is FINAL — and a final refusal is
    a row that says so, never a missing one.

    `upsert_state` implements a PENDING fold by deleting the config's row, and a
    migrated fold whose every derived event failed settles pending: the archived
    attempts carry the ladder state of the extraction that SUCCEEDED (attempt 1,
    ladder not exhausted), which is the state of a document that still has rungs
    to climb. It has none — nothing will re-derive it but another replay of the
    same bytes — so the document vanished from the active partition instead of
    landing in it refused. 6,534 documents in the 2026-09-23 production replay,
    each a frozen row with a published candidate and no row at all under the
    tuple the read surface answers from.
    """
    from jobhunter.l2 import bundles
    from jobhunter.l2.report import Report
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    _archive_schema2_attempt(pg, store, "C03")
    real = bundles._verify_v2

    def _refuse_schema3(record: Any, md: str, schema_version: str) -> Report:
        if schema_version != "3":
            return real(record, md, schema_version=schema_version)
        report = Report(validator_version="20")
        report.error("binding", "/statements/0", "span_moved")
        return report

    monkeypatch.setattr(bundles, "_verify_v2", _refuse_schema3)
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()

    migrated = _row(pg, v2.V2_TUPLE)
    assert migrated is not None, "the document was erased from the active tuple"
    assert migrated["status"] == "quarantined"
    assert migrated["profile"] is None and migrated["chosen_attempt"] is None
    # and the refusal says WHY, on the row itself: the migrated partition holds
    # no attempt rows to read a reason off (they keep their archived tuple)
    flags = migrated["flags"] or {}
    assert flags["migration"]["derived"] == "refused"
    assert any("span_moved" in reason for reason in flags["migration"]["reasons"])
    # the frozen partition is the archive's own history and keeps standing
    frozen = _row(pg, v2.V2_SCHEMA2_TUPLE)
    assert frozen is not None and frozen["status"] == "validated"
    assert (frozen["flags"] or {}).get("migration") is None


def test_a_document_mid_ladder_under_schema_2_is_not_parked_by_the_migration(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """The quarantine belongs to a REFUSED derivation, and a document the
    archive holds no candidate for was never derived at all.

    Its schema-2 fold is pending — an attribution failure with rungs left — and
    the drain is still owed those rungs. Settling it here would hand the queue a
    terminal row for a document nothing has finished extracting, so the
    migration leaves it exactly as it found it.
    """
    from datetime import UTC, datetime

    from jobhunter.archive import keys
    from jobhunter.l2.attempts import to_bytes
    from tests.l2 import test_runner_v2 as v2
    from tests.l2.test_attempts import _attempt

    _no_engine(monkeypatch)
    dh = v2.seed_case(pg, "C03")
    started = datetime(2026, 9, 12, 6, 12, 4, tzinfo=UTC)
    attempt = _attempt(
        attempt_key=keys.x_attempt_key(started, dh, 1, 1), document_hash=dh,
        prompt_version=v2.V2_SCHEMA2_TUPLE[0], schema_version="2",
        validator_version=v2.V2_SCHEMA2_TUPLE[2], requested_model=v2.MODEL,
        observed_model=v2.MODEL, record=None, raw_response=None,
        outcome="attribution_failed", ladder_exhausted=False,
        validation=[{"error": "statements[0].evidence: not a literal substring"}],
        started_at="2026-09-12T06:12:04Z", finished_at="2026-09-12T06:12:09Z",
    )
    store.put(attempt.attempt_key, to_bytes(attempt))

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    assert _row(pg, v2.V2_SCHEMA2_TUPLE) is None
    assert _row(pg, v2.V2_TUPLE) is None


def test_a_non_ok_schema2_attempt_carries_its_outcome_across(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The ladder state a fold reads is the same in either shape, so an attempt
    that never produced a record is re-keyed and left alone."""
    _, _, _, markdown = _archive_schema2_attempt(pg, store, "C03")
    from tests.l2 import test_runner_v2 as v2

    event = _derived_attempt(store, None, markdown, outcome="transport")
    assert event.outcome == "transport" and event.record is None
    assert (event.prompt_version, event.schema_version, event.validator_version) \
        == v2.V2_TUPLE


def test_an_archived_repair_of_a_schema2_candidate_is_itself_migrated(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """`_MigratedPhases`: the audit join runs on the schema-2 candidate the
    artifact names, and a repaired candidate it publishes is schema 2 — so the
    return trip derives it too, or the migrated row would publish a schema-2
    record under the active tuple."""
    import gzip

    from jobhunter.archive import keys
    from jobhunter.l2.v2.assemble import candidate_hash
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    _, attempt, record2, markdown = _archive_schema2_attempt(pg, store, "C03")
    base_hash = record2["extraction"]["candidate_hash"]
    repaired = json.loads(json.dumps(record2))
    repaired["statements"][0]["polarity"] = "positive"
    repaired["extraction"]["parent_candidate_hash"] = base_hash
    repaired["extraction"]["candidate_hash"] = ""
    repaired["extraction"]["candidate_hash"] = candidate_hash(repaired)
    store.put(
        keys.x_repair_key(attempt.attempt_key),
        gzip.compress(json.dumps(
            {"candidate_hash": base_hash, "outcome": "repaired", "record": repaired},
            sort_keys=True,
        ).encode("utf-8"), mtime=0),
    )
    # the base candidate's blocking verdict, and the repaired candidate's clean one
    v2.archive_audit(store, attempt.attempt_key, semantics="findings", blocking=1,
                     candidate_hash=base_hash)
    store.put(
        v2.repaired_audit_key(attempt.attempt_key),
        gzip.compress(json.dumps({
            "audit_version": v2.AUDIT_VERSION, "outcome": "ok",
            "attempt_key": attempt.attempt_key, "audit_pass": 1,
            "candidate_hash": repaired["extraction"]["candidate_hash"],
            "semantics": "no_findings", "completeness": "no_findings", "blocking": 0,
            "findings": [], "unresolved": [],
        }, sort_keys=True).encode("utf-8"), mtime=0),
    )

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    row = _row(pg, v2.V2_TUPLE)
    assert row is not None
    assert row["profile"]["schema"] == "3"
    # the repaired candidate, derived: its own polarity, in the schema-3 shape
    served = {s["id"]: s for s in row["profile"]["statements"]}
    repaired3 = {s["id"]: s for s in repaired["statements"]}
    assert served[repaired["statements"][0]["id"]]["polarity"] \
        == repaired3[repaired["statements"][0]["id"]]["polarity"]
    assert row["profile"]["quality"]["search_eligible"] is True


# --- an archived record no store can hold (validator/17, retroactively) -----


HISTORICAL_TUPLE = ("demand-profile/v9", "2", "16")


def _archive_nul_attempt(
    pg: Conn,
    store: ArchiveStore,  # noqa: F811
    case: str = "C01",
    tup: tuple[str, str, str] = HISTORICAL_TUPLE,
) -> tuple[str, Any, dict[str, Any]]:
    """One archived `ok` attempt whose record carries a NUL inside a statement
    topic — the defect validator/17 rules `attribution_failed`, sealed by a
    validator that predates the rule.

    Synthesized, never copied: the two real artifacts stay read-only in the
    production archive.
    """
    from datetime import UTC, datetime

    from jobhunter.archive import keys
    from jobhunter.l2.attempts import to_bytes
    from jobhunter.l2.v2.assemble import assemble, candidate_hash
    from tests.l2 import test_runner_v2 as v2
    from tests.l2.test_attempts import _attempt

    markdown = v2.source(case)
    dh = v2.seed_case(pg, case)
    record = assemble(v2.emit2_of(case), markdown, document_hash=dh,
                      observed_model=v2.MODEL, at="2026-09-12T03:26:18Z",
                      schema_version="2")
    record["statements"][0]["topic"] += "\x00"  # "…at global<NUL>", 2026-09-12
    record["extraction"]["validator_version"] = tup[2]
    record["extraction"]["candidate_hash"] = candidate_hash(record)
    started = datetime(2026, 9, 12, 3, 26, 18, tzinfo=UTC)
    attempt = _attempt(
        attempt_key=keys.x_attempt_key(started, dh, 2, 6), document_hash=dh,
        sample_slot=2, attempt_no=6, ladder_exhausted=True,
        prompt_version=tup[0], schema_version=tup[1],
        validator_version=tup[2], requested_model=v2.MODEL,
        observed_model=v2.MODEL, record=record, outcome="ok",
        raw_response=json.dumps(v2.emit2_of(case)),
        started_at="2026-09-12T03:26:18Z", finished_at="2026-09-12T03:26:31Z",
    )
    store.put(attempt.attempt_key, to_bytes(attempt))
    return dh, attempt, record


def test_rebuild_refuses_a_historical_record_carrying_a_control_character(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """An archived verdict the store cannot hold is not a verdict.

    Two attempts in the production archive were sealed under validators 15 and
    16, before validator/17 added the control-character scan, and their records
    carry a real NUL in a statement topic. The historical branch folds an
    archived verdict as-is, so `upsert_state` handed jsonb a string Postgres
    cannot store and the whole replay died with UntranslatableCharacter. The
    defect is exactly the one validator/17 names, so the fold files it that way
    — under the archived tuple, with the archived attempt object untouched.
    """
    from jobhunter.l2.attempts import from_bytes

    _, attempt, _ = _archive_nul_attempt(pg, store)

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()

    row = _row(pg, HISTORICAL_TUPLE)
    assert row is not None
    # no candidate survives the fold, and the ladder is exhausted: quarantined
    assert row["status"] == "quarantined" and row["chosen_attempt"] is None
    assert row["profile"] is None
    assert "\x00" not in json.dumps(row["profile"])
    # provenance is frozen: the archived attempt still says what it said
    archived = from_bytes(store.get(attempt.attempt_key))
    assert archived == attempt and archived.outcome == "ok"
    prov = pg.execute(
        "SELECT outcome, validator_version FROM extraction_attempts WHERE attempt_key=%s",
        (attempt.attempt_key,),
    ).fetchone()
    assert prov is not None and prov["validator_version"] == HISTORICAL_TUPLE[2]
    # and the fold event itself carries the validator/17 verdict, by path
    from jobhunter.l2.rebuild import _storable_event

    folded = _storable_event(attempt)
    assert folded.outcome == "attribution_failed" and folded.record is None
    assert folded.validation == [
        {"error": "record.statements[0].topic: control character U+0000"
                  " in emitted string"}
    ]


def test_the_live_fold_refuses_to_adopt_a_record_the_store_cannot_hold(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The same hole on the live path: a migrated document's row is folded by
    ADOPTING its archived schema-2 record forward (`runner._Records`), and the
    derivation carries a topic string across untouched — so a record sealed
    before validator/17 would crash the fold that serves it, not just a
    rebuild. It publishes nothing instead, exactly like an adoption the
    migration refuses."""
    from jobhunter.l2.runner import settle
    from tests.l2 import test_runner_v2 as v2

    dh, attempt, _ = _archive_nul_attempt(pg, store, "C01", v2.V2_SCHEMA2_TUPLE)
    # the attempt row the live fold reads it by (replay's own provenance write)
    extraction.record_attempt(pg, attempt, None)
    pg.commit()

    at = utcnow_precise().isoformat()
    state = settle(pg, store, dh, ("z-ai/*",), at, prompt_version=v2.V2_TUPLE[0],
                   schema_version=v2.V2_TUPLE[1], validator_version=v2.V2_TUPLE[2])
    pg.commit()
    assert state.chosen_attempt == attempt.attempt_key
    row = _row(pg, v2.V2_TUPLE)
    assert row is not None and row["profile"] is None  # nothing servable
    assert _mentions(pg, v2.V2_TUPLE) == []


def test_rebuild_refuses_a_repaired_candidate_the_store_cannot_hold(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """A repaired record is read straight out of its artifact and published in
    the base candidate's place (`_ArchivedPhases`), with no assembly between —
    the third door onto the same jsonb column. A patch carrying a control
    character publishes nothing, which is what a refused patch already does:
    the base candidate stands, with its own blocking verdict."""
    import gzip

    from jobhunter.archive import keys
    from jobhunter.l2.v2.assemble import candidate_hash
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    _, attempt, record2, _ = _archive_schema2_attempt(pg, store, "C03")
    base_hash = record2["extraction"]["candidate_hash"]
    repaired = json.loads(json.dumps(record2))
    repaired["statements"][0]["topic"] += "\x00"
    repaired["extraction"]["parent_candidate_hash"] = base_hash
    repaired["extraction"]["candidate_hash"] = ""
    repaired["extraction"]["candidate_hash"] = candidate_hash(repaired)
    store.put(
        keys.x_repair_key(attempt.attempt_key),
        gzip.compress(json.dumps(
            {"candidate_hash": base_hash, "outcome": "repaired", "record": repaired},
            sort_keys=True,
        ).encode("utf-8"), mtime=0),
    )
    v2.archive_audit(store, attempt.attempt_key, semantics="findings", blocking=1,
                     candidate_hash=base_hash)
    store.put(
        v2.repaired_audit_key(attempt.attempt_key),
        gzip.compress(json.dumps({
            "audit_version": v2.AUDIT_VERSION, "outcome": "ok",
            "attempt_key": attempt.attempt_key, "audit_pass": 1,
            "candidate_hash": repaired["extraction"]["candidate_hash"],
            "semantics": "no_findings", "completeness": "no_findings", "blocking": 0,
            "findings": [], "unresolved": [],
        }, sort_keys=True).encode("utf-8"), mtime=0),
    )

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    for tup in (v2.V2_SCHEMA2_TUPLE, v2.V2_TUPLE):
        row = _row(pg, tup)
        assert row is not None and row["profile"] is not None
        assert "\x00" not in json.dumps(row["profile"])
        # the BASE candidate, under the blocking verdict the repair never lifted
        assert row["profile"]["quality"]["semantics"] == "findings"
        assert row["profile"]["quality"]["search_eligible"] is False


# --- validator/20: a bookkeeping-only exhausted ladder serves (T-Q3S9 ac-3) --
# The 271 production documents are MIGRATED rows: their whole ladder ran under
# `(demand-profile/v10, 2)` and failed only block accounting, so the replay
# that owes them a schema-3 row is where they are recovered — offline, from the
# archived raw responses, with no engine call.


def _archive_schema2_ladder(
    pg: Conn, store: ArchiveStore, case: str, emit: dict[str, Any]  # noqa: F811
) -> tuple[str, list[Any]]:
    """Three `(demand-profile/v10, 2)` content failures, the last one exhausting
    the ladder — archived exactly as the drain writes them: no record, the raw
    response, and the verifier's findings (validator 19, the corpus's own)."""
    from datetime import UTC, datetime

    from jobhunter.archive import keys
    from jobhunter.l2.attempts import to_bytes
    from jobhunter.l2.bundles import get_bundle_for_tuple
    from tests.l2 import test_runner_v2 as v2
    from tests.l2.test_attempts import _attempt

    markdown = v2.source(case)
    dh = v2.seed_case(pg, case)
    bundle = get_bundle_for_tuple(v2.V2_SCHEMA2_TUPLE[0], "2")
    ladder = []
    for no in (1, 2, 3):
        started = datetime(2026, 9, 12, 6, 12, no, tzinfo=UTC)
        record = bundle.assemble(emit, markdown, document_hash=dh, observed_model=v2.MODEL,
                                 normalizer_version="md/1", at=started.isoformat())
        findings = [
            {"check": f.check, "path": f.path, "code": f.code, "severity": f.severity,
             "detail": f.detail}
            for f in bundle.verify(record, markdown).findings
        ]
        attempt = _attempt(
            attempt_key=keys.x_attempt_key(started, dh, 1, no), document_hash=dh,
            prompt_version=v2.V2_SCHEMA2_TUPLE[0], schema_version="2", validator_version="19",
            requested_model=v2.MODEL, observed_model=v2.MODEL, record=None,
            raw_response=json.dumps(emit), outcome="attribution_failed", attempt_no=no,
            ladder_exhausted=no == 3, validation=findings,
            started_at=started.isoformat(), finished_at=started.isoformat(),
        )
        store.put(attempt.attempt_key, to_bytes(attempt))
        ladder.append(attempt)
    return dh, ladder


def test_rebuild_settles_a_bookkeeping_only_schema2_ladder_in_both_partitions(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    from tests.l2 import test_runner_v2 as v2
    from tests.l2.test_runner_v20_bookkeeping import bookkeeping_gap

    _no_engine(monkeypatch)
    _, ladder = _archive_schema2_ladder(pg, store, "C04", bookkeeping_gap(v2.emit2_of("C04")))

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    for tup, schema, rows in ((v2.V2_SCHEMA2_TUPLE, "2", v2.C04_ROWS_V2),
                              (v2.V2_TUPLE, "3", v2.C04_ROWS)):
        row = _row(pg, tup)
        assert row is not None, tup
        assert row["status"] == "validated", tup
        assert row["chosen_attempt"] == ladder[-1].attempt_key
        assert row["profile"]["schema"] == schema
        quality = row["profile"]["quality"]
        assert quality["completeness"] == "accounting_gaps"
        assert quality["search_eligible"] is False
        assert [g["code"] for g in quality["accounting_gaps"]] == ["coverage_unevidenced"]
        assert _mentions(pg, tup) == rows
    # a settled migrated row is not a refusal: nothing to note on it
    migrated = _row(pg, v2.V2_TUPLE)
    assert migrated is not None and (migrated["flags"] or {}).get("migration") is None
    assert "importance" not in _record_keys(migrated["profile"])


def test_rebuild_keeps_quarantining_a_schema2_ladder_with_a_non_accounting_finding(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    from tests.l2 import test_runner_v2 as v2
    from tests.l2.test_runner_v20_bookkeeping import unknown_reference

    _no_engine(monkeypatch)
    _archive_schema2_ladder(pg, store, "C04", unknown_reference(v2.emit2_of("C04")))

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    for tup in (v2.V2_SCHEMA2_TUPLE, v2.V2_TUPLE):
        row = _row(pg, tup)
        assert row is not None and row["status"] == "quarantined", tup
        assert row["profile"] is None and row["chosen_attempt"] is None
        assert _mentions(pg, tup) == []


def test_a_live_settle_of_a_migrated_bookkeeping_row_keeps_the_replayed_accounting_verdict(
    pg: Conn, store: ArchiveStore, monkeypatch: Any  # noqa: F811
) -> None:
    """One shared rule: the drain re-folds a migrated row by reading its schema-2
    attempts (`Bundle.migrated_from`), and must recover the SAME candidate the
    replay did — same hash, same gaps — or the row would flip on the next
    review, re-audit or catch-up."""
    from jobhunter.l2.bundles import get_bundle
    from jobhunter.l2.runner import settle
    from jobhunter.timeutil import iso
    from tests.l2 import test_runner_v2 as v2
    from tests.l2.test_runner_v20_bookkeeping import bookkeeping_gap

    _no_engine(monkeypatch)
    dh, _ = _archive_schema2_ladder(pg, store, "C04", bookkeeping_gap(v2.emit2_of("C04")))
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    replayed = _row(pg, v2.V2_TUPLE)
    assert replayed is not None and replayed["status"] == "validated"

    settle(pg, store, dh, ("z-ai/*",), iso(utcnow_precise()), bundle=get_bundle("v2"))
    pg.commit()
    live = _row(pg, v2.V2_TUPLE)
    assert live is not None
    for column in ("status", "chosen_attempt", "profile", "flags"):
        assert live[column] == replayed[column], column
    assert _mentions(pg, v2.V2_TUPLE) == v2.C04_ROWS


def test_rebuild_reproduces_a_live_bookkeeping_settlement_row_for_row(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The native (v11, 3) path: a drain settles the bookkeeping-only ladder, and
    `extract rebuild` re-derives the identical row from the archive."""
    from tests.l2 import test_runner_v2 as v2
    from tests.l2.test_runner_v20_bookkeeping import bookkeeping_gap

    v2.seed_case(pg, "C04")
    engine = v2.AuditingEngine([v2.result(bookkeeping_gap(v2.emit_of("C04")))] * 3,
                               v2.clean_audit)
    run(v2.v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    pg.commit()
    live = _dump(pg)
    assert [r["status"] for r in live["extractions"]] == ["validated"]
    mentions = _mentions(pg, v2.V2_TUPLE)

    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    assert _dump(pg) == live
    assert _mentions(pg, v2.V2_TUPLE) == mentions == v2.C04_ROWS


@pytest.mark.parametrize(("check", "code"), [
    ("accounting", "coverage_unevidenced"),  # a refusal the rule's findings test admits
    ("binding", "span_moved"),
], ids=["accounting_refusal", "binding_refusal"])
def test_a_refused_derivation_is_never_settled_by_the_bookkeeping_rule(
    pg: Conn, store: ArchiveStore, monkeypatch: Any, check: str, code: str  # noqa: F811
) -> None:
    """The rule speaks for a REAL exhausted ladder: attempts that each failed,
    were fed back, and ran out. A refused derivation has none — its schema-2
    candidate passed, and only `_spent` marks the derived events exhausted, so
    the fold writes the refusal instead of erasing the document. Such a document
    was never asked to fix anything, so it stays the quarantined, reasoned row
    the migration writes for every refusal, whatever the refusal was about."""
    from jobhunter.l2 import bundles
    from jobhunter.l2.report import Report
    from tests.l2 import test_runner_v2 as v2

    _no_engine(monkeypatch)
    _archive_schema2_attempt(pg, store, "C03")
    real = bundles._verify_v2

    def _refuse_schema3(record: Any, md: str, schema_version: str) -> Report:
        if schema_version != "3":
            return real(record, md, schema_version=schema_version)
        report = Report(validator_version="20")
        report.error(check, "/blocks/0", code)
        return report

    monkeypatch.setattr(bundles, "_verify_v2", _refuse_schema3)
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()

    migrated = _row(pg, v2.V2_TUPLE)
    assert migrated is not None, "the document was erased from the active tuple"
    assert migrated["status"] == "quarantined"
    assert migrated["profile"] is None and migrated["chosen_attempt"] is None
    reasons = (migrated["flags"] or {})["migration"]
    assert reasons["derived"] == "refused"
    assert any(code in reason for reason in reasons["reasons"])
    frozen = _row(pg, v2.V2_SCHEMA2_TUPLE)
    assert frozen is not None and frozen["status"] == "validated"
