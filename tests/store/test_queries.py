from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from jobhunter.archive.local import LocalFS
from jobhunter.models import Board
from jobhunter.store.lifecycle import Ingestor
from jobhunter.store.queries import (
    board_health,
    boards_overview,
    database_size,
    docs_for_events,
    events_after_watermark,
    events_page,
    events_since,
    open_counts,
    panel_rows,
    posting_detail,
    postings_page,
    served_profiles,
)
from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry

DAY0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
DAY1 = DAY0 + timedelta(days=1)
DAY2 = DAY0 + timedelta(days=2)
# the engine tuple `views.postings_view` scopes the authorization columns to;
# nothing here is extracted, so every row reads null in both
ENGINE = {"model_regex": ".*", "prompt_version": "p", "schema_version": "4",
          "validator_version": "v", "normalizer_version": "md/1"}


def test_queries(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    store = LocalFS(tmp_path)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    ing = Ingestor(pg, store)
    ing.ingest(make_manifest(
        store, "ashby", "ramp", t0,
        board_payload("ashby", [ab_record("x", "T", "<p>t</p>"), ab_record("y", "U", "<p>u</p>")]),
        registry_revision=rev,
    ))
    ing.ingest(make_manifest(
        store, "ashby", "ramp", t0 + timedelta(days=1),
        board_payload("ashby", [ab_record("x", "T2", "<p>t</p>")]),
        registry_revision=rev,
    ))
    pg.commit()
    ev = events_since(pg, t0 + timedelta(hours=1))
    assert [(e["kind"], e["uid"]) for e in ev] == [
        ("changed", "ab:ramp:x"), ("closed", "ab:ramp:y"),
    ]
    assert ev[0]["title"] == "T2" and ev[0]["url"].endswith("/x")
    assert ev[1]["closed_lower_at"] == t0 and ev[1]["closed_upper_at"] == t0 + timedelta(days=1)
    assert panel_rows(pg)[0]["board"] == "ramp"
    h = board_health(pg)["ashby:ramp"]
    assert h["health"] == "ok" and h["observed_count"] == 1
    assert open_counts(pg) == {"ashby:ramp": 1}


def test_events_join_is_scoped_by_uid(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    store = LocalFS(tmp_path)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    same = "<p>same</p>"
    Ingestor(pg, store).ingest(make_manifest(
        store, "ashby", "ramp", t0,
        board_payload("ashby", [ab_record("x", "Same", same), ab_record("y", "Same", same)]),
        registry_revision=rev))
    pg.commit()
    ev = {e["uid"]: e for e in events_since(pg, t0 - timedelta(hours=1))}
    assert ev["ab:ramp:x"]["url"].endswith("/x") and ev["ab:ramp:y"]["url"].endswith("/y")


def test_database_size(pg: psycopg.Connection[dict[str, Any]]) -> None:
    n = database_size(pg)
    assert isinstance(n, int) and n > 0


def _corpus(pg: psycopg.Connection[dict[str, Any]], tmp_path: Path) -> None:
    """Three ingest days on one board: three opens at DAY0; two changes and one
    open at DAY1 (one instant, several events — the watermark tie-break case);
    one close at DAY2."""
    store = LocalFS(tmp_path)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    ing = Ingestor(pg, store)
    ing.ingest(make_manifest(
        store, "ashby", "ramp", DAY0,
        board_payload("ashby", [
            ab_record("x", "Rust Engineer", "<p>x</p>"),
            ab_record("y", "Data Scientist", "<p>y</p>"),
            ab_record("z", "Designer", "<p>z</p>"),
        ]),
        registry_revision=rev))
    ing.ingest(make_manifest(
        store, "ashby", "ramp", DAY1,
        board_payload("ashby", [
            ab_record("x", "Rust Engineer II", "<p>x2</p>"),
            ab_record("y", "Data Scientist II", "<p>y2</p>"),
            ab_record("z", "Designer", "<p>z</p>"),
            ab_record("w", "Recruiter", "<p>w</p>"),
        ]),
        registry_revision=rev))
    ing.ingest(make_manifest(
        store, "ashby", "ramp", DAY2,
        board_payload("ashby", [
            ab_record("x", "Rust Engineer II", "<p>x2</p>"),
            ab_record("z", "Designer", "<p>z</p>"),
            ab_record("w", "Recruiter", "<p>w</p>"),
        ]),
        registry_revision=rev))
    pg.commit()


def test_postings_page_keyset_pagination(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    _corpus(pg, tmp_path)
    page1 = postings_page(pg, engine=ENGINE, limit=2)
    assert len(page1) == 3  # limit + 1: the caller learns it truncated
    emitted = page1[:2]
    assert [r["uid"] for r in emitted] == ["ab:ramp:w", "ab:ramp:z"]
    assert emitted[0]["title"] == "Recruiter" and emitted[0]["company"] == "Ramp"
    assert emitted[0]["source"] == "ashby" and emitted[0]["board"] == "ramp"
    assert emitted[0]["url"].endswith("/w") and emitted[0]["status"] == "open"
    assert emitted[0]["version_count"] == 1 and emitted[0]["reopen_count"] == 0
    assert emitted[0]["first_seen_at"] == DAY1 and emitted[0]["last_seen_at"] == DAY2
    cursor = f"{emitted[-1]['first_seen_at'].isoformat()}|{emitted[-1]['uid']}"
    page2 = postings_page(pg, engine=ENGINE, limit=2, after=cursor)
    assert [r["uid"] for r in page2] == ["ab:ramp:y", "ab:ramp:x"]  # no overlap, page exhausted


def test_postings_page_filters(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    _corpus(pg, tmp_path)
    closed = postings_page(pg, engine=ENGINE, status="closed")
    assert [r["uid"] for r in closed] == ["ab:ramp:y"]
    assert closed[0]["closed_lower_at"] == DAY1 and closed[0]["closed_upper_at"] == DAY2
    assert {r["uid"] for r in postings_page(pg, engine=ENGINE, status="open")} == {
        "ab:ramp:x", "ab:ramp:z", "ab:ramp:w",
    }
    assert [r["uid"] for r in postings_page(pg, engine=ENGINE, since=DAY1)] == ["ab:ramp:w"]
    assert postings_page(pg, engine=ENGINE, source="greenhouse") == []
    assert postings_page(pg, engine=ENGINE, board="palantir") == []
    assert len(postings_page(pg, engine=ENGINE, source="ashby", board="ramp")) == 4


def test_postings_page_search_is_case_insensitive_over_title_and_company(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    _corpus(pg, tmp_path)
    assert [r["uid"] for r in postings_page(pg, engine=ENGINE, search="rust")] == ["ab:ramp:x"]
    # the company matches every posting
    assert len(postings_page(pg, engine=ENGINE, search="rAmP")) == 4
    assert postings_page(pg, engine=ENGINE, search="kubernetes") == []


def test_posting_detail(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    _corpus(pg, tmp_path)
    d = posting_detail(pg, "ab:ramp:x")
    assert d is not None
    assert d["status"] == "open" and d["version_count"] == 2 and d["board"] == "ramp"
    assert [(v["title"], v["at"]) for v in d["versions"]] == [
        ("Rust Engineer", DAY0), ("Rust Engineer II", DAY1),
    ]
    assert [e["kind"] for e in d["events"]] == ["opened", "changed"]
    assert d["events"][1]["at"] == DAY1
    row = pg.execute(
        "SELECT d.document_hash FROM documents d JOIN postings p"
        " ON p.current_version_hash = d.version_hash"
        " WHERE p.uid = %s AND d.normalizer_version = 'md/1'",
        ("ab:ramp:x",),
    ).fetchone()
    assert row is not None and d["document_hash"] == row["document_hash"]
    assert posting_detail(pg, "ab:ramp:nope") is None


def test_events_page_filters_and_keyset(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    _corpus(pg, tmp_path)
    first = events_page(pg, limit=2)
    assert len(first) == 3 and [e["kind"] for e in first[:2]] == ["opened", "opened"]
    assert first[0]["source"] == "ashby" and first[0]["board"] == "ramp"
    nxt = events_page(pg, limit=2, after_event_id=first[1]["event_id"])
    assert all(e["event_id"] > first[1]["event_id"] for e in nxt)
    closed = events_page(pg, kinds=("closed",))
    assert [e["uid"] for e in closed] == ["ab:ramp:y"]
    assert closed[0]["closed_lower_at"] == DAY1 and closed[0]["closed_upper_at"] == DAY2
    assert closed[0]["title"] == "Data Scientist II"
    assert [e["kind"] for e in events_page(pg, uid="ab:ramp:w")] == ["opened"]
    assert [e["kind"] for e in events_page(pg, since=DAY2)] == ["closed"]
    assert events_page(pg, board="palantir") == []
    assert len(events_page(pg, kinds=("opened", "changed"))) == 6


def test_events_after_watermark_excludes_the_tie_break_ids(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    _corpus(pg, tmp_path)
    all_events = events_page(pg, limit=500)
    at_day1 = [e for e in all_events if e["at"] == DAY1]
    assert len(at_day1) == 3  # two changes and an open share the instant
    close_id = [e["event_id"] for e in all_events if e["kind"] == "closed"]
    reported = tuple(e["event_id"] for e in at_day1[:2])
    rows = events_after_watermark(pg, at=DAY1, exclude_ids=reported, limit=50)
    assert [e["event_id"] for e in rows] == [at_day1[2]["event_id"], *close_id]
    # nothing reported at that instant yet: the whole instant comes back, plus later
    assert len(events_after_watermark(pg, at=DAY1, exclude_ids=(), limit=50)) == 4
    # limit + 1 rows so the caller can mark truncation
    assert len(events_after_watermark(pg, at=DAY0, exclude_ids=(), limit=1)) == 2


def test_boards_overview(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    _corpus(pg, tmp_path)
    rows = boards_overview(pg)
    assert [r["board"] for r in rows] == ["ashby:ramp"]
    assert rows[0]["health"] == "ok" and rows[0]["open"] == 3
    assert rows[0]["error"] is None and rows[0]["started_at"] == DAY2


def test_docs_for_events(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    _corpus(pg, tmp_path)
    got = docs_for_events(pg, ["ab:ramp:x", "ab:ramp:y", "ab:ramp:nope"], "md/1")
    assert set(got) == {"ab:ramp:x", "ab:ramp:y"}
    row = pg.execute(
        "SELECT d.document_hash FROM documents d JOIN postings p"
        " ON p.current_version_hash = d.version_hash"
        " WHERE p.uid = %s AND d.normalizer_version = 'md/1'",
        ("ab:ramp:x",),
    ).fetchone()
    assert row is not None and got["ab:ramp:x"] == row["document_hash"]
    assert docs_for_events(pg, ["ab:ramp:x"], "md/999") == {}
    assert docs_for_events(pg, [], "md/1") == {}


def test_served_profiles_carry_review_rows_and_say_which_they_are(
    pg: psycopg.Connection[dict[str, Any]]
) -> None:
    """Parsing contract v3 §4: a `needs_review` row serves like a validated one.
    The status travels with the blob so a reader can see which it got; a
    quarantined row and another engine's row still do not come back at all."""
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.state import DerivedState, globs_to_regex
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.store import extraction

    config: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION, "schema_version": "1",
        "validator_version": VALIDATOR_VERSION,
    }
    hashes = ["d" * 63 + n for n in "1234"]
    profile = {"demand_profile": {"areas": [{"name": "Rust", "kind": "skill"}]}}
    for dh, model, status in (
        (hashes[0], "z-ai/glm-5.2:free", "validated"),
        (hashes[1], "openai/gpt-5.6-sol", "validated"),    # outside the engine glob
        (hashes[2], "z-ai/glm-5.2:free", "needs_review"),
        (hashes[3], "z-ai/glm-5.2:free", "quarantined"),
    ):
        extraction.upsert_state(
            pg, document_hash=dh, model=model, **config,
            state=DerivedState(status, None), profile=profile,
            updated_at="2026-08-27T00:00:00Z",
        )
    pg.commit()
    rx = globs_to_regex(("z-ai/*",))
    got = served_profiles(pg, hashes, model_regex=rx, **config)
    assert set(got) == {hashes[0], hashes[2]}
    assert got[hashes[0]]["status"] == "validated"
    assert got[hashes[2]]["status"] == "needs_review"
    assert got[hashes[0]]["profile"]["demand_profile"]["areas"][0]["name"] == "Rust"
    assert served_profiles(pg, [], model_regex=rx, **config) == {}
    other = dict(config, prompt_version="demand-profile/vOTHER")
    assert served_profiles(pg, hashes, model_regex=rx, **other) == {}


def test_claims_by_mention_returns_a_review_rows_skills(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    """The aggregate is what `q claims` reads, so once the store refills a
    review row's mentions the read path returns them with no filter of its own
    standing in the way."""
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.state import DerivedState, globs_to_regex
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.l2.v2 import serve
    from jobhunter.store import extraction
    from jobhunter.store.queries import claims_by_mention
    from tests.l2.v2.conftest import make_serving_record

    _corpus(pg, tmp_path)
    row = pg.execute(
        "SELECT d.document_hash FROM documents d JOIN postings p"
        " ON p.current_version_hash = d.version_hash WHERE p.uid = 'ab:ramp:x'"
    ).fetchone()
    assert row is not None
    config: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION, "schema_version": "3",
        "validator_version": VALIDATOR_VERSION,
    }
    record = make_serving_record()
    extraction.upsert_state(
        pg, document_hash=str(row["document_hash"]), model="z-ai/glm-5.2:free", **config,
        state=DerivedState("needs_review", None), profile=serve.profile_of(record),
        mentions=serve.mention_rows(record), updated_at="2026-09-22T00:00:00Z",
    )
    pg.commit()
    engine = {**config, "model_regex": globs_to_regex(("z-ai/*",))}
    rows = claims_by_mention(pg, mention="cpa", **engine)
    assert [(r["uid"], r["mention"]) for r in rows] == [("ab:ramp:x", "CPA")]
    # the schema the row was written under rides along: two nulls mean one thing
    # on a schema-3 row and another on a legacy one, and only this tells them apart
    assert [r["schema_version"] for r in rows] == ["3"]


def test_mention_contexts_narrows_the_blob_to_one_surface_and_its_statements(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    """`q claims` needs two strings per row off each document's blob, and stored
    blobs run tens of kilobytes. This fetches the mention entries for the surface
    asked about plus the statements they link, and nothing else — the served
    status still rides along, because a row that stopped serving must not be
    labelled with a tier it no longer holds.
    """
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.state import DerivedState, globs_to_regex
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.l2.v2 import serve
    from jobhunter.store import extraction
    from jobhunter.store.queries import mention_contexts
    from tests.l2.v2.conftest import make_serving_record

    _corpus(pg, tmp_path)
    row = pg.execute(
        "SELECT d.document_hash FROM documents d JOIN postings p"
        " ON p.current_version_hash = d.version_hash WHERE p.uid = 'ab:ramp:x'"
    ).fetchone()
    assert row is not None
    dh = str(row["document_hash"])
    config: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION, "schema_version": "3",
        "validator_version": VALIDATOR_VERSION,
    }
    record = make_serving_record()
    profile = serve.profile_of(record)
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", **config,
        state=DerivedState("needs_review", None), profile=profile,
        mentions=serve.mention_rows(record), updated_at="2026-09-22T00:00:00Z",
    )
    pg.commit()
    engine = {**config, "model_regex": globs_to_regex(("z-ai/*",))}
    got = mention_contexts(pg, [dh], mention="cpa", **engine)
    assert set(got) == {dh}
    assert got[dh]["status"] == "needs_review"
    narrowed = got[dh]["profile"]
    assert set(narrowed) == {"mentions", "statements"}
    assert [m["surface"] for m in narrowed["mentions"]] == ["CPA"]
    assert [s["id"] for s in narrowed["statements"]] == ["s_cert"]
    # what comes back is the blob's own entries, verbatim and in record order
    assert narrowed["mentions"] == [m for m in profile["mentions"] if m["surface"] == "CPA"]
    assert narrowed["statements"] == [s for s in profile["statements"] if s["id"] == "s_cert"]
    # a surface the record never names narrows to nothing, not to everything
    empty = mention_contexts(pg, [dh], mention="rust", **engine)
    assert empty[dh]["profile"] == {"mentions": [], "statements": []}
    assert mention_contexts(pg, [], mention="cpa", **engine) == {}
    other = dict(engine, prompt_version="demand-profile/vOTHER")
    assert mention_contexts(pg, [dh], mention="cpa", **other) == {}
