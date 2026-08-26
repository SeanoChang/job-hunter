from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from jobhunter.l2.state import DerivedState
from jobhunter.store import extraction
from tests.l2.test_attempts import _attempt

Conn = psycopg.Connection[dict[str, Any]]

CONFIG = {
    "prompt_version": "demand-profile/v1",
    "schema_version": "1",
    "validator_version": "1",
}


def test_globs_to_regex() -> None:
    rx = extraction.globs_to_regex(("z-ai/glm-5.2*", "nvidia/*"))
    import re

    assert re.match(rx, "z-ai/glm-5.2:free")
    assert re.match(rx, "nvidia/nemotron-3-ultra-550b-a55b:free")
    assert not re.match(rx, "openai/gpt-5.6-sol")
    assert not re.match(rx, "xz-ai/glm-5.2:free")


def _seed(pg: Conn) -> None:
    now = datetime.now(UTC)
    pg.execute(
        "INSERT INTO fetch_attempts (attempt_id, run_id, source, board, started_at,"
        " finished_at, transport, health, adapter_version, registry_revision, cli_version)"
        " VALUES ('att1','r1','greenhouse','x',%s,%s,'ok','ok','greenhouse/1','rev','0')",
        (now, now),
    )
    docs = [
        # (uid, version_hash, document_hash, status, current, closed_upper, last_seen)
        ("gh:x:1", "v1", "d" * 63 + "1", "open", "v1", None, now),
        ("gh:x:2", "v2", "d" * 63 + "2", "open", "vNEW", None, now - timedelta(days=1)),
        ("gh:x:3", "v3", "d" * 63 + "3", "closed", "v3", now - timedelta(days=10), now),
    ]
    for uid, vh, dh, status, current, closed_upper, last_seen in docs:
        pg.execute(
            "INSERT INTO posting_versions (version_hash, version_hash_v, uid, source, board,"
            " source_id, title, company, locations, first_seen_attempt)"
            " VALUES (%s,1,%s,'greenhouse','x',%s,'t','c','[]','att1')",
            (vh, uid, uid.split(":")[-1]),
        )
        pg.execute(
            "INSERT INTO documents (version_hash, normalizer_version, document_hash, markdown)"
            " VALUES (%s,'md/1',%s,'## Doc')",
            (vh, dh),
        )
        pg.execute(
            "INSERT INTO postings (uid, source, board, source_id, status, current_version_hash,"
            " first_seen_attempt, first_seen_at, last_seen_attempt, last_seen_at,"
            " closed_upper_at)"
            " VALUES (%s,'greenhouse','x',%s,%s,%s,'att1',%s,'att1',%s,%s)",
            (uid, uid.split(":")[-1], status, current, now, last_seen, closed_upper),
        )


def test_queue_priorities_and_blocking(pg: Conn) -> None:
    _seed(pg)
    kwargs: dict[str, Any] = {
        **CONFIG,
        "model_regex": extraction.globs_to_regex(("z-ai/*",)),
        "normalizer_version": "md/1",
        "limit": 10,
    }
    order = extraction.queue(pg, **kwargs)
    # open-current -> open-but-older-version -> recent close
    assert order == ["d" * 63 + "1", "d" * 63 + "2", "d" * 63 + "3"]

    a = _attempt(document_hash="d" * 63 + "1")
    extraction.record_attempt(pg, a, None)
    extraction.upsert_state(
        pg,
        document_hash="d" * 63 + "1",
        model="z-ai/glm-5.2:free",
        **CONFIG,
        state=DerivedState("quarantined", None),
        profile=None,
        updated_at="2026-08-27T00:00:00Z",
    )
    assert ("d" * 63 + "1") not in extraction.queue(pg, **kwargs)  # any status blocks

    other = dict(kwargs, prompt_version="demand-profile/v2")
    assert ("d" * 63 + "1") in extraction.queue(pg, **other)  # new config re-selects


def test_attempt_idempotent_and_watermark(pg: Conn) -> None:
    a = _attempt()
    extraction.record_attempt(pg, a, {"errors": 1})
    extraction.record_attempt(pg, a, {"errors": 1})
    rows = pg.execute("SELECT count(*) AS n FROM extraction_attempts").fetchone()
    assert rows and rows["n"] == 1
    w = extraction.watermark(pg)
    assert w is not None and w.year == 2026


def test_state_roundtrip_and_delete(pg: Conn) -> None:
    a = _attempt()
    extraction.record_attempt(pg, a, None)
    key: dict[str, Any] = {
        "document_hash": a.document_hash,
        "model": "z-ai/glm-5.2:free",
        **CONFIG,
    }
    extraction.upsert_state(
        pg, **key, state=DerivedState("validated", a.attempt_key),
        profile={"demand_profile": {"areas": []}}, updated_at="2026-08-27T00:00:00Z",
    )
    row = pg.execute("SELECT status, profile FROM extractions").fetchone()
    assert row and row["status"] == "validated" and row["profile"]["demand_profile"] == {
        "areas": []
    }
    extraction.upsert_state(
        pg, **key, state=DerivedState(None, None), profile=None,
        updated_at="2026-08-27T00:00:00Z",
    )
    assert pg.execute("SELECT count(*) AS n FROM extractions").fetchone()["n"] == 0  # type: ignore[index]


def test_attempts_and_reviews_for(pg: Conn) -> None:
    a = _attempt()
    extraction.record_attempt(pg, a, None)
    extraction.record_review(
        pg, review_key="extractions/reviews/2026/08/28T000000Z-abcdefabcdef.json",
        document_hash=a.document_hash, model="z-ai/glm-5.2:free", **CONFIG,
        verb="flag", payload=None, actor="human", at="2026-08-28T00:00:00Z",
    )
    attempts = extraction.attempts_for(pg, a.document_hash)
    assert len(attempts) == 1 and attempts[0].outcome == "ok"
    assert attempts[0].attempt_key == a.attempt_key
    reviews = extraction.reviews_for(pg, a.document_hash)
    assert len(reviews) == 1 and reviews[0].verb == "flag"
