"""Integration: the whole drain loop over Postgres + LocalFS archive with a
scripted fake engine. No network, no LLM."""

import copy
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from jobhunter.archive import keys, open_store
from jobhunter.archive.base import ArchiveStore
from jobhunter.config import Settings
from jobhunter.hashing import sha256_hex
from jobhunter.l2.attempts import from_bytes, to_bytes
from jobhunter.l2.engines import (
    EngineAuthError,
    EngineFatalError,
    EngineResult,
    EngineThrottled,
    EngineTransportError,
)
from jobhunter.l2.prompt import PROMPT_VERSION
from jobhunter.l2.runner import LockLost, run
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.store import db
from jobhunter.timeutil import iso
from tests.conftest import TEST_DSN
from tests.l2.conftest import DOC_MD
from tests.l2.test_assemble import EMIT
from tests.l2.test_attempts import _attempt

Conn = psycopg.Connection[dict[str, Any]]

DH = sha256_hex(DOC_MD.encode("utf-8"))
GOOD = EngineResult(
    raw_text=json.dumps(EMIT),
    observed_model="z-ai/glm-5.2:free",
    input_tokens=40,
    output_tokens=9,
    cost_usd=0.0,
)


class FakeEngine:
    name = "fake"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        self.calls.append(model)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, EngineResult)
        return item


class FlakyConn:
    """A live connection that dies the way Neon's idle-kill kills one.

    The first statement whose SQL contains `die_on` raises OperationalError, and
    every statement after that raises too — the backend is gone. Everything else
    is the real connection, so assertions still read real rows.
    """

    def __init__(self, real: Conn, die_on: str) -> None:
        self._real = real
        self._die_on = die_on
        self.dead = False
        self.closed = False

    def _check(self, sql: object = "") -> None:
        if self.dead:
            raise psycopg.OperationalError("the connection is lost")
        if self._die_on in str(sql):
            self.dead = True
            raise psycopg.OperationalError("the connection is lost")

    def execute(self, query: Any, params: Any = None, **kw: Any) -> Any:
        self._check(query)
        return self._real.execute(query, params, **kw)

    def cursor(self, *a: Any, **kw: Any) -> Any:
        self._check()
        return self._real.cursor(*a, **kw)

    def commit(self) -> None:
        self._check()
        self._real.commit()

    def rollback(self) -> None:
        self._real.rollback()

    def close(self) -> None:
        self.closed = True  # the pg fixture owns the real connection


def _terminate(killer: Conn, pid: int) -> None:
    """End a backend the way Neon's idle suspend ends one, and wait for it to go.

    Not a simulation: the terminated session's uncommitted rows are really rolled
    back, which is the whole point — a simulated death that keeps writing into the
    same live transaction cannot lose anything.
    """
    killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
    killer.commit()
    for _ in range(500):
        row = killer.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE pid = %s", (pid,)
        ).fetchone()
        killer.commit()
        if row and row["n"] == 0:
            return
        time.sleep(0.01)
    raise AssertionError(f"backend {pid} did not die")


class KillingConn:
    """A real connection whose own backend is terminated at a chosen moment.

    `die_on` names a SQL fragment: the kill lands on that statement, or — with
    `at_next_commit` — on the first commit after it. Everything else is the real
    connection, so assertions read real rows.
    """

    def __init__(self, real: Conn, killer: Conn, die_on: str, *,
                 at_next_commit: bool = False) -> None:
        self._real = real
        self._killer = killer
        self._die_on = die_on
        self._at_next_commit = at_next_commit
        row = real.execute("SELECT pg_backend_pid() AS p").fetchone()
        assert row is not None
        self._pid = int(row["p"])
        self._armed = False
        self.fired = False
        self.closed = False

    def _kill(self) -> None:
        if self.fired:
            return
        self.fired = True
        _terminate(self._killer, self._pid)

    def execute(self, query: Any, params: Any = None, **kw: Any) -> Any:
        if not self.fired and self._die_on in str(query):
            if self._at_next_commit:
                self._armed = True
            else:
                self._kill()
        return self._real.execute(query, params, **kw)

    def cursor(self, *a: Any, **kw: Any) -> Any:
        return self._real.cursor(*a, **kw)

    def commit(self) -> None:
        if self._armed:
            self._kill()
        self._real.commit()

    def rollback(self) -> None:
        self._real.rollback()

    def close(self) -> None:
        self.closed = True
        self._real.close()


def _settings(**env: str) -> Settings:
    return Settings.load(
        {
            "JOB_HUNTER_ARCHIVE_URL": "file:///unused",
            "JOB_HUNTER_L2_MODELS": "z-ai/*",
            "JOB_HUNTER_L2_MODEL_CANDIDATES": "z-ai/glm-5.2:free",
            **env,
        }
    )


def _seed_doc(pg: Conn, dh: str = DH, markdown: str = DOC_MD, uid: str = "gh:x:1") -> None:
    now = datetime.now(UTC)
    pg.execute(
        "INSERT INTO fetch_attempts (attempt_id, run_id, source, board, started_at,"
        " finished_at, transport, health, adapter_version, registry_revision, cli_version)"
        " VALUES ('att-'||%s,'r1','greenhouse','x',%s,%s,'ok','ok','g/1','rev','0')"
        " ON CONFLICT DO NOTHING",
        (uid, now, now),
    )
    vh = "v-" + uid
    pg.execute(
        "INSERT INTO posting_versions (version_hash, version_hash_v, uid, source, board,"
        " source_id, title, company, locations, first_seen_attempt)"
        " VALUES (%s,1,%s,'greenhouse','x',%s,'t','c','[]','att-'||%s)",
        (vh, uid, uid.split(":")[-1], uid),
    )
    pg.execute(
        "INSERT INTO documents (version_hash, normalizer_version, document_hash, markdown)"
        " VALUES (%s,'md/1',%s,%s)",
        (vh, dh, markdown),
    )
    pg.execute(
        "INSERT INTO postings (uid, source, board, source_id, status, current_version_hash,"
        " first_seen_attempt, first_seen_at, last_seen_attempt, last_seen_at)"
        " VALUES (%s,'greenhouse','x',%s,'open',%s,'att-'||%s,%s,'att-'||%s,%s)",
        (uid, uid.split(":")[-1], vh, uid, now, uid, now),
    )
    pg.commit()


@pytest.fixture
def store(tmp_path: Any) -> ArchiveStore:
    return open_store(f"file://{tmp_path}/archive")


def _state_row(pg: Conn) -> dict[str, Any] | None:
    return pg.execute("SELECT * FROM extractions").fetchone()


def test_valid_document_validates(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    engine = FakeEngine([GOOD])
    summary = run(_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1 and summary.docs_attempted == 1
    row = _state_row(pg)
    assert row and row["status"] == "validated" and row["model"] == "z-ai/glm-5.2:free"
    assert row["profile"]["demand_profile"]["areas"][0]["id"] == "a1"
    archived = list(store.list(keys.X_ATTEMPTS_PREFIX))
    assert len(archived) == 1
    attempt = from_bytes(store.get(archived[0]))
    assert attempt.outcome == "ok" and attempt.record is not None


def test_ladder_escalation_with_prior_errors(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    bad = EngineResult("not json", "z-ai/glm-5.2:free", 1, 1, 0.0)
    engine = FakeEngine([bad, bad, bad, GOOD])
    settings = _settings(
        **{"JOB_HUNTER_L2_MODEL_CANDIDATES": "z-ai/glm-5.2:free, z-ai/glm-5.2"}
    )
    summary = run(settings, pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1
    assert engine.calls == ["z-ai/glm-5.2:free"] * 3 + ["z-ai/glm-5.2"]
    archived = sorted(store.list(keys.X_ATTEMPTS_PREFIX))
    attempts = [from_bytes(store.get(k)) for k in archived]
    assert [a.outcome for a in attempts].count("schema_invalid") == 3
    second = next(a for a in attempts if a.attempt_no == 2)
    assert second.prior_errors and "not valid JSON" in second.prior_errors[0]


def test_fabricated_quote_repaired_on_retry(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    emit_bad = copy.deepcopy(EMIT)
    emit_bad["demand_profile"]["areas"][0]["claims"][0]["quote"] = {"text": "Rust experience"}
    bad = EngineResult(json.dumps(emit_bad), "z-ai/glm-5.2:free", 1, 1, 0.0)
    # the reprompt escalates to k=3 (spec §4.5): two extra samples follow
    summary = run(_settings(), pg, store, engine=FakeEngine([bad, GOOD, GOOD, GOOD]),
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1
    attempts = [from_bytes(store.get(k)) for k in sorted(store.list(keys.X_ATTEMPTS_PREFIX))]
    first = next(a for a in attempts if a.attempt_no == 1)
    assert first.outcome == "attribution_failed"
    produced = [v["error"] for v in first.validation if "error" in v]
    assert any("matches the document for" in e for e in produced)
    second = next(a for a in attempts if a.attempt_no == 2)
    assert first.prior_errors == []  # nothing was fed into attempt 1
    assert any("matches the document for" in e for e in second.prior_errors)  # fed into retry


def test_ladder_exhaustion_quarantines(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    bad = EngineResult("not json", "z-ai/glm-5.2:free", 1, 1, 0.0)
    summary = run(_settings(), pg, store, engine=FakeEngine([bad] * 3),
                  max_docs=10, max_usd=5.0)
    assert summary.quarantined == 1
    row = _state_row(pg)
    assert row and row["status"] == "quarantined"
    attempts = [from_bytes(store.get(k)) for k in sorted(store.list(keys.X_ATTEMPTS_PREFIX))]
    assert attempts[-1].ladder_exhausted is True


def test_out_of_glob_breaker_aborts(pg: Conn, store: ArchiveStore) -> None:
    for i in range(1, 7):
        _seed_doc(pg, dh=sha256_hex(f"{DOC_MD}{i}".encode()), markdown=f"{DOC_MD}{i}",
                  uid=f"gh:x:{i}")
    rogue = EngineResult(json.dumps(EMIT), "claude-haiku-4-5", 1, 1, 0.0)
    summary = run(_settings(), pg, store, engine=FakeEngine([rogue] * 6),
                  max_docs=10, max_usd=5.0)
    assert summary.breaker_abort is True
    assert summary.quarantined == 0 and summary.pending == 4  # fold agreement: no rows -> pending
    assert pg.execute("SELECT count(*) AS n FROM extractions").fetchone()["n"] == 0  # type: ignore[index]
    outcomes = pg.execute(
        "SELECT count(*) AS n FROM extraction_attempts WHERE outcome='model_rejected'"
    ).fetchone()
    assert outcomes and outcomes["n"] == 5  # aborted at the limit, not after it


def test_throttled_stops_batch(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    _seed_doc(pg, dh=sha256_hex(f"{DOC_MD}2".encode()), markdown=f"{DOC_MD}2", uid="gh:x:2")
    engine = FakeEngine([GOOD, EngineThrottled("429")])
    summary = run(_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1 and summary.throttled is True
    assert pg.execute("SELECT count(*) AS n FROM extractions").fetchone()["n"] == 1  # type: ignore[index]


def test_transport_leaves_pending_then_recovers(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    boom = EngineTransportError("boom")
    summary = run(_settings(), pg, store, engine=FakeEngine([boom, boom, boom]),
                  max_docs=10, max_usd=5.0)
    assert summary.pending == 1 and _state_row(pg) is None
    summary2 = run(_settings(), pg, store, engine=FakeEngine([GOOD]),
                   max_docs=10, max_usd=5.0)
    assert summary2.validated == 1


def test_catch_up_replays_orphan_attempt(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    record = {"facts": {"boilerplate_spans": []}, "demand_profile": {"areas": [],
              "interview_evaluated": []}}
    orphan = _attempt(
        attempt_key=keys.x_attempt_key(datetime(2026, 8, 27, 7, 0, 0, tzinfo=UTC), DH, 1, 1),
        document_hash=DH,
        record=record,
        started_at="2026-08-27T07:00:00Z",
    )
    store.put(orphan.attempt_key, to_bytes(orphan))
    summary = run(_settings(), pg, store, engine=FakeEngine([]), max_docs=10, max_usd=5.0)
    assert summary.replayed == 1
    row = _state_row(pg)
    assert row and row["status"] == "validated"
    assert summary.queued == []  # replay satisfied the document before the queue ran


def test_usd_cap_stops_run(pg: Conn, store: ArchiveStore) -> None:
    for i in range(1, 4):
        _seed_doc(pg, dh=sha256_hex(f"{DOC_MD}{i}".encode()), markdown=f"{DOC_MD}{i}",
                  uid=f"gh:x:{i}")
    costly = EngineResult(json.dumps(EMIT), "z-ai/glm-5.2:free", 1, 1, 3.0)
    summary = run(_settings(), pg, store, engine=FakeEngine([costly] * 3),
                  max_docs=10, max_usd=5.0)
    assert summary.docs_attempted == 2  # 3.0 + 3.0 crosses the cap
    assert summary.spend_usd == 6.0


def test_dry_run_writes_nothing(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    summary = run(_settings(), pg, store, engine=FakeEngine([]), max_docs=10,
                  max_usd=5.0, dry_run=True)
    assert summary.queued and summary.docs_attempted == 0
    assert _state_row(pg) is None
    assert list(store.list(keys.X_ATTEMPTS_PREFIX)) == []
    assert list(store.list("extractions/")) == []  # no prompt/schema write-once objects


def test_prompt_bump_requeues_without_contamination(
    pg: Conn, store: ArchiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_doc(pg)
    assert run(_settings(), pg, store, engine=FakeEngine([GOOD]),
               max_docs=10, max_usd=5.0).validated == 1

    from jobhunter.l2 import runner as runner_mod

    monkeypatch.setattr(runner_mod, "PROMPT_VERSION", "demand-profile/vNEXT")
    bad = EngineResult("not json", "z-ai/glm-5.2:free", 1, 1, 0.0)
    summary = run(_settings(), pg, store, engine=FakeEngine([bad] * 3),
                  max_docs=10, max_usd=5.0)
    assert summary.queued == [DH]  # the bump re-selects the validated doc
    assert summary.quarantined == 1
    rows = pg.execute(
        "SELECT prompt_version, status, chosen_attempt FROM extractions ORDER BY prompt_version"
    ).fetchall()
    assert [(r["prompt_version"], r["status"]) for r in rows] == sorted(
        [(PROMPT_VERSION, "validated"), ("demand-profile/vNEXT", "quarantined")]
    )
    assert rows[0]["chosen_attempt"] is not None
    assert rows[1]["chosen_attempt"] is None  # v1's ok attempt must not leak into v2


def test_observed_model_differing_from_candidate_still_satisfies(
    pg: Conn, store: ArchiveStore
) -> None:
    _seed_doc(pg)
    canonical = EngineResult(json.dumps(EMIT), "z-ai/glm-5.2", 1, 1, 0.0)  # ':free' dropped
    settings = _settings(**{"JOB_HUNTER_L2_MODELS": "z-ai/*"})
    assert run(settings, pg, store, engine=FakeEngine([canonical]),
               max_docs=10, max_usd=5.0).validated == 1
    second = run(settings, pg, store, engine=FakeEngine([]), max_docs=10, max_usd=5.0)
    assert second.queued == []  # satisfied under l2_models, not the candidate spelling


def test_retry_review_then_next_run_revalidates(pg: Conn, store: ArchiveStore) -> None:
    from jobhunter.archive.keys import x_review_key
    from jobhunter.l2.runner import settle
    from jobhunter.store import extraction as xstore
    from jobhunter.timeutil import utcnow_precise

    _seed_doc(pg)
    bad = EngineResult("not json", "z-ai/glm-5.2:free", 1, 1, 0.0)
    assert run(_settings(), pg, store, engine=FakeEngine([bad] * 3),
               max_docs=10, max_usd=5.0).quarantined == 1
    at = utcnow_precise()
    event = {
        "review_key": x_review_key(at, DH, "retry", 1), "document_hash": DH,
        "model": "z-ai/glm-5.2:free", "prompt_version": PROMPT_VERSION,
        "schema_version": "1", "validator_version": VALIDATOR_VERSION, "verb": "retry",
        "payload": None, "actor": "human", "at": at.isoformat(),
    }
    store.put(event["review_key"], json.dumps(event).encode())
    xstore.record_review(pg, **event)
    state = settle(pg, store, DH, _settings().l2_models, at.isoformat())
    assert state.status is None  # pending again
    pg.commit()
    summary = run(_settings(), pg, store, engine=FakeEngine([GOOD]), max_docs=10, max_usd=5.0)
    assert summary.validated == 1  # the retry does not erase the later success


def test_doc_rerun_profile_stays_with_chosen_attempt(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    assert run(_settings(), pg, store, engine=FakeEngine([GOOD]),
               max_docs=10, max_usd=5.0).validated == 1
    emit_one = copy.deepcopy(EMIT)
    area = emit_one["demand_profile"]["areas"][0]
    area["claims"] = area["claims"][:1]
    del area["structure"]
    second_record = EngineResult(json.dumps(emit_one), "z-ai/glm-5.2:free", 1, 1, 0.0)
    run(_settings(), pg, store, engine=FakeEngine([second_record]),
        max_docs=10, max_usd=5.0, only_doc=DH)
    row = pg.execute("SELECT profile, chosen_attempt FROM extractions").fetchone()
    assert row is not None
    # first ok attempt stays chosen; the stored profile must be ITS record (2 claims)
    assert len(row["profile"]["demand_profile"]["areas"][0]["claims"]) == 2
    chosen = from_bytes(store.get(row["chosen_attempt"]))
    assert chosen.record is not None
    assert row["profile"]["demand_profile"] == chosen.record["demand_profile"]


def test_catch_up_replays_same_second_orphan(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    at = datetime(2026, 8, 27, 7, 0, 0, tzinfo=UTC)
    recorded = _attempt(
        attempt_key=keys.x_attempt_key(at, DH, 1, 1), document_hash=DH,
        outcome="transport", raw_response=None, observed_model=None,
        started_at="2026-08-27T07:00:00Z", validator_version=VALIDATOR_VERSION,
    )
    store.put(recorded.attempt_key, to_bytes(recorded))
    from jobhunter.store import extraction as xstore

    xstore.record_attempt(pg, recorded, None)  # watermark now at 07:00:00
    pg.commit()
    record = {"facts": {"boilerplate_spans": []},
              "demand_profile": {"areas": [], "interview_evaluated": []}}
    orphan = _attempt(
        attempt_key=keys.x_attempt_key(at, DH, 1, 2), document_hash=DH,
        record=record, started_at="2026-08-27T07:00:00Z", attempt_no=2,
    )
    store.put(orphan.attempt_key, to_bytes(orphan))  # archived, never recorded (crash)
    summary = run(_settings(), pg, store, engine=FakeEngine([]), max_docs=10, max_usd=5.0)
    assert summary.replayed >= 1  # the same-second orphan is not skipped
    row = _state_row(pg)
    assert row and row["status"] == "validated"


def test_zero_usd_cap_is_free_only_mode(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    _seed_doc(pg, dh=sha256_hex(f"{DOC_MD}2".encode()), markdown=f"{DOC_MD}2", uid="gh:x:2")
    summary = run(_settings(), pg, store, engine=FakeEngine([GOOD, GOOD]),
                  max_docs=10, max_usd=0.0)
    assert summary.validated == 2  # zero-cost calls proceed under --max-usd 0
    _seed_doc(pg, dh=sha256_hex(f"{DOC_MD}3".encode()), markdown=f"{DOC_MD}3", uid="gh:x:3")
    _seed_doc(pg, dh=sha256_hex(f"{DOC_MD}4".encode()), markdown=f"{DOC_MD}4", uid="gh:x:4")
    paid = EngineResult(json.dumps(EMIT), "z-ai/glm-5.2:free", 1, 1, 0.5)
    summary2 = run(_settings(), pg, store, engine=FakeEngine([paid, paid]),
                   max_docs=10, max_usd=0.0)
    assert summary2.docs_attempted == 1  # first paid call exceeds the zero cap


def test_retry_clears_row_keyed_by_observed_model(pg: Conn, store: ArchiveStore) -> None:
    from jobhunter.archive.keys import x_review_key
    from jobhunter.l2.runner import settle
    from jobhunter.store import extraction as xstore
    from jobhunter.timeutil import utcnow_precise

    _seed_doc(pg)
    canonical = EngineResult(json.dumps(EMIT), "z-ai/glm-5.2", 1, 1, 0.0)  # != requested alias
    assert run(_settings(**{"JOB_HUNTER_L2_MODELS": "z-ai/*"}), pg, store,
               engine=FakeEngine([canonical]), max_docs=10, max_usd=5.0).validated == 1
    at = utcnow_precise()
    event = {
        "review_key": x_review_key(at, DH, "flag", 1), "document_hash": DH,
        "model": "z-ai/glm-5.2", "prompt_version": PROMPT_VERSION,
        "schema_version": "1", "validator_version": VALIDATOR_VERSION, "verb": "flag",
        "payload": None, "actor": "human", "at": at.isoformat(),
    }
    store.put(event["review_key"], json.dumps(event).encode())
    xstore.record_review(pg, **event)
    at2 = utcnow_precise()
    retry = dict(event, review_key=x_review_key(at2, DH, "retry", 2), verb="retry",
                 at=at2.isoformat())
    store.put(retry["review_key"], json.dumps(retry).encode())
    xstore.record_review(pg, **retry)
    settle(pg, store, DH, ("z-ai/*",), at2.isoformat())
    rows = pg.execute("SELECT count(*) AS n FROM extractions").fetchone()
    assert rows and rows["n"] == 0  # the observed-model row is gone, not orphaned


def test_catch_up_replays_orphaned_review_event(pg: Conn, store: ArchiveStore) -> None:
    from jobhunter.archive.keys import x_review_key
    from jobhunter.timeutil import utcnow_precise

    _seed_doc(pg)
    assert run(_settings(), pg, store, engine=FakeEngine([GOOD]),
               max_docs=10, max_usd=5.0).validated == 1
    at = utcnow_precise()
    event = {
        "review_key": x_review_key(at, DH, "reject", 1), "document_hash": DH,
        "model": "z-ai/glm-5.2:free", "prompt_version": PROMPT_VERSION,
        "schema_version": "1", "validator_version": VALIDATOR_VERSION, "verb": "reject",
        "payload": {"note": "wrong"}, "actor": "human", "at": at.isoformat(),
    }
    store.put(event["review_key"], json.dumps(event).encode())  # archived, DB row lost (crash)
    summary = run(_settings(), pg, store, engine=FakeEngine([]), max_docs=10, max_usd=5.0)
    assert summary.replayed >= 1
    row = _state_row(pg)
    assert row and row["status"] == "rejected"  # the archived decision was applied


def test_one_row_per_config_across_model_spellings(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    bad = EngineResult("not json", "z-ai/glm-5.2", 1, 1, 0.0)
    settings = _settings(**{"JOB_HUNTER_L2_MODELS": "z-ai/*"})
    assert run(settings, pg, store, engine=FakeEngine([bad] * 3),
               max_docs=10, max_usd=5.0).quarantined == 1
    # human retries, next run validates under the observed spelling
    from jobhunter.archive.keys import x_review_key
    from jobhunter.store import extraction as xstore
    from jobhunter.timeutil import utcnow_precise

    at = utcnow_precise()
    event = {
        "review_key": x_review_key(at, DH, "retry", 1), "document_hash": DH,
        "model": "z-ai/glm-5.2:free", "prompt_version": PROMPT_VERSION,
        "schema_version": "1", "validator_version": VALIDATOR_VERSION, "verb": "retry",
        "payload": None, "actor": "human", "at": at.isoformat(),
    }
    store.put(event["review_key"], json.dumps(event).encode())
    xstore.record_review(pg, **event)
    from jobhunter.l2.runner import settle as l2_settle

    l2_settle(pg, store, DH, settings.l2_models, at.isoformat())  # as the CLI verb does
    pg.commit()
    canonical = EngineResult(json.dumps(EMIT), "z-ai/glm-5.2", 1, 1, 0.0)
    assert run(settings, pg, store, engine=FakeEngine([canonical]),
               max_docs=10, max_usd=5.0).validated == 1
    rows = pg.execute("SELECT model, status FROM extractions").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "validated"


def test_reconnects_when_the_connection_dies_mid_run(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    flaky = FlakyConn(pg, die_on="INSERT INTO extraction_attempts")
    healthy = FlakyConn(pg, die_on="\x00")  # same backend, never dies, ours to close
    summary = run(_settings(), flaky, store, engine=FakeEngine([GOOD]),  # type: ignore[arg-type]
                  max_docs=10, max_usd=5.0, connect=lambda: healthy)  # type: ignore[arg-type,return-value]
    assert flaky.dead and flaky.closed  # the corpse was replaced, not reused
    assert healthy.closed  # and the replacement is not leaked
    assert summary.validated == 1
    row = pg.execute("SELECT count(*) AS n FROM extraction_attempts").fetchone()
    assert row and row["n"] == 1  # the failed write was replayed exactly once


def test_backend_killed_during_the_engine_call_is_replaced(
    pg: Conn, store: ArchiveStore
) -> None:
    """The production shape, without simulation: a real backend is terminated
    while the engine call is in flight, as Neon's idle suspend terminates it."""
    schema_row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert schema_row is not None
    schema = str(schema_row["s"])
    _seed_doc(pg)
    victim = db.connect(TEST_DSN, schema=schema)
    pid_row = victim.execute("SELECT pg_backend_pid() AS p").fetchone()
    assert pid_row is not None

    class KillingEngine:
        name = "fake"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
            self.calls.append(model)
            pg.execute("SELECT pg_terminate_backend(%s)", (pid_row["p"],))
            pg.commit()
            return GOOD

    summary = run(_settings(), victim, store, engine=KillingEngine(), max_docs=10,
                  max_usd=5.0, connect=lambda: db.connect(TEST_DSN, schema=schema))
    assert summary.validated == 1
    row = pg.execute("SELECT count(*) AS n FROM extraction_attempts").fetchone()
    assert row and row["n"] == 1  # committed by the replacement connection
    victim.close()


def test_reconnect_that_lost_the_lock_aborts_without_writing(
    pg: Conn, store: ArchiveStore
) -> None:
    _seed_doc(pg)
    schema_row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert schema_row is not None
    schema = str(schema_row["s"])

    def _connect() -> Conn:
        # a genuinely new backend: the extract lock is still held elsewhere (by
        # the old session here, by another writer in production)
        return db.connect(TEST_DSN, schema=schema)

    flaky = FlakyConn(pg, die_on="INSERT INTO extraction_attempts")
    summary = run(_settings(), flaky, store, engine=FakeEngine([GOOD]),  # type: ignore[arg-type]
                  max_docs=10, max_usd=5.0, connect=_connect)
    assert summary.lock_held is True
    assert summary.validated == 0
    checker = db.connect(TEST_DSN, schema=schema)
    try:
        row = checker.execute("SELECT count(*) AS n FROM extraction_attempts").fetchone()
        assert row and row["n"] == 0  # nothing committed alongside the other writer
    finally:
        checker.close()


def test_a_lost_lock_reports_the_spend_it_already_made(pg: Conn, store: ArchiveStore) -> None:
    """`lock_held` covers two different runs and must not describe them alike.

    Nothing-happened (the lock was busy at the start) is genuinely "nothing
    done"; losing the lock mid-run is not — the engine was paid for work whose
    rows rolled back, and the summary is the only place that survives.
    """
    _seed_doc(pg)
    schema_row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert schema_row is not None
    schema = str(schema_row["s"])
    costly = EngineResult(json.dumps(EMIT), "z-ai/glm-5.2:free", 1, 1, 0.25)
    flaky = FlakyConn(pg, die_on="INSERT INTO extraction_attempts")
    summary = run(_settings(), flaky, store, engine=FakeEngine([costly]),  # type: ignore[arg-type]
                  max_docs=10, max_usd=5.0,
                  connect=lambda: db.connect(TEST_DSN, schema=schema))
    assert summary.lock_held is True
    assert summary.aborted == "lock_lost"  # not the same thing as a busy lock
    assert summary.docs_attempted == 1 and summary.spend_usd == 0.25  # the money left anyway
    assert summary.validated == 0  # counted only after the per-document commit
    assert summary.to_dict()["aborted"] == "lock_lost"

    # `pg` still holds the extract lock (the dead session could not unlock), so a
    # second run walks into the busy-lock branch — which really did do nothing
    latecomer = db.connect(TEST_DSN, schema=schema)
    try:
        busy = run(_settings(), latecomer, store, engine=FakeEngine([]),
                   max_docs=10, max_usd=5.0)
        assert busy.lock_held is True and busy.aborted is None
    finally:
        latecomer.close()


def test_payment_required_is_an_engine_failure_that_trips_fast(
    pg: Conn, store: ArchiveStore
) -> None:
    _seed_doc(pg)
    engine = FakeEngine([EngineAuthError(402, "Insufficient credits")])
    started = time.monotonic()
    with pytest.raises(EngineFatalError) as caught:
        run(_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert time.monotonic() - started < 5.0  # payment failures do not back off
    assert len(engine.calls) == 1  # and are never retried or laddered
    assert "402" in str(caught.value) and "Insufficient credits" in str(caught.value)
    row = pg.execute("SELECT outcome, error_detail FROM extraction_attempts").fetchone()
    assert row and row["outcome"] == "engine_fatal"
    assert "402" in row["error_detail"]["errors"][0]


def test_attempts_before_a_mid_document_kill_stay_in_the_ledger(
    pg: Conn, store: ArchiveStore
) -> None:
    """The run's own rolled-back attempts must reach the ledger.

    When the backend dies between two attempts of ONE document, the earlier
    attempts roll back while the later ones commit on the replacement — the
    watermark (max started_at) jumps past the orphans and no catch-up scan ever
    lists them again. The run must therefore re-apply what it wrote itself.
    """
    schema_row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert schema_row is not None
    schema = str(schema_row["s"])
    _seed_doc(pg)
    victim = db.connect(TEST_DSN, schema=schema)
    pid_row = victim.execute("SELECT pg_backend_pid() AS p").fetchone()
    assert pid_row is not None
    bad = EngineResult("not json", "z-ai/glm-5.2:free", 1, 1, 0.0)

    class KillBetweenAttempts:
        name = "fake"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
            self.calls.append(model)
            if len(self.calls) == 1:
                return bad  # attempt 1: archived and recorded, not yet committed
            if len(self.calls) == 2:
                _terminate(pg, int(pid_row["p"]))  # attempt 1's row dies with the backend
            return GOOD  # the reprompt escalates to k=3: calls 3 and 4 are samples

    summary = run(_settings(), victim, store, engine=KillBetweenAttempts(), max_docs=10,
                  max_usd=5.0, connect=lambda: db.connect(TEST_DSN, schema=schema))
    assert summary.validated == 1
    rows = pg.execute(
        "SELECT attempt_key FROM extraction_attempts ORDER BY attempt_key"
    ).fetchall()
    assert [r["attempt_key"] for r in rows] == sorted(store.list(keys.X_ATTEMPTS_PREFIX))
    assert len(rows) == 4  # both ladder attempts AND both samples, not just survivors
    # and nothing is left for a later catch-up to find, so no `extract rebuild`
    later = run(_settings(), pg, store, engine=FakeEngine([]), max_docs=10, max_usd=5.0)
    assert later.replayed == 0
    victim.close()


def test_a_kill_at_the_per_document_commit_keeps_the_derived_row(
    pg: Conn, store: ArchiveStore
) -> None:
    """The same rollback can land on the per-document commit, after `settle` has
    written the derived row: re-applying the attempt is not enough, the fold has
    to run again or the summary claims a validation the store does not hold."""
    schema_row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert schema_row is not None
    schema = str(schema_row["s"])
    _seed_doc(pg)
    victim = db.connect(TEST_DSN, schema=schema)
    conn = KillingConn(victim, pg, "INSERT INTO extractions", at_next_commit=True)
    summary = run(_settings(), conn, store, engine=FakeEngine([GOOD]),  # type: ignore[arg-type]
                  max_docs=10, max_usd=5.0,
                  connect=lambda: db.connect(TEST_DSN, schema=schema))
    assert conn.fired and summary.validated == 1
    row = _state_row(pg)
    assert row and row["status"] == "validated"
    attempts = pg.execute("SELECT count(*) AS n FROM extraction_attempts").fetchone()
    assert attempts and attempts["n"] == 1
    victim.close()


def test_engine_failure_survives_a_dead_connection_teardown(
    pg: Conn, store: ArchiveStore
) -> None:
    """The CI symptom (run 33632605810): a 402 abort whose cleanup ran against a
    connection Neon had already dropped was reported as a database error."""
    _seed_doc(pg)
    flaky = FlakyConn(pg, die_on="pg_advisory_unlock")
    with pytest.raises(EngineFatalError) as caught:
        run(_settings(), flaky, store,  # type: ignore[arg-type]
            engine=FakeEngine([EngineAuthError(402, "Insufficient credits")]),
            max_docs=10, max_usd=5.0, connect=lambda: pg)
    assert "402" in str(caught.value)


def test_engine_failure_survives_losing_the_lock_while_recording_it(
    pg: Conn, store: ArchiveStore
) -> None:
    """The engine failure is the news, even when its own bookkeeping loses the lock.

    Recording the `engine_fatal` attempt can hit the dead connection; if the
    replacement cannot re-take the extract lock, the LockLost must not be
    reported as `lock_held` — that exits 0 saying "nothing done" and throws the
    402 away.
    """
    _seed_doc(pg)
    schema_row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert schema_row is not None
    schema = str(schema_row["s"])
    flaky = FlakyConn(pg, die_on="INSERT INTO extraction_attempts")
    with pytest.raises(EngineFatalError) as caught:
        run(_settings(), flaky, store,  # type: ignore[arg-type]
            engine=FakeEngine([EngineAuthError(402, "Insufficient credits")]),
            max_docs=10, max_usd=5.0,
            # a genuinely new backend: the extract lock is still held elsewhere
            connect=lambda: db.connect(TEST_DSN, schema=schema))
    assert "402" in str(caught.value) and "Insufficient credits" in str(caught.value)
    assert isinstance(caught.value.__cause__, LockLost)  # the lock loss is not hidden either


def test_no_transaction_spans_the_engine_call_under_idle_timeout(
    pg: Conn, store: ArchiveStore
) -> None:
    """The production mechanism (canary run 33666006472): a transaction held open
    across the minute-plus model call trips Neon's idle_in_transaction_session_timeout.

    That raises IdleInTransactionSessionTimeout (SQLSTATE 25P03) — an InternalError,
    NOT the OperationalError the reconnect used to catch — so before the fix the run
    dies with it. With the runner holding no transaction while the engine runs, the
    timeout never fires; the connection is merely transaction-idle across the call.
    """
    schema_row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert schema_row is not None
    schema = str(schema_row["s"])
    _seed_doc(pg)

    def _connect() -> Conn:
        # the GUC is per-session, so every connection the runner uses (the first
        # one and any reconnect) arms the same short idle-in-transaction timeout
        c = db.connect(TEST_DSN, schema=schema)
        c.execute("SET idle_in_transaction_session_timeout = '400ms'")
        c.commit()
        return c

    victim = _connect()

    class SlowEngine:
        name = "fake"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
            self.calls.append(model)
            time.sleep(0.7)  # longer than the 400ms idle-in-transaction timeout
            return GOOD

    summary = run(_settings(), victim, store, engine=SlowEngine(), max_docs=10,
                  max_usd=5.0, connect=_connect)
    assert summary.validated == 1  # no transaction spanned the sleeping engine call
    row = pg.execute("SELECT count(*) AS n FROM extraction_attempts").fetchone()
    assert row and row["n"] == 1  # the attempt was recorded on the live connection
    victim.close()


def test_session_reconnects_on_idle_timeout_only_when_the_connection_is_dead(
    pg: Conn, store: ArchiveStore
) -> None:
    """`_Session.do` must recover from a 25P03 that killed the connection — the
    reconnect defense #7 built for OperationalError has to reach this sibling class
    too — but a 25P03 raised on a LIVE connection is a genuine bug and must propagate,
    not loop forever re-taking the lock and retrying the failing op."""
    from jobhunter.l2.runner import _Session

    # DEAD: the op raises the idle-in-transaction kill and the socket is gone.
    class DeadConn:
        closed = True

        def close(self) -> None:
            pass

    dead_calls = {"n": 0}

    def _op_on_dead(conn: Any) -> str:
        dead_calls["n"] += 1
        if dead_calls["n"] == 1:
            raise psycopg.errors.IdleInTransactionSessionTimeout(
                "terminating connection due to idle-in-transaction timeout"
            )
        return "recovered"

    session = _Session(DeadConn(), connect=lambda: pg)  # type: ignore[arg-type,return-value]
    session.holds_lock = True
    assert session.do(_op_on_dead) == "recovered"  # reconnected, replayed the op once
    assert dead_calls["n"] == 2
    db.unlock(pg, db.EXTRACT_LOCK_KEY)  # release the lock _revive re-took on pg

    # LIVE: a 25P03 on a connection that is still open is a real query bug.
    class LiveConn:
        closed = False

    live_calls = {"n": 0}

    def _op_on_live(conn: Any) -> str:
        live_calls["n"] += 1
        raise psycopg.errors.IdleInTransactionSessionTimeout("but the socket is fine")

    session2 = _Session(LiveConn(), connect=lambda: pg)  # type: ignore[arg-type,return-value]
    session2.holds_lock = True
    with pytest.raises(psycopg.errors.IdleInTransactionSessionTimeout):
        session2.do(_op_on_live)
    assert live_calls["n"] == 1  # propagated, never retried


# --- k-sampling and the agreement gate (M3, spec §4.5) ----------------------
# Audit slot: int(document_hash[:8], 16) % JOB_HUNTER_L2_AUDIT_MOD == 0. The
# test document's value is 3743166876: mod 1 always audits, mod 5 misses.


def _divergent() -> EngineResult:
    emit = copy.deepcopy(EMIT)
    del emit["demand_profile"]["areas"][0]["claims"][1]  # drop c2 -> pairwise F1 2/3
    emit["demand_profile"]["areas"][0]["structure"] = None
    return EngineResult(
        raw_text=json.dumps(emit), observed_model="z-ai/glm-5.2:free",
        input_tokens=40, output_tokens=9, cost_usd=0.0,
    )


def test_sampling_audit_slot_runs_k3(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    engine = FakeEngine([GOOD, GOOD, GOOD])
    summary = run(_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1
    archived = [from_bytes(store.get(k)) for k in store.list(keys.X_ATTEMPTS_PREFIX)]
    assert sorted(a.sample_slot for a in archived) == [1, 2, 3]
    row = _state_row(pg)
    assert row and row["status"] == "validated" and row["k"] == 3
    assert row["agreement"] and row["agreement"]["mean_f1"] == 1.0
    assert row["chosen_attempt"] in {a.attempt_key for a in archived}


def test_sampling_non_audit_doc_runs_k1(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    engine = FakeEngine([GOOD])
    summary = run(_settings(JOB_HUNTER_L2_AUDIT_MOD="5"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1
    assert len(list(store.list(keys.X_ATTEMPTS_PREFIX))) == 1
    row = _state_row(pg)
    assert row and row["k"] == 1 and row["agreement"] is None


def test_sampling_reprompt_escalates_to_k3(pg: Conn, store: ArchiveStore) -> None:
    # slot-1 needs a reprompt (schema_invalid then ok): the cheapest predictor
    # of a hard document escalates to k=3 even off the audit slot.
    _seed_doc(pg)
    bad = EngineResult(raw_text="not json", observed_model="z-ai/glm-5.2:free",
                       input_tokens=4, output_tokens=1, cost_usd=0.0)
    engine = FakeEngine([bad, GOOD, GOOD, GOOD])
    summary = run(_settings(JOB_HUNTER_L2_AUDIT_MOD="5"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1
    archived = [from_bytes(store.get(k)) for k in store.list(keys.X_ATTEMPTS_PREFIX)]
    assert sorted(a.sample_slot for a in archived) == [1, 1, 2, 3]
    row = _state_row(pg)
    assert row and row["status"] == "validated" and row["k"] == 3


def test_sampling_agreement_failure_demotes_to_needs_review(
    pg: Conn, store: ArchiveStore
) -> None:
    _seed_doc(pg)
    engine = FakeEngine([GOOD, _divergent(), _divergent()])
    summary = run(_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 0
    row = _state_row(pg)
    assert row and row["status"] == "needs_review" and row["k"] == 3
    assert "f1" in row["agreement"]["failures"]
    # the medoid is the majority shape: one of the divergent samples
    archived = {from_bytes(store.get(k)).attempt_key: from_bytes(store.get(k))
                for k in store.list(keys.X_ATTEMPTS_PREFIX)}
    chosen = archived[row["chosen_attempt"]]
    assert chosen.sample_slot in (2, 3)


def test_sampling_failed_sample_demotes_to_needs_review(
    pg: Conn, store: ArchiveStore
) -> None:
    # A sample that cannot produce a valid record is itself a disagreement
    # about a document the audit chose: escalate, never validate silently.
    _seed_doc(pg)
    bad = EngineResult(raw_text="not json", observed_model="z-ai/glm-5.2:free",
                       input_tokens=4, output_tokens=1, cost_usd=0.0)
    engine = FakeEngine([GOOD, bad, GOOD])
    summary = run(_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    row = _state_row(pg)
    assert row and row["status"] == "needs_review" and row["k"] == 3
    assert "sample_failed" in row["agreement"]["failures"]
    assert summary.validated == 0


def test_sampling_spend_counts_every_sample(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    costly = EngineResult(raw_text=json.dumps(EMIT), observed_model="z-ai/glm-5.2:free",
                          input_tokens=40, output_tokens=9, cost_usd=0.5)
    engine = FakeEngine([costly, costly, costly])
    summary = run(_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert abs(summary.spend_usd - 1.5) < 1e-9


# --- parallel engine calls (T-20260906-SVY4) --------------------------------
# The extract lock allows one runner; each doc spends ~30s waiting on the
# engine. Workers overlap ONLY the engine calls: a single gate serializes all
# DB, journal, summary and breaker access, released strictly around
# engine.complete, so the semantics stay those of the serial drain.


class BarrierEngine:
    """Blocks each complete() until `expected` calls are in flight at once —
    proof the engine phase truly overlaps — then answers per document."""

    name = "fake"

    def __init__(self, expected: int) -> None:
        import threading

        self.barrier = threading.Barrier(expected, timeout=30)
        self.calls: list[str] = []

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        self.calls.append(model)
        self.barrier.wait()  # raises BrokenBarrierError if overlap never happens
        return GOOD


def _seed_three(pg: Conn) -> list[str]:
    hashes = []
    for i in range(3):
        md = DOC_MD + f"\n\n<!-- doc {i} -->"
        dh = sha256_hex(md.encode("utf-8"))
        _seed_doc(pg, dh=dh, markdown=md, uid=f"gh:x:{i + 10}")
        hashes.append(dh)
    return hashes


def test_parallel_workers_overlap_engine_calls(pg: Conn, store: ArchiveStore) -> None:
    _seed_three(pg)
    engine = BarrierEngine(expected=3)
    summary = run(
        _settings(JOB_HUNTER_L2_CONCURRENCY="3", JOB_HUNTER_L2_AUDIT_MOD="999983"),
        pg, store, engine=engine, max_docs=10, max_usd=5.0,
    )
    assert summary.validated == 3 and summary.docs_attempted == 3
    rows = pg.execute("SELECT status, count(*) AS n FROM extractions GROUP BY status").fetchall()
    assert rows and rows[0]["status"] == "validated" and rows[0]["n"] == 3


def test_parallel_respects_the_docs_cap(pg: Conn, store: ArchiveStore) -> None:
    _seed_three(pg)
    engine = FakeEngine([GOOD, GOOD, GOOD])
    summary = run(
        _settings(JOB_HUNTER_L2_CONCURRENCY="3", JOB_HUNTER_L2_AUDIT_MOD="999983"),
        pg, store, engine=engine, max_docs=2, max_usd=5.0,
    )
    assert summary.docs_attempted == 2 and summary.validated == 2


def test_concurrency_default_is_serial() -> None:
    assert _settings().l2_concurrency == 1


# --- [A1] archive I/O never holds a transaction open ------------------------
# A managed Postgres kills a session that sits idle IN a transaction (SQLSTATE
# 25P03, canary run 33666006472). Every archive read is a network round trip,
# so a GET issued inside a transaction is that kill waiting to happen. The fix
# is structural: the connection is transaction-idle whenever the archive is
# read, in `settle` and in the catch-up scan alike.


class SlowStore:
    """The archive with a hook on every touch — where a real backend stalls.

    `watch(op, key)` runs BEFORE the call is served, so it observes exactly what
    the database connection was doing while the archive was slow.
    """

    def __init__(self, inner: ArchiveStore, watch: Callable[[str, str], None]) -> None:
        self._inner = inner
        self._watch = watch

    def put(self, key: str, data: bytes) -> bool:
        self._watch("put", key)
        return self._inner.put(key, data)

    def get(self, key: str) -> bytes:
        self._watch("get", key)
        return self._inner.get(key)

    def exists(self, key: str) -> bool:
        self._watch("exists", key)
        return self._inner.exists(key)

    def list(self, prefix: str, start_after: str | None = None) -> Iterator[str]:
        for key in self._inner.list(prefix, start_after=start_after):
            self._watch("list", key)  # a listing page is a round trip of its own
            yield key


ORPHAN_RECORD = {
    "facts": {"boilerplate_spans": []},
    "demand_profile": {"areas": [], "interview_evaluated": []},
}


@contextmanager
def _own_connection(pg: Conn) -> Iterator[Conn]:
    """A second session for the runner, so `pg` sees only what really committed."""
    schema_row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert schema_row is not None
    pg.commit()
    conn = db.connect(TEST_DSN, schema=str(schema_row["s"]))
    try:
        yield conn
    finally:
        conn.close()


def _breaking_store(store: ArchiveStore, dh: str) -> SlowStore:
    """The archive with one document's objects unreadable.

    Nothing on the catch-up path catches, so a GET that raises in a later chunk
    ends the scan exactly where a killed backend would — without needing one.
    """

    def boom(op: str, key: str) -> None:
        if op == "get" and dh[:12] in key:  # attempt keys carry 12 hex chars of it
            raise RuntimeError("the archive went away mid-scan")

    return SlowStore(store, boom)


def _orphan_bytes(at: datetime, dh: str, no: int, ok: bool) -> tuple[str, bytes]:
    """One archived attempt that never reached the database — the crash window
    between the archive write and its row. An `ok` one carries the record the
    fold settles on; anything else is a transport failure, which settles
    nothing on its own."""
    orphan = _attempt(
        attempt_key=keys.x_attempt_key(at, dh, 1, no), document_hash=dh, attempt_no=no,
        outcome="ok" if ok else "transport",
        observed_model="z-ai/glm-5.2:free" if ok else None,
        record=ORPHAN_RECORD if ok else None, started_at=iso(at),
    )
    return orphan.attempt_key, to_bytes(orphan)


def _seed_orphans(store: ArchiveStore, n: int) -> None:
    """n orphans one second apart, the last of them the one carrying a record."""
    for i in range(1, n + 1):
        store.put(*_orphan_bytes(datetime(2026, 8, 27, 7, 0, i, tzinfo=UTC), DH, i, i == n))


def test_settle_archive_reads_are_transaction_idle(pg: Conn, store: ArchiveStore) -> None:
    """`settle` loads the chosen attempt's record from the archive. That GET must
    not run inside the transaction the attempt row was written in — nor may any
    other archive touch the drain makes on the way there."""
    _seed_doc(pg)
    seen: list[tuple[str, str, TransactionStatus]] = []
    slow = SlowStore(store, lambda op, key: seen.append((op, key, pg.info.transaction_status)))
    summary = run(_settings(), pg, slow, engine=FakeEngine([GOOD]), max_docs=10, max_usd=5.0)
    assert summary.validated == 1
    assert [op for op, _, _ in seen].count("get") == 1  # the chosen attempt's record
    assert [s for _, _, s in seen] == [TransactionStatus.IDLE] * len(seen)
    row = _state_row(pg)
    assert row and row["status"] == "validated"  # and the fold still wrote its row


def test_catch_up_archive_reads_are_transaction_idle(
    pg: Conn, store: ArchiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catch-up scan reads the whole orphan window out of the archive — key
    listings and object GETs both — and settles what it replays. None of that
    traffic may hold a transaction open, and chunking it must not make a replay
    land twice."""
    from jobhunter.l2 import runner as runner_mod

    monkeypatch.setattr(runner_mod, "CATCH_UP_CHUNK", 2)  # five orphans -> three chunks
    _seed_doc(pg)
    _seed_orphans(store, 5)
    event = {
        "review_key": keys.x_review_key(datetime(2026, 8, 27, 8, 0, tzinfo=UTC), DH, "flag", 1),
        "document_hash": DH, "model": "z-ai/glm-5.2:free", "prompt_version": PROMPT_VERSION,
        "schema_version": "1", "validator_version": VALIDATOR_VERSION, "verb": "flag",
        "payload": None, "actor": "human", "at": "2026-08-27T08:00:00Z",
    }
    store.put(event["review_key"], json.dumps(event).encode())
    seen: list[tuple[str, str, TransactionStatus]] = []
    slow = SlowStore(store, lambda op, key: seen.append((op, key, pg.info.transaction_status)))
    summary = run(_settings(), pg, slow, engine=FakeEngine([]), max_docs=10, max_usd=5.0)
    assert summary.replayed == 6  # five attempts and the review event
    assert [s for _, _, s in seen] == [TransactionStatus.IDLE] * len(seen)
    gets = [k for op, k, _ in seen if op == "get" and k.startswith(keys.X_ATTEMPTS_PREFIX)]
    # five scanned orphans, plus the chosen attempt once per folding chunk: the
    # chunk that replayed the ok attempt, and the one that replayed the review
    assert len(gets) == 7
    row = _state_row(pg)
    assert row and row["status"] == "needs_review"  # the flagged fold, settled once
    again = run(_settings(), pg, slow, engine=FakeEngine([]), max_docs=10, max_usd=5.0)
    assert again.replayed == 0  # chunked commits never make a replay observable twice


def test_catch_up_commits_each_chunk_as_it_scans(
    pg: Conn, store: ArchiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunking is not only a memory bound: each chunk COMMITS, so the scan holds
    no transaction across the next chunk's archive reads. An observer connection
    watches the replayed rows appear while the scan is still running."""
    from jobhunter.l2 import runner as runner_mod

    monkeypatch.setattr(runner_mod, "CATCH_UP_CHUNK", 2)
    schema_row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert schema_row is not None
    _seed_doc(pg)
    _seed_orphans(store, 5)
    committed: list[int] = []

    def watch(op: str, key: str) -> None:
        if op != "get" or not key.startswith(keys.X_ATTEMPTS_PREFIX):
            return
        pg.commit()  # a fresh snapshot: what another session can see right now
        row = pg.execute("SELECT count(*) AS n FROM extraction_attempts").fetchone()
        pg.commit()
        committed.append(int(row["n"]) if row else -1)

    runner_conn = db.connect(TEST_DSN, schema=str(schema_row["s"]))
    try:
        summary = run(_settings(), runner_conn, SlowStore(store, watch),
                      engine=FakeEngine([]), max_docs=10, max_usd=5.0)
    finally:
        runner_conn.close()
    assert summary.replayed == 5
    # chunks of two: the third and fifth GETs see the earlier chunks already
    # committed, which a single scan-wide transaction could never show
    assert committed[:5] == [0, 0, 2, 2, 4]


def test_catch_up_settles_each_chunk_before_reading_the_next(
    pg: Conn, store: ArchiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan that dies in a later chunk leaves the earlier chunks FOLDED.

    Each chunk commits its attempt rows, and the watermark is max(started_at)
    over committed rows — so a chunk whose rows landed without their
    `extractions` row would sit behind every later run's watermark, unreachable
    by `record_attempt` (idempotent: no insert, no settle) and healed only by
    `extract rebuild`. The scan therefore settles what it recorded before it
    touches the archive again. No database death is needed to show it: the
    loops have no `except`, so a single unreadable object in a later chunk ends
    the scan exactly as a killed backend would.
    """
    from jobhunter.l2 import runner as runner_mod

    monkeypatch.setattr(runner_mod, "CATCH_UP_CHUNK", 1)  # one orphan per chunk
    dh_b = sha256_hex(f"{DOC_MD}B".encode())
    _seed_doc(pg)
    _seed_doc(pg, dh=dh_b, markdown=f"{DOC_MD}B", uid="gh:x:2")
    for i, dh in ((1, DH), (2, dh_b)):
        at = datetime(2026, 8, 27, 7, 0, i, tzinfo=UTC)
        orphan = _attempt(
            attempt_key=keys.x_attempt_key(at, dh, 1, 1), document_hash=dh,
            record=ORPHAN_RECORD, started_at=f"2026-08-27T07:00:0{i}Z",
        )
        store.put(orphan.attempt_key, to_bytes(orphan))

    with _own_connection(pg) as runner_conn, pytest.raises(RuntimeError, match="mid-scan"):
        run(_settings(), runner_conn, _breaking_store(store, dh_b),
            engine=FakeEngine([]), max_docs=10, max_usd=5.0)
    pg.commit()  # a fresh snapshot: what the interrupted scan really left behind
    rows = pg.execute("SELECT document_hash, status FROM extractions").fetchall()
    assert {r["document_hash"]: r["status"] for r in rows} == {DH: "validated"}
    n = pg.execute("SELECT count(*) AS n FROM extraction_attempts").fetchone()
    assert n and n["n"] == 1  # only the folded chunk's row committed


def test_catch_up_refolds_a_document_its_chunk_reopens(
    pg: Conn, store: ArchiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chunk's fold is the whole document's, not just the new attempt's.

    A settled document that a replayed sample reopens (k=1 -> k=2, agreement
    computed) must be re-derived by the chunk that replays it — otherwise the
    served row keeps a verdict the events no longer support and, the watermark
    having moved past the sample, nothing re-reads it.
    """
    from jobhunter.l2 import runner as runner_mod
    from jobhunter.timeutil import utcnow_precise

    monkeypatch.setattr(runner_mod, "CATCH_UP_CHUNK", 1)
    _seed_doc(pg)
    assert run(_settings(), pg, store, engine=FakeEngine([GOOD]),
               max_docs=1, max_usd=5.0).validated == 1
    before = _state_row(pg)
    assert before and before["k"] == 1 and before["agreement"] is None
    first = from_bytes(store.get(before["chosen_attempt"]))
    assert first.record is not None
    dh_b = sha256_hex(f"{DOC_MD}B".encode())
    _seed_doc(pg, dh=dh_b, markdown=f"{DOC_MD}B", uid="gh:x:2")
    at = utcnow_precise() + timedelta(seconds=1)  # after the run's own watermark
    sample = _attempt(  # a second slot, archived, its row lost to the crash
        attempt_key=keys.x_attempt_key(at, DH, 2, 9), document_hash=DH, sample_slot=2,
        attempt_no=9, record=copy.deepcopy(first.record), started_at=iso(at),
    )
    store.put(sample.attempt_key, to_bytes(sample))
    later = at + timedelta(seconds=1)
    orphan_b = _attempt(
        attempt_key=keys.x_attempt_key(later, dh_b, 1, 1), document_hash=dh_b,
        record=ORPHAN_RECORD, started_at=iso(later),
    )
    store.put(orphan_b.attempt_key, to_bytes(orphan_b))
    with _own_connection(pg) as runner_conn, pytest.raises(RuntimeError, match="mid-scan"):
        run(_settings(), runner_conn, _breaking_store(store, dh_b),
            engine=FakeEngine([]), max_docs=10, max_usd=5.0)
    pg.commit()
    after = _state_row(pg)
    assert after and after["k"] == 2 and after["agreement"] is not None


def test_catch_up_heals_the_fold_its_own_death_interrupted(
    pg: Conn, store: ArchiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend killed DURING a chunk's fold is healed by the rescan.

    `settle` commits the chunk's attempt rows at its [A1] boundary and only then
    reads the archive, so a kill in that gap leaves rows committed with no
    derived row — and the watermark now sits ON those rows, so `record_attempt`
    answers "known" for them ever after. The rescan that the reconnect triggers
    therefore folds every tuple in its window, not only the ones it inserted:
    "the row was already there" says nothing about whether the fold ran.
    """
    from jobhunter.l2 import runner as runner_mod

    monkeypatch.setattr(runner_mod, "CATCH_UP_CHUNK", 2)
    dh_b = sha256_hex(f"{DOC_MD}B".encode())
    _seed_doc(pg)
    _seed_doc(pg, dh=dh_b, markdown=f"{DOC_MD}B", uid="gh:x:2")
    for dh, secs in ((DH, (1, 2)), (dh_b, (3, 4, 5, 6))):
        for i, sec in enumerate(secs, start=1):
            at = datetime(2026, 8, 27, 7, 0, sec, tzinfo=UTC)
            ok = i == len(secs)
            store.put(*_orphan_bytes(at, dh, i, ok))
    schema_row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert schema_row is not None
    schema = str(schema_row["s"])
    pg.commit()
    runner_conn = db.connect(TEST_DSN, schema=schema)
    pid_row = runner_conn.execute("SELECT pg_backend_pid() AS p").fetchone()
    assert pid_row is not None
    runner_conn.commit()
    gets = [0]

    def kill_on_third_get(op: str, key: str) -> None:
        if op != "get" or not key.startswith(keys.X_ATTEMPTS_PREFIX):
            return
        gets[0] += 1
        # 1,2 = chunk one's scan; 3 = its fold re-reading the chosen attempt,
        # i.e. after settle's boundary commit landed doc A's rows
        if gets[0] == 3:
            _terminate(pg, int(pid_row["p"]))

    try:
        # max_docs=0: this is the scan's story, so no engine work follows it
        summary = run(_settings(), runner_conn, SlowStore(store, kill_on_third_get),
                      engine=FakeEngine([]), max_docs=0, max_usd=0.0,
                      connect=lambda: db.connect(TEST_DSN, schema=schema))
    finally:
        runner_conn.close()
    assert summary.aborted is None
    pg.commit()
    rows = pg.execute("SELECT document_hash, status FROM extractions").fetchall()
    assert {r["document_hash"]: r["status"] for r in rows} == {
        DH: "validated", dh_b: "validated",
    }  # the interrupted document is folded too, by the rescan that followed


def test_over_budget_attempt_is_archived_transaction_idle(
    pg: Conn, store: ArchiveStore
) -> None:
    """The over-budget branch never calls an engine, so nothing on its path has
    ended the read transaction `markdown_for`/`next_attempt_no` opened. It ends
    that transaction itself: its archive PUT is a round trip like any other, and
    [A1] holds for writes as well as reads."""
    huge = "# Big\n\n" + "requirement " * 6_000
    dh = sha256_hex(huge.encode("utf-8"))
    _seed_doc(pg, dh=dh, markdown=huge, uid="gh:x:big")
    seen: list[tuple[str, str, TransactionStatus]] = []
    slow = SlowStore(store, lambda op, key: seen.append((op, key, pg.info.transaction_status)))
    summary = run(_settings(), pg, slow, engine=FakeEngine([]), max_docs=10, max_usd=5.0)
    assert summary.docs_attempted == 1
    puts = [(k, s) for op, k, s in seen if op == "put" and k.startswith(keys.X_ATTEMPTS_PREFIX)]
    assert len(puts) == 1  # the over_budget attempt object
    assert [s for _, s in puts] == [TransactionStatus.IDLE]
    assert [s for _, _, s in seen] == [TransactionStatus.IDLE] * len(seen)
