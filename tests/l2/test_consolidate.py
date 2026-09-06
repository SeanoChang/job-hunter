"""Weekly consolidation (M3, spec §2): validated rows only in, append-only
archive artifacts out — a drift report, the human audit queue, and the refuter
summary — under the consolidation/ prefix, with a built-in freshness check so
the hourly workflow can call it every run and it fires weekly (no second
scheduler to die silently)."""

import json
from typing import Any

import psycopg
import pytest

from jobhunter.archive import keys, open_store
from jobhunter.archive.base import ArchiveStore
from jobhunter.l2.consolidate import consolidate
from jobhunter.l2.engines import EngineResult
from jobhunter.l2.refuter import refute_doc
from jobhunter.l2.runner import run
from tests.l2.test_runner import DH, GOOD, FakeEngine, _seed_doc, _settings, _state_row

Conn = psycopg.Connection[dict[str, Any]]


@pytest.fixture
def store(tmp_path: Any) -> ArchiveStore:
    return open_store(f"file://{tmp_path}/archive")


class PrefixWatchingStore:
    def __init__(self, inner: ArchiveStore) -> None:
        self._inner = inner
        self.written: list[str] = []

    def put(self, key: str, data: bytes) -> bool:
        self.written.append(key)
        return self._inner.put(key, data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _validated(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    run(_settings(JOB_HUNTER_L2_AUDIT_MOD="5"), pg, store,
        engine=FakeEngine([GOOD]), max_docs=10, max_usd=5.0)


def test_consolidate_emits_drift_audit_queue_and_refuter_summary(
    pg: Conn, store: ArchiveStore
) -> None:
    _validated(pg, store)
    verdict = EngineResult(
        raw_text=json.dumps({"verdict": "refute", "reasons": ["bad quote"]}),
        observed_model="z-ai/glm-5.2:free", input_tokens=1, output_tokens=1, cost_usd=0.0,
    )
    refute_doc(_settings(), pg, store, FakeEngine([verdict]), DH)  # -> needs_review
    row = _state_row(pg)
    assert row and row["status"] == "needs_review"

    out = consolidate(pg, store)
    assert out["skipped"] is None
    artifact_keys = list(store.list(keys.X_CONSOLIDATION_PREFIX))
    assert len(artifact_keys) == 1
    artifact = json.loads(store.get(artifact_keys[0]))
    assert artifact["counts"]["needs_review"] == 1
    queue = artifact["audit_queue"]
    assert queue and queue[0]["document_hash"] == DH
    assert queue[0]["status"] == "needs_review"
    refuters = artifact["refuter"]
    assert refuters["refute"] == 1
    # validated-only inputs: the demoted row is in the queue, not the series
    assert artifact["counts"].get("validated", 0) == 0


def test_noop_when_the_last_artifact_is_fresh(pg: Conn, store: ArchiveStore) -> None:
    _validated(pg, store)
    first = consolidate(pg, store)
    assert first["skipped"] is None
    second = consolidate(pg, store)
    assert second["skipped"] == "fresh"
    assert len(list(store.list(keys.X_CONSOLIDATION_PREFIX))) == 1
    # a forced re-run in the same second would collide with the write-once
    # key, so stamp it a step later like a real later run
    from datetime import timedelta

    from jobhunter.timeutil import utcnow

    forced = consolidate(pg, store, force=True, now=utcnow() + timedelta(seconds=2))
    assert forced["skipped"] is None
    assert len(list(store.list(keys.X_CONSOLIDATION_PREFIX))) == 2


def test_writes_stay_inside_the_consolidation_prefix(
    pg: Conn, store: ArchiveStore
) -> None:
    _validated(pg, store)
    watching = PrefixWatchingStore(store)
    consolidate(pg, watching)  # type: ignore[arg-type]
    assert watching.written
    assert all(k.startswith(keys.X_CONSOLIDATION_PREFIX) for k in watching.written)
