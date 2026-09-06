"""Demote-only refuter (M3, spec §4.4/§2): an engine pass over a VALIDATED
row that returns refute/uphold with cited reasons. A refute verdict appends
the review event to the archive BEFORE the row demotes to needs_review;
uphold is provenance only. It never promotes and never touches non-validated
rows — automated certification of generations is the self-poisoning loop the
spec forbids."""

import json
from typing import Any

import psycopg
import pytest

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.l2.engines import EngineResult
from jobhunter.l2.refuter import NotValidated, refute_doc
from jobhunter.l2.runner import run
from tests.l2.test_runner import DH, GOOD, FakeEngine, _seed_doc, _settings, _state_row

Conn = psycopg.Connection[dict[str, Any]]


@pytest.fixture
def store(tmp_path: Any) -> ArchiveStore:
    from jobhunter.archive import open_store

    return open_store(f"file://{tmp_path}/archive")


def _verdict(verdict: str, *reasons: str) -> EngineResult:
    return EngineResult(
        raw_text=json.dumps({"verdict": verdict, "reasons": list(reasons)}),
        observed_model="z-ai/glm-5.2:free",
        input_tokens=20, output_tokens=5, cost_usd=0.0,
    )


def _validated(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)
    run(_settings(JOB_HUNTER_L2_AUDIT_MOD="5"), pg, store,
        engine=FakeEngine([GOOD]), max_docs=10, max_usd=5.0)
    row = _state_row(pg)
    assert row and row["status"] == "validated"


def test_refute_verdict_demotes_to_needs_review(pg: Conn, store: ArchiveStore) -> None:
    _validated(pg, store)
    engine = FakeEngine([_verdict("refute", "quote contradicted")])
    out = refute_doc(_settings(), pg, store, engine, DH)
    assert out.verdict == "refute"
    row = _state_row(pg)
    assert row and row["status"] == "needs_review"
    assert row["reviewed_by"].startswith("refuter")
    events = [json.loads(store.get(k)) for k in store.list(keys.X_REVIEWS_PREFIX)]
    assert any(e["verb"] == "refute" and e["payload"]["reasons"] == ["quote contradicted"]
               for e in events)


def test_uphold_is_provenance_only(pg: Conn, store: ArchiveStore) -> None:
    _validated(pg, store)
    out = refute_doc(_settings(), pg, store, FakeEngine([_verdict("uphold")]), DH)
    assert out.verdict == "uphold"
    row = _state_row(pg)
    assert row and row["status"] == "validated"  # never promotes, never demotes on uphold
    events = [json.loads(store.get(k)) for k in store.list(keys.X_REVIEWS_PREFIX)]
    assert any(e["verb"] == "uphold" for e in events)  # the decision is provenance too


def test_refuses_non_validated_rows(pg: Conn, store: ArchiveStore) -> None:
    _seed_doc(pg)  # pending: no extraction ran
    with pytest.raises(NotValidated):
        refute_doc(_settings(), pg, store, FakeEngine([]), DH)


def test_garbage_verdict_changes_nothing(pg: Conn, store: ArchiveStore) -> None:
    _validated(pg, store)
    bad = EngineResult(raw_text="not json", observed_model="z-ai/glm-5.2:free",
                       input_tokens=1, output_tokens=1, cost_usd=0.0)
    out = refute_doc(_settings(), pg, store, FakeEngine([bad]), DH)
    assert out.verdict == "error"
    row = _state_row(pg)
    assert row and row["status"] == "validated"
