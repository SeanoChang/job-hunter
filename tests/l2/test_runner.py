"""Integration: the whole drain loop over Postgres + LocalFS archive with a
scripted fake engine. No network, no LLM."""

import copy
import json
import time
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

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
from jobhunter.l2.runner import run
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.store import db
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
    summary = run(_settings(), pg, store, engine=FakeEngine([bad, GOOD]),
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
