import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from jobhunter.l2.prompt import PROMPT_VERSION
from jobhunter.l2.state import DerivedState
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.store import extraction
from jobhunter.store.queries import claims_by_mention
from tests.l2.test_attempts import _attempt

Conn = psycopg.Connection[dict[str, Any]]

CONFIG = {
    "prompt_version": PROMPT_VERSION,
    "schema_version": "1",
    "validator_version": VALIDATOR_VERSION,
}
# the engine tuple in force, as `q claims` passes it
ENGINE = {**CONFIG, "model_regex": extraction.globs_to_regex(("z-ai/*",))}


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

    # a synthetic version, so this stays a different config no matter what
    # the live PROMPT_VERSION becomes
    other = dict(kwargs, prompt_version="demand-profile/vOTHER")
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
    attempts = extraction.attempts_for(pg, a.document_hash, **CONFIG)
    assert len(attempts) == 1 and attempts[0].outcome == "ok"
    assert attempts[0].attempt_key == a.attempt_key
    reviews = extraction.reviews_for(pg, a.document_hash, **CONFIG)
    assert len(reviews) == 1 and reviews[0].verb == "flag"


def _fixture_profile() -> dict[str, Any]:
    """The anthropic record: one technical, required area mentioning
    Python/React/TypeScript."""
    record = json.loads(
        (Path(__file__).parents[1] / "l2" / "fixtures" / "anthropic.extraction.json").read_text()
    )
    return {"facts": record["facts"], "demand_profile": record["demand_profile"]}


def _mentions(pg: Conn) -> list[tuple[str, str, str]]:
    rows = pg.execute(
        "SELECT mention, area_kind, importance FROM profile_mentions ORDER BY mention"
    ).fetchall()
    return [(r["mention"], r["area_kind"], r["importance"]) for r in rows]


def test_profile_mentions_are_a_validated_only_aggregate(pg: Conn) -> None:
    dh = "d" * 63 + "1"
    key: dict[str, Any] = {"document_hash": dh, "model": "z-ai/glm-5.2:free", **CONFIG}
    extraction.upsert_state(
        pg, **key, state=DerivedState("validated", None), profile=_fixture_profile(),
        updated_at="2026-08-27T00:00:00Z",
    )
    assert _mentions(pg) == [
        ("Python", "technical", "required"),
        ("React", "technical", "required"),
        ("TypeScript", "technical", "required"),
    ]
    row = pg.execute("SELECT * FROM profile_mentions WHERE mention = 'Python'").fetchone()
    assert row is not None and row["document_hash"] == dh
    assert row["model"] == "z-ai/glm-5.2:free" and row["prompt_version"] == PROMPT_VERSION
    assert row["schema_version"] == "1" and row["validator_version"] == VALIDATOR_VERSION

    # a rejection retracts what the corpus asserts, profile column or not
    extraction.upsert_state(
        pg, **key, state=DerivedState("rejected", None), profile=_fixture_profile(),
        updated_at="2026-08-28T00:00:00Z",
    )
    assert _mentions(pg) == []


def test_profile_mentions_follow_the_extraction_row(pg: Conn) -> None:
    dh = "d" * 63 + "1"
    for model in ("z-ai/glm-5.2:free", "z-ai/glm-5.2"):
        extraction.upsert_state(
            pg, document_hash=dh, model=model, **CONFIG,
            state=DerivedState("validated", None), profile=_fixture_profile(),
            updated_at="2026-08-27T00:00:00Z",
        )
    models = pg.execute("SELECT DISTINCT model FROM profile_mentions").fetchall()
    assert [m["model"] for m in models] == ["z-ai/glm-5.2"]  # the stale spelling went too

    other = dict(CONFIG, prompt_version="demand-profile/vOTHER")
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2", **other,
        state=DerivedState("validated", None), profile=_fixture_profile(),
        updated_at="2026-08-27T00:00:00Z",
    )
    assert len(_mentions(pg)) == 6  # a second config is a second set of claims

    extraction.upsert_state(  # back to pending: the config's rows go entirely
        pg, document_hash=dh, model="z-ai/glm-5.2", **CONFIG,
        state=DerivedState(None, None), profile=None, updated_at="2026-08-29T00:00:00Z",
    )
    assert len(_mentions(pg)) == 3


def _validate(pg: Conn, dh: str) -> None:
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", **CONFIG,
        state=DerivedState("validated", None), profile=_fixture_profile(),
        updated_at="2026-08-27T00:00:00Z",
    )


def test_claims_by_mention(pg: Conn) -> None:
    _seed(pg)
    for n in "123":
        _validate(pg, "d" * 63 + n)
    rows = claims_by_mention(pg, mention="python", **ENGINE)  # matching is case-insensitive
    # gh:x:2's document belongs to an older version, so no posting is on it now
    assert [r["uid"] for r in rows] == ["gh:x:1", "gh:x:3"]
    r = rows[0]
    assert r["document_hash"] == "d" * 63 + "1" and r["mention"] == "Python"
    assert r["area_kind"] == "technical" and r["importance"] == "required"
    assert r["source"] == "greenhouse" and r["board"] == "x"
    assert r["title"] == "t" and r["company"] == "c"
    assert claims_by_mention(pg, mention="Python", importance="preferred", **ENGINE) == []
    assert len(claims_by_mention(pg, mention="Python", importance="required", **ENGINE)) == 2
    assert len(
        claims_by_mention(pg, mention="Python", source="greenhouse", board="x", **ENGINE)
    ) == 2
    assert claims_by_mention(pg, mention="Python", board="other", **ENGINE) == []
    assert claims_by_mention(pg, mention="Rust", **ENGINE) == []
    # limit + 1 rows, like every other page: the caller marks truncation honestly
    assert len(claims_by_mention(pg, mention="Python", limit=1, **ENGINE)) == 2


def test_claims_by_mention_is_scoped_to_the_engine_in_force(pg: Conn) -> None:
    """`profile_mentions` keeps a row set per engine tuple the archive produced
    (`extract rebuild` replays historical configs on purpose). A retired prompt
    must not double the posting, nor answer for an importance the current
    extraction contradicts."""
    _seed(pg)
    dh = "d" * 63 + "1"
    retired = _fixture_profile()
    for area in retired["demand_profile"]["areas"]:
        area["importance"] = "preferred"  # what demand-profile/v3 said back then
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free",
        **dict(CONFIG, prompt_version="demand-profile/vOLD"),
        state=DerivedState("validated", None), profile=retired,
        updated_at="2026-08-01T00:00:00Z",
    )
    _validate(pg, dh)  # the tuple in force: required

    rows = claims_by_mention(pg, mention="Python", **ENGINE)
    assert [(r["uid"], r["importance"]) for r in rows] == [("gh:x:1", "required")]
    assert claims_by_mention(pg, mention="Python", importance="preferred", **ENGINE) == []
    # a model outside the glob in force is another engine, not this corpus
    other_model = dict(ENGINE, model_regex=extraction.globs_to_regex(("nvidia/*",)))
    assert claims_by_mention(pg, mention="Python", **other_model) == []
    # and the retired tuple is still readable when asked for by name
    old = dict(ENGINE, prompt_version="demand-profile/vOLD")
    assert [r["importance"] for r in claims_by_mention(pg, mention="Python", **old)] == [
        "preferred"
    ]
