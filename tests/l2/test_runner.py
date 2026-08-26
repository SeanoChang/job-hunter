"""Integration: the whole drain loop over Postgres + LocalFS archive with a
scripted fake engine. No network, no LLM."""

import copy
import json
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from jobhunter.archive import keys, open_store
from jobhunter.archive.base import ArchiveStore
from jobhunter.config import Settings
from jobhunter.hashing import sha256_hex
from jobhunter.l2.attempts import from_bytes, to_bytes
from jobhunter.l2.engines import EngineResult, EngineThrottled, EngineTransportError
from jobhunter.l2.runner import run
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
    assert any("longest matching prefix" in e for e in first.prior_errors)


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
