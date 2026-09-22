"""extract rebuild must reproduce the incrementally-built surface row for row —
the increment's recomputability assertion."""

import json
from typing import Any

import psycopg

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
    # nothing servable under the active tuple: the read surface answers with the
    # document's absence, never with a record validator 20 refused
    migrated = _row(pg, v2.V2_TUPLE)
    assert migrated is None or migrated["profile"] is None


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
