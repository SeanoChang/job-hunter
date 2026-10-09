"""The two v20 operator scripts, driven the way the operator cannot be: against
the test database and a fake engine.

Neither script may be RUN for real from here — the migration report takes the
extract lock on the production store and the live gate spends codex calls — so
what is tested is everything up to that: the counts, the pass/fail predicates,
the document selection, and the promise that the offline half calls no engine.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jobhunter.archive.base import ArchiveStore
from tests.conftest import TEST_DSN
from tests.l2.test_runner import store  # noqa: F401

Conn = psycopg.Connection[dict[str, Any]]
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _script(name: str) -> Any:
    """One `scripts/*.py` as a module. They are operator entry points, not part
    of the package, so they are loaded by path rather than imported."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def report_script() -> Any:
    return _script("migrate_v20_report")


@pytest.fixture
def gate_script() -> Any:
    return _script("live_gate_v20")


class _Kept:
    """The test connection, behind a `close()` the script's `finally` cannot
    honour — `main` owns its connection and the fixture owns this one."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def close(self) -> None:
        return None


def _as_operator(monkeypatch: pytest.MonkeyPatch, pg: Conn, **env: str) -> Any:
    """Drive a script's `main()` against the test surface: its own `Settings`
    and its own connection, so the exit code the operator sees is the one
    under test."""
    from jobhunter.config import Settings
    from jobhunter.store import db
    from tests.l2 import test_runner_v2 as v2

    settings = v2.v2_settings(**env)
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(Settings, "require_database_url", lambda self: "postgresql:///test")
    monkeypatch.setattr(db, "connect", lambda *a, **k: _Kept(pg))
    return settings


# --- the migration report (ac-2) -------------------------------------------


def _seed_migrated(pg: Conn, store: ArchiveStore) -> str:  # noqa: F811
    """One archived schema-2 attempt, replayed — so the surface holds both the
    frozen partition and the derived one, exactly as the migration leaves it."""
    from jobhunter.l2.rebuild import rebuild_extractions
    from tests.l2.test_rebuild import _archive_schema2_attempt

    dh, _, _, _ = _archive_schema2_attempt(pg, store, "C03")
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    return dh


def test_the_report_counts_rows_per_status_and_per_tuple(
    pg: Conn, store: ArchiveStore, report_script: Any  # noqa: F811
) -> None:
    from tests.l2 import test_runner_v2 as v2

    _seed_migrated(pg, store)
    counts = report_script.counts(pg)
    assert counts.by_status["validated"] == 2  # frozen + derived
    assert counts.by_tuple["/".join(v2.V2_SCHEMA2_TUPLE)] == 1
    assert counts.by_tuple["/".join(v2.V2_TUPLE)] == 1
    assert counts.active_by_status == {"validated": 1}
    assert counts.docs_serving_skills == 1
    assert counts.review_share == 0.0
    assert counts.schema2_rows_under_active == 0
    assert counts.documents_not_migrated == 0
    assert counts.documents_owed_migration == 0


def test_check_passes_a_fully_migrated_surface(
    pg: Conn, store: ArchiveStore, report_script: Any  # noqa: F811
) -> None:
    _seed_migrated(pg, store)
    assert report_script.check_failures(report_script.counts(pg), engine_calls=0) == []


def test_check_fails_a_corpus_the_migration_never_touched(
    pg: Conn, store: ArchiveStore, report_script: Any  # noqa: F811
) -> None:
    """ac-2's predicate has to tell "the migration ran" from "the migration
    never started". Both of the original terms were satisfied by an untouched
    surface: no code path can store a schema-2 blob under the active tuple, and
    `--check` runs no replay so its engine counter is zero by construction."""
    from tests.l2 import test_runner_v2 as v2

    _seed_migrated(pg, store)
    pg.execute(
        "DELETE FROM extractions WHERE prompt_version=%s AND schema_version=%s"
        " AND validator_version=%s", v2.V2_TUPLE,
    )
    pg.commit()
    counts = report_script.counts(pg)
    assert counts.documents_owed_migration == 1
    assert counts.schema2_rows_under_active == 0  # the old terms are still silent
    failures = report_script.check_failures(counts, engine_calls=0)
    assert len(failures) == 1 and "no row under the active tuple" in failures[0]


def test_check_exits_non_zero_on_a_corpus_the_migration_never_touched(
    pg: Conn, store: ArchiveStore, report_script: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """ac-2's predicate is this exit code. It was 0 on a surface the migration
    had never run against — and on an empty database."""
    from tests.l2 import test_runner_v2 as v2

    _seed_migrated(pg, store)
    pg.execute(
        "DELETE FROM extractions WHERE prompt_version=%s AND schema_version=%s"
        " AND validator_version=%s", v2.V2_TUPLE,
    )
    pg.commit()
    _as_operator(monkeypatch, pg)
    assert report_script.main(["--check"]) == 1
    assert capsys.readouterr().out.rstrip().endswith("FAIL")


def _seed_refused(pg: Conn, store: ArchiveStore,  # noqa: F811
                  monkeypatch: pytest.MonkeyPatch) -> str:
    """A document the schema-3 derivation refuses, replayed: the migration
    leaves it quarantined under the active tuple with its reasons on the row."""
    from jobhunter.l2 import bundles
    from jobhunter.l2.rebuild import rebuild_extractions
    from jobhunter.l2.report import Report
    from tests.l2.test_rebuild import _archive_schema2_attempt

    dh, _, _, _ = _archive_schema2_attempt(pg, store, "C03")
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
    return dh


def test_the_report_counts_the_documents_the_derivation_refused(
    pg: Conn, store: ArchiveStore, report_script: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal is a fact about a document, not a hole in the migration: it has
    a row, so the completeness term is satisfied, and the operator still has to
    see how many were refused and what for."""
    _seed_refused(pg, store, monkeypatch)
    counts = report_script.counts(pg)
    assert counts.documents_owed_migration == 0
    assert counts.active_by_status == {"quarantined": 1}
    assert counts.documents_refused_by_derivation == 1
    assert counts.refusal_reasons[0][1] == 1
    assert "span_moved" in counts.refusal_reasons[0][0]
    lines = "\n".join(report_script._counts_lines("now", counts))
    assert "documents refused by derivation: 1" in lines
    assert "span_moved" in lines


def test_check_passes_a_surface_whose_refusals_all_have_rows(
    pg: Conn, store: ArchiveStore, report_script: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """The predicate fails for documents with NO row. A refused document has
    one, and hiding it behind a FAIL would make the operator's only completeness
    signal fire on the one thing the migration cannot do anything about."""
    _seed_refused(pg, store, monkeypatch)
    _as_operator(monkeypatch, pg)
    assert report_script.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "documents refused by derivation: 1" in out
    assert out.rstrip().endswith("PASS")


def test_check_exits_zero_on_a_migrated_corpus(
    pg: Conn, store: ArchiveStore, report_script: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    _seed_migrated(pg, store)
    _as_operator(monkeypatch, pg)
    assert report_script.main(["--check"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("PASS")


def test_check_fails_an_empty_surface_that_holds_a_migratable_partition(
    pg: Conn, store: ArchiveStore, report_script: Any  # noqa: F811
) -> None:
    """The same predicate on the pre-migration surface the operator actually
    starts from: the frozen partition alone, nothing derived."""
    from jobhunter.l2 import rebuild as rebuild_mod
    from tests.l2.test_rebuild import _archive_schema2_attempt

    _archive_schema2_attempt(pg, store, "C03")
    original = rebuild_mod._migration_target
    rebuild_mod._migration_target = lambda bundle: None  # type: ignore[assignment]
    try:
        rebuild_mod.rebuild_extractions(pg, store, ("z-ai/*",))
    finally:
        rebuild_mod._migration_target = original  # type: ignore[assignment]
    pg.commit()
    counts = report_script.counts(pg)
    assert counts.active_by_status == {}
    assert report_script.check_failures(counts, engine_calls=0) != []


def test_check_fails_when_a_schema2_blob_sits_under_the_active_tuple(
    pg: Conn, store: ArchiveStore, report_script: Any  # noqa: F811
) -> None:
    """The failure the migration exists to remove: a row the read surface
    answers from, holding the shape the contract retired."""
    from tests.l2 import test_runner_v2 as v2

    _seed_migrated(pg, store)
    pg.execute(
        "UPDATE extractions SET profile = jsonb_set(profile, '{schema}', '\"2\"')"
        " WHERE prompt_version=%s AND schema_version=%s AND validator_version=%s",
        v2.V2_TUPLE,
    )
    pg.commit()
    counts = report_script.counts(pg)
    assert counts.schema2_rows_under_active == 1
    failures = report_script.check_failures(counts, engine_calls=0)
    assert len(failures) == 1 and "schema-2" in failures[0]


def test_check_fails_when_the_engine_counter_moved(
    pg: Conn, store: ArchiveStore, report_script: Any  # noqa: F811
) -> None:
    """Migration is offline and total: one engine call is a re-extraction."""
    _seed_migrated(pg, store)
    failures = report_script.check_failures(report_script.counts(pg), engine_calls=3)
    assert len(failures) == 1 and "engine" in failures[0]


def test_the_engine_counter_sees_a_call_through_any_engine(
    report_script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobhunter.l2 import engines

    monkeypatch.setattr(engines.CodexCli, "complete", lambda *a, **k: None)
    counter = report_script.EngineCalls()
    with counter.installed():
        engines.CodexCli().complete("p", {}, "m")
    assert counter.n == 1


def test_the_report_runs_the_replay_and_reports_before_and_after(
    pg: Conn, store: ArchiveStore, report_script: Any  # noqa: F811
) -> None:
    """The operator's actual invocation, minus the production store: the replay
    runs, the engine counter stays at zero, and the two snapshots differ."""
    from tests.l2.test_rebuild import _archive_schema2_attempt

    _archive_schema2_attempt(pg, store, "C03")
    result = report_script.run_report(pg, store, ("z-ai/*",))
    pg.commit()
    assert result.engine_calls == 0
    assert result.before.by_tuple == {}
    assert result.after.docs_serving_skills == 1
    assert "docs now serving skills" in report_script.render(result)


def test_the_dry_run_writes_no_row(
    pg: Conn, store: ArchiveStore, report_script: Any  # noqa: F811
) -> None:
    from tests.l2.test_rebuild import _archive_schema2_attempt

    _archive_schema2_attempt(pg, store, "C03")
    plan = report_script.plan(pg)
    assert "extract rebuild" in plan
    assert pg.execute("SELECT count(*) AS n FROM extractions").fetchone()["n"] == 0


# --- the live gate (ac-3) ---------------------------------------------------


def _row(dh: str, **over: Any) -> dict[str, Any]:
    statement = {
        "id": "s1", "kind": "qualification", "subject": "candidate",
        "topic": "Python", "evidence": [], "section_heading": "Requirements",
        "modality_evidence": [{"block_id": "b000001", "text": "Must have",
                               "span": [0, 9], "occurrence": 0}],
        "polarity": "positive", "polarity_evidence": None,
        "condition_ids": [], "fact_ids": [], "unresolved": [],
    }
    base = {
        "document_hash": dh, "status": "validated", "k": 1,
        "profile": {"schema": "3", "statements": [statement], "relations": {},
                    "facts": {"entries": []}, "mentions": [],
                    "quality": {"semantics": "no_findings",
                                "completeness": "no_findings"},
                    "demand_profile": {"areas": [{"id": "s1", "importance": "contextual"}]}},
        "flags": {"audit": "done"},
    }
    base.update(over)
    return base


ON_SLOT = "00000000" + "a" * 56  # int(dh[:8], 16) % 20 == 0
OFF_SLOT = "00000001" + "a" * 56  # ... % 20 == 1


def test_the_gate_passes_a_clean_drain(gate_script: Any) -> None:
    checks = gate_script.gate_checks([ON_SLOT, OFF_SLOT],
                                     [_row(ON_SLOT, k=3), _row(OFF_SLOT)],
                                     audit_mod=20, docs=2)
    assert [c.name for c in checks if not c.ok] == []
    assert gate_script.verdict(checks) == "PASS"


def test_the_gate_fails_a_document_that_never_settled(gate_script: Any) -> None:
    checks = gate_script.gate_checks([ON_SLOT, OFF_SLOT], [_row(ON_SLOT)],
                                     audit_mod=20, docs=2)
    failed = {c.name for c in checks if not c.ok}
    assert failed == {"terminal"}
    assert gate_script.verdict(checks) == "FAIL"


def test_the_gate_fails_a_drain_that_never_happened(gate_script: Any) -> None:
    """Every other check is `nothing wrong in this list`, so an empty selection
    satisfies all of them: without this the gate printed five ok lines and
    exited 0 having called the engine zero times."""
    checks = gate_script.gate_checks([], [], audit_mod=20, docs=25)
    assert gate_script.verdict(checks) == "FAIL"
    assert {c.name for c in checks if not c.ok} == {"drained"}
    assert "0/25" in next(c.detail for c in checks if c.name == "drained")


def test_the_gate_fails_when_the_extract_lock_was_held(gate_script: Any) -> None:
    """The operator's actual situation: `runner.run` returns an empty queue the
    moment the writer lock is taken, which is exactly when a drain is running."""
    checks = gate_script.gate_checks(
        [], [], audit_mod=20, docs=25,
        summary={"extraction": {"lock_held": True, "queued": []}},
    )
    failed = {c.name: c.detail for c in checks if not c.ok}
    assert list(failed) == ["drained"]
    assert "lock" in failed["drained"]


def test_the_gate_fails_a_short_queue(gate_script: Any) -> None:
    """ac-3 is 25 fresh documents; a gate that passes on three measured three."""
    checks = gate_script.gate_checks([ON_SLOT], [_row(ON_SLOT)], audit_mod=20, docs=25)
    assert {c.name for c in checks if not c.ok} == {"drained"}


def test_the_gate_fails_a_run_the_breaker_aborted(gate_script: Any) -> None:
    checks = gate_script.gate_checks(
        [ON_SLOT], [_row(ON_SLOT)], audit_mod=20, docs=1,
        summary={"extraction": {"breaker_abort": True}},
    )
    assert {c.name for c in checks if not c.ok} == {"drained"}


def test_the_gate_fails_an_audit_machinery_error(gate_script: Any) -> None:
    row = _row(ON_SLOT)
    row["profile"]["quality"]["semantics"] = "error"
    checks = gate_script.gate_checks([ON_SLOT], [row], audit_mod=20, docs=1)
    assert {c.name for c in checks if not c.ok} == {"audit_machinery"}


def test_the_gate_fails_a_k_sample_off_the_audit_slot(gate_script: Any) -> None:
    """Sampling is monitoring under validator 20: only the 5% slot takes extra
    samples, and a cohort anywhere else is the reprompt escalation coming back."""
    checks = gate_script.gate_checks([OFF_SLOT], [_row(OFF_SLOT, k=3)],
                                     audit_mod=20, docs=1)
    assert {c.name for c in checks if not c.ok} == {"k_samples_on_slot"}


def test_the_gate_fails_a_record_without_a_section_heading(gate_script: Any) -> None:
    row = _row(ON_SLOT)
    del row["profile"]["statements"][0]["section_heading"]
    checks = gate_script.gate_checks([ON_SLOT], [row], audit_mod=20, docs=1)
    assert {c.name for c in checks if not c.ok} == {"record_shape"}


def test_the_gate_fails_an_unquoted_modality(gate_script: Any) -> None:
    row = _row(ON_SLOT)
    row["profile"]["statements"][0]["modality_evidence"] = [{"block_id": "b1", "text": None}]
    checks = gate_script.gate_checks([ON_SLOT], [row], audit_mod=20, docs=1)
    assert {c.name for c in checks if not c.ok} == {"record_shape"}


def test_the_gate_fails_an_importance_key_in_a_served_record(gate_script: Any) -> None:
    row = _row(ON_SLOT)
    row["profile"]["statements"][0]["importance"] = "required"
    checks = gate_script.gate_checks([ON_SLOT], [row], audit_mod=20, docs=1)
    assert {c.name for c in checks if not c.ok} == {"no_importance"}


def test_the_dry_run_selects_documents_and_calls_no_engine(
    pg: Conn, store: ArchiveStore, gate_script: Any  # noqa: F811
) -> None:
    from tests.l2 import test_runner_v2 as v2

    dh = v2.seed_case(pg, "C03")
    pg.commit()

    class Boom:
        name = "boom"

        def complete(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("--dry-run called the engine")

    result = gate_script.gate(v2.v2_settings(), pg, store, engine=Boom(), docs=1, dry_run=True)
    assert result.selected == [dh]
    assert result.checks == []
    assert "would drain" in result.plan and "WARNING" not in result.plan


def test_the_dry_run_says_so_when_the_drain_cannot_happen(
    pg: Conn, store: ArchiveStore, gate_script: Any  # noqa: F811
) -> None:
    """`would drain 0 fresh document(s)` reads like an empty corpus. When the
    writer lock is held — a drain running, the operator's normal state — the
    plan has to say that instead."""
    from jobhunter.store import db
    from tests.l2 import test_runner_v2 as v2

    class Named:
        name = "boom"

    v2.seed_case(pg, "C03")
    pg.commit()
    holder = psycopg.connect(TEST_DSN)  # info.dsn drops the password
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", (db.EXTRACT_LOCK_KEY,))
        holder.commit()
        result = gate_script.gate(v2.v2_settings(), pg, store, engine=Named(),
                                  docs=25, dry_run=True)
    finally:
        holder.close()
    assert result.selected == []
    assert "WARNING" in result.plan and "lock" in result.plan


def test_the_gate_fails_when_a_drain_holds_the_writer_lock(
    pg: Conn, store: ArchiveStore, gate_script: Any  # noqa: F811
) -> None:
    """End to end: the lock is held, no engine is called, and the gate says FAIL
    rather than passing five vacuous checks."""
    from jobhunter.store import db
    from tests.l2 import test_runner_v2 as v2

    class Boom:
        name = "boom"

        def complete(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("the gate called the engine with the lock held")

    v2.seed_case(pg, "C03")
    pg.commit()
    holder = psycopg.connect(TEST_DSN)  # info.dsn drops the password
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", (db.EXTRACT_LOCK_KEY,))
        holder.commit()
        result = gate_script.gate(v2.v2_settings(), pg, store, engine=Boom(),
                                  docs=25, dry_run=False)
    finally:
        holder.close()
    assert result.selected == []
    assert gate_script.verdict(result.checks) == "FAIL"
    assert [c.name for c in result.checks if not c.ok] == ["drained"]


def test_the_gate_exits_non_zero_when_the_drain_never_ran(
    pg: Conn, store: ArchiveStore, gate_script: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """ac-3's predicate is this exit code, and it was 0 with the writer lock
    held and the engine never called."""
    from jobhunter.store import db
    from tests.l2 import test_runner_v2 as v2

    v2.seed_case(pg, "C03")
    pg.commit()
    _as_operator(monkeypatch, pg, JOB_HUNTER_L2_ENGINE="codex-cli")
    holder = psycopg.connect(TEST_DSN)  # info.dsn drops the password
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", (db.EXTRACT_LOCK_KEY,))
        holder.commit()
        assert gate_script.main(["--docs", "25"]) == 1
    finally:
        holder.close()
    assert "FAIL  drained" in capsys.readouterr().out


def test_the_gate_drains_and_checks_a_real_document(
    pg: Conn, store: ArchiveStore, gate_script: Any  # noqa: F811
) -> None:
    """End to end on the gate DB with a scripted engine: the selection, the
    drain, and every check reading the row the drain wrote."""
    from tests.l2 import test_runner_v2 as v2

    v2.seed_case(pg, "C03")
    pg.commit()
    # audit mod 1 puts every document on the sampling slot, so the cohort the
    # gate's k-sample check reads is a real one
    engine = v2.AuditingEngine([v2.result(v2.emit_of("C03"))] * 3, v2.clean_audit)
    result = gate_script.gate(v2.v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store,
                              engine=engine, docs=1, dry_run=False)
    assert len(result.selected) == 1
    row = pg.execute("SELECT k, status FROM extractions").fetchone()
    assert row is not None and row["status"] == "validated" and row["k"] == 3
    assert gate_script.verdict(result.checks) == "PASS", [
        (c.name, c.detail) for c in result.checks if not c.ok
    ]
    assert json.dumps(result.summary)  # the printed payload is JSON-serialisable
