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


def test_queue_can_be_limited_to_matching_titles(pg: Conn) -> None:
    """A re-extraction can start with the slice the reader hunts in (internship
    and new-grad roles) instead of the whole corpus: `title_regex` keeps only
    documents whose posting version's title matches, case-insensitively, and
    leaves the priority order untouched."""
    _seed(pg)
    titles = {"1": "Software Engineer Intern", "2": "Senior Staff Engineer",
              "3": "New Grad Software Engineer"}
    for suffix, title in titles.items():
        pg.execute(
            "UPDATE posting_versions SET title = %s WHERE version_hash ="
            " (SELECT version_hash FROM documents WHERE document_hash = %s)",
            (title, "d" * 63 + suffix),
        )
    kwargs: dict[str, Any] = {
        **CONFIG,
        "model_regex": extraction.globs_to_regex(("z-ai/*",)),
        "normalizer_version": "md/1",
        "limit": 10,
    }
    entry = r"\m(intern|new grad)"
    assert extraction.queue(pg, **kwargs, title_regex=entry) == ["d" * 63 + "1", "d" * 63 + "3"]
    assert len(extraction.queue(pg, **kwargs, title_regex=None)) == 3


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


def test_profile_mentions_are_a_served_status_aggregate(pg: Conn) -> None:
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


def test_profile_mentions_are_normalized_at_write_time(pg: Conn) -> None:
    """The live corpus fragments the aggregate (5,083 distinct mentions over
    8,315 rows): compounds split, trailing parentheticals drop, protected
    compounds (CI/CD, TCP/IP…) stay whole, and casings dedupe within a doc."""
    dh = "d" * 63 + "2"
    profile = _fixture_profile()
    profile["demand_profile"]["areas"][0]["mentions"] = [
        "Python/C/C++", "python", "CI/CD", "HTTP/2", "A/B testing",
        "Python (pandas, PySpark)", "Kubernetes and/or Docker", "TCP/IP",
    ]
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", **CONFIG,
        state=DerivedState("validated", None), profile=profile,
        updated_at="2026-08-27T00:00:00Z",
    )
    assert {m for m, _, _ in _mentions(pg)} == {
        "Python", "C", "C++", "CI/CD", "HTTP/2", "A/B testing",
        "Kubernetes", "Docker", "TCP/IP",
    }


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


def _serving_blob() -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """The settled schema-3 record as the runner hands it to the store: the
    stored blob and the mention projection that belongs to it."""
    from jobhunter.l2.v2 import serve
    from tests.l2.v2.conftest import make_serving_record

    record = make_serving_record()
    return serve.profile_of(record), serve.mention_rows(record)


V3_CONFIG = {**CONFIG, "schema_version": "3"}


def _chosen(pg: Conn, dh: str) -> str:
    """One archived attempt on `dh`, so a row may cite it as its candidate."""
    a = _attempt(document_hash=dh, schema_version="3")
    extraction.record_attempt(pg, a, None)
    return a.attempt_key


def test_profile_mentions_are_refilled_for_a_needs_review_row(pg: Conn) -> None:
    """Parsing contract v3 §4: every verified extraction serves. A row parked
    for review still chose a candidate, and what that candidate extracted is
    what the aggregate carries — with its quality note attached to the blob."""
    dh = "d" * 63 + "1"
    key: dict[str, Any] = {"document_hash": dh, "model": "z-ai/glm-5.2:free", **V3_CONFIG}
    att = _chosen(pg, dh)
    profile, mentions = _serving_blob()
    extraction.upsert_state(
        pg, **key, state=DerivedState("needs_review", att), profile=profile,
        mentions=mentions, updated_at="2026-09-22T00:00:00Z",
    )
    assert _mentions(pg) == [
        ("Bachelor's degree", "qualification", "contextual"),
        ("CPA", "qualification", "contextual"),
    ]
    row = pg.execute("SELECT status, profile FROM extractions").fetchone()
    assert row is not None and row["status"] == "needs_review"
    assert row["profile"]["quality"]["sample_notes"]["splits"] == {"kind": 2}

    # the flip a human review performs: same rows, no duplicates, no loss
    extraction.upsert_state(
        pg, **key, state=DerivedState("validated", att), profile=profile,
        mentions=mentions, updated_at="2026-09-22T01:00:00Z",
    )
    assert _mentions(pg) == [
        ("Bachelor's degree", "qualification", "contextual"),
        ("CPA", "qualification", "contextual"),
    ]
    count = pg.execute("SELECT count(*) AS n FROM profile_mentions").fetchone()
    assert count is not None and count["n"] == 2


def test_quarantined_and_pending_rows_still_clear_the_aggregate(pg: Conn) -> None:
    dh = "d" * 63 + "1"
    key: dict[str, Any] = {"document_hash": dh, "model": "z-ai/glm-5.2:free", **V3_CONFIG}
    att = _chosen(pg, dh)
    profile, mentions = _serving_blob()
    extraction.upsert_state(
        pg, **key, state=DerivedState("needs_review", att), profile=profile,
        mentions=mentions, updated_at="2026-09-22T00:00:00Z",
    )
    assert len(_mentions(pg)) == 2
    extraction.upsert_state(  # a quarantined row asserts nothing, blob or not
        pg, **key, state=DerivedState("quarantined", None), profile=profile,
        mentions=mentions, updated_at="2026-09-22T02:00:00Z",
    )
    assert _mentions(pg) == []

    extraction.upsert_state(
        pg, **key, state=DerivedState("needs_review", att), profile=profile,
        mentions=mentions, updated_at="2026-09-22T03:00:00Z",
    )
    assert len(_mentions(pg)) == 2
    extraction.upsert_state(  # back to pending: the config's rows go entirely
        pg, **key, state=DerivedState(None, None), profile=None,
        updated_at="2026-09-22T04:00:00Z",
    )
    assert _mentions(pg) == []
    assert pg.execute("SELECT count(*) AS n FROM extractions").fetchone()["n"] == 0  # type: ignore[index]


def test_a_needs_review_row_without_a_chosen_candidate_serves_nothing(pg: Conn) -> None:
    """The predicate is status AND a chosen candidate: a review row whose fold
    never chose one has no profile to project, so the key stays empty."""
    dh = "d" * 63 + "1"
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", **V3_CONFIG,
        state=DerivedState("needs_review", None), profile=None,
        updated_at="2026-09-22T00:00:00Z",
    )
    assert _mentions(pg) == []
    row = pg.execute("SELECT status FROM extractions").fetchone()
    assert row is not None and row["status"] == "needs_review"


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


def test_the_runners_serving_predicate_is_extractions_own() -> None:
    """`runner._write_derived` decides whether to project a blob and its
    mentions with a bare `state.status in ("validated", "needs_review")`
    literal — `extraction.SERVING_STATUSES` spelled a second time. Two spellings
    of one rule drift silently and in the worst direction: the writer projects a
    status the store then refuses to keep, or keeps one it refuses to project,
    and the aggregate ends up disagreeing with the blob about one document.

    The literal sits inside a function body, so it is read out of the source
    rather than imported. `runner.py` is not this increment's to edit — folding
    the two together is a follow-up — but it cannot move without this failing.
    """
    import ast
    import inspect

    from jobhunter.l2 import runner

    found: list[frozenset[str]] = []
    for node in ast.walk(ast.parse(inspect.getsource(runner))):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.In):
            continue
        if not (isinstance(node.left, ast.Attribute) and node.left.attr == "status"):
            continue
        elts = getattr(node.comparators[0], "elts", None)
        if elts is None:
            continue
        found.append(frozenset(
            e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ))
    assert extraction.SERVING_STATUSES in found, (
        f"runner.py carries no `.status in {sorted(extraction.SERVING_STATUSES)}` literal; "
        f"the status tuples it does test against are {[sorted(f) for f in found]}"
    )


# --- profile_authorization: the sponsorship filter's table (contract v4 §5) --


def _authorization_rows(pg: Conn) -> list[tuple[str, str, str, bool]]:
    rows = pg.execute(
        "SELECT document_hash, model, sponsorship, citizenship_required"
        " FROM profile_authorization ORDER BY document_hash, model"
    ).fetchall()
    return [(r["document_hash"], r["model"], r["sponsorship"], r["citizenship_required"])
            for r in rows]


def _v4_blob(case: str = "visa") -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """A settled §7 case as the runner hands it to the store: visa (no),
    may_sponsor (yes), anduril (undeclared, citizenship required)."""
    from jobhunter.l2.v2 import serve
    from tests.l2.v2.v4_serving import v4_record

    record = v4_record(case)
    return serve.profile_of(record), serve.mention_rows(record)


def test_profile_authorization_is_written_beside_the_mentions(pg: Conn) -> None:
    """One row per (document, engine tuple), from the same write that refills
    profile_mentions, and only for a schema-4 blob that carries the derived
    authorization."""
    dh = "d" * 63 + "1"
    key: dict[str, Any] = {"document_hash": dh, "model": "z-ai/glm-5.2:free", **V3_CONFIG}
    att = _chosen(pg, dh)
    profile, mentions = _v4_blob("anduril")
    extraction.upsert_state(
        pg, **key, state=DerivedState("needs_review", att), profile=profile,
        mentions=mentions, updated_at="2026-10-07T00:00:00Z",
    )
    assert _authorization_rows(pg) == [(dh, "z-ai/glm-5.2:free", "undeclared", True)]
    row = pg.execute("SELECT * FROM profile_authorization").fetchone()
    assert row is not None and row["schema_version"] == V3_CONFIG["schema_version"]
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["validator_version"] == VALIDATOR_VERSION

    # a re-extraction rewrites the row, never adds a second one
    profile, mentions = _v4_blob("may_sponsor")
    extraction.upsert_state(
        pg, **key, state=DerivedState("validated", att), profile=profile,
        mentions=mentions, updated_at="2026-10-07T01:00:00Z",
    )
    assert _authorization_rows(pg) == [(dh, "z-ai/glm-5.2:free", "yes", False)]


def test_profile_authorization_follows_the_extraction_row(pg: Conn) -> None:
    dh = "d" * 63 + "1"
    att = _chosen(pg, dh)
    profile, mentions = _v4_blob()
    for model in ("z-ai/glm-5.2:free", "z-ai/glm-5.2"):
        extraction.upsert_state(
            pg, document_hash=dh, model=model, **V3_CONFIG,
            state=DerivedState("validated", att), profile=profile, mentions=mentions,
            updated_at="2026-10-07T00:00:00Z",
        )
    # the stale model spelling went with its extractions row
    assert _authorization_rows(pg) == [(dh, "z-ai/glm-5.2", "no", False)]

    other = dict(V3_CONFIG, prompt_version="demand-profile/vOTHER")
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2", **other,
        state=DerivedState("validated", att), profile=profile, mentions=mentions,
        updated_at="2026-10-07T00:00:00Z",
    )
    assert len(_authorization_rows(pg)) == 2  # a second tuple is a second reading

    extraction.upsert_state(  # a quarantined row asserts nothing, blob or not
        pg, document_hash=dh, model="z-ai/glm-5.2", **other,
        state=DerivedState("quarantined", None), profile=profile, mentions=mentions,
        updated_at="2026-10-07T01:00:00Z",
    )
    assert len(_authorization_rows(pg)) == 1

    extraction.upsert_state(  # back to pending: the config's row goes entirely
        pg, document_hash=dh, model="z-ai/glm-5.2", **V3_CONFIG,
        state=DerivedState(None, None), profile=None, updated_at="2026-10-07T02:00:00Z",
    )
    assert _authorization_rows(pg) == []


def test_a_schema_3_blob_writes_no_authorization_row(pg: Conn) -> None:
    """Schema 3 has no authorization reading. A missing row is what the filter
    reads as "not extracted", which is the truth for that partition."""
    dh = "d" * 63 + "1"
    att = _chosen(pg, dh)
    profile, mentions = _serving_blob()
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", **V3_CONFIG,
        state=DerivedState("validated", att), profile=profile, mentions=mentions,
        updated_at="2026-10-07T00:00:00Z",
    )
    assert len(_mentions(pg)) == 2
    assert _authorization_rows(pg) == []
