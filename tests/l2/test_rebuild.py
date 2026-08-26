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
