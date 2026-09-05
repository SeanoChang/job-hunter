import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jobhunter import sources
from jobhunter.archive.keys import version_key
from jobhunter.archive.local import LocalFS
from jobhunter.models import SOURCE_PREFIX, Board
from jobhunter.store import extraction
from jobhunter.store.lifecycle import PENDING_DETAIL, Ingestor, OutOfOrder
from tests.store.helpers import (
    FakeTwoPhase,
    ab_record,
    board_payload,
    gh_record,
    lv_record,
    make_manifest,
    make_two_phase_manifest,
    wd_detail,
    wd_row,
    write_registry,
)

T0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
BOARDS = [Board("Anthropic", "greenhouse", "anthropic"), Board("Palantir", "lever", "palantir"),
          Board("Ramp", "ashby", "ramp")]


def day(n: int) -> datetime:
    return T0 + timedelta(days=n)


@pytest.fixture
def store(tmp_path: Path) -> LocalFS:
    return LocalFS(tmp_path / "archive")


@pytest.fixture
def rev(store: LocalFS) -> str:
    return write_registry(store, BOARDS)


def q(conn: psycopg.Connection[dict[str, Any]], sql: str, *args: Any) -> list[dict[str, Any]]:
    return conn.execute(sql, args).fetchall()


def test_ingest_ok_attempt_writes_provenance_and_presence(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    body = board_payload(
        "greenhouse", [gh_record(1, "A", "<p>a</p>"), gh_record(2, "B", "<p>b</p>")]
    )
    m = make_manifest(store, "greenhouse", "anthropic", day(0), body, registry_revision=rev)
    r = Ingestor(pg, store).ingest(m)
    pg.commit()
    assert r is not None and r.health == "ok"
    assert (r.observed_count, r.parsed_count, r.failed_count, r.unidentifiable_count) == (
        2, 2, 0, 0,
    )
    assert r.new_versions == 2 and r.new_documents == 2
    att = q(pg, "SELECT * FROM fetch_attempts")[0]
    assert att["health"] == "ok" and att["observed_count"] == 2
    assert att["prev_observed_count"] is None
    vs = q(pg, "SELECT uid, title, first_seen_attempt FROM posting_versions ORDER BY uid")
    assert [v["uid"] for v in vs] == ["gh:anthropic:1", "gh:anthropic:2"]
    assert all(v["first_seen_attempt"] == m.attempt_id for v in vs)
    for v in q(pg, "SELECT version_hash FROM posting_versions"):
        assert store.exists(version_key(v["version_hash"]))
    docs = q(pg, "SELECT normalizer_version, markdown FROM documents ORDER BY markdown")
    assert [d["markdown"] for d in docs] == ["a", "b"]
    assert docs[0]["normalizer_version"] == "md/1"
    pres = q(
        pg,
        "SELECT uid, parse_status, runs, first_attempt, last_attempt FROM presence ORDER BY uid",
    )
    assert len(pres) == 2 and all(p["runs"] == 1 and p["parse_status"] == "ok" for p in pres)
    assert all(p["first_attempt"] == p["last_attempt"] == m.attempt_id for p in pres)
    # snapshot applied (ordered explicitly: panel insert order is by "source:board" key)
    assert [p["board"] for p in q(pg, "SELECT board FROM panel ORDER BY board")] == [
        "anthropic", "palantir", "ramp"
    ]
    meta = q(pg, "SELECT value FROM schema_meta WHERE key='last_ingested_attempt'")
    assert meta[0]["value"] == m.attempt_id


def test_presence_extends_then_splits(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store)
    same = board_payload("greenhouse", [gh_record(1, "A", "<p>a</p>")])
    changed = board_payload("greenhouse", [gh_record(1, "A2", "<p>a</p>")])
    for n, body in enumerate([same, same, changed, changed]):
        ing.ingest(
            make_manifest(store, "greenhouse", "anthropic", day(n), body, registry_revision=rev)
        )
    pg.commit()
    rows = q(pg, "SELECT runs, first_at, last_at FROM presence ORDER BY first_at")
    assert [r["runs"] for r in rows] == [2, 2]
    assert rows[0]["first_at"] == day(0) and rows[0]["last_at"] == day(1)
    assert rows[1]["first_at"] == day(2) and rows[1]["last_at"] == day(3)
    assert q(pg, "SELECT count(*) AS n FROM posting_versions")[0]["n"] == 2


def test_gap_after_error_attempt_starts_new_interval(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store)
    body = board_payload("ashby", [ab_record("x", "T", "<p>t</p>")])
    ing.ingest(make_manifest(store, "ashby", "ramp", day(0), body, registry_revision=rev))
    ing.ingest(make_manifest(store, "ashby", "ramp", day(1), None, transport="timeout",
                             http_status=None, registry_revision=rev))
    ing.ingest(make_manifest(store, "ashby", "ramp", day(2), body, registry_revision=rev))
    pg.commit()
    assert [r["runs"] for r in q(pg, "SELECT runs FROM presence ORDER BY first_at")] == [1, 1]
    assert q(pg, "SELECT health FROM fetch_attempts ORDER BY started_at")[1]["health"] == "error"


def test_failed_record_is_present_without_version(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    recs = [
        gh_record(1, "A", "<p>a</p>"), {"id": 2, "content": "&lt;p&gt;no title&lt;/p&gt;"}, "junk"
    ]
    m = make_manifest(store, "greenhouse", "anthropic", day(0), board_payload("greenhouse", recs),
                      registry_revision=rev)
    r = Ingestor(pg, store).ingest(m)
    pg.commit()
    assert r is not None
    assert (r.observed_count, r.parsed_count, r.failed_count, r.unidentifiable_count) == (
        2, 1, 1, 1,
    )
    pres = {p["uid"]: p for p in q(pg, "SELECT uid, parse_status, version_hash FROM presence")}
    assert pres["gh:anthropic:2"]["parse_status"] == "failed"
    assert pres["gh:anthropic:2"]["version_hash"] is None
    assert pres["gh:anthropic:1"]["parse_status"] == "ok"


def test_duplicate_ids_in_payload_are_counted(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    recs = [gh_record(1, "A", "<p>a</p>"), gh_record(1, "A dup", "<p>a</p>")]
    m = make_manifest(store, "greenhouse", "anthropic", day(0), board_payload("greenhouse", recs),
                      registry_revision=rev)
    r = Ingestor(pg, store).ingest(m)
    pg.commit()
    assert r is not None and r.observed_count == 1
    assert q(pg, "SELECT warnings FROM fetch_attempts")[0]["warnings"] == {"duplicate_ids": 1}


def test_envelope_error_is_health_error(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    m = make_manifest(store, "lever", "palantir", day(0), b"<html>", registry_revision=rev)
    r = Ingestor(pg, store).ingest(m)
    pg.commit()
    assert r is not None and r.health == "error" and r.observed_count == 0
    assert q(pg, "SELECT error FROM fetch_attempts")[0]["error"].startswith("envelope")
    assert q(pg, "SELECT count(*) AS n FROM presence")[0]["n"] == 0


def test_drop_guard(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store, drop_ratio=0.5)
    four = board_payload("lever", [lv_record(str(i), f"T{i}", "<p>x</p>") for i in range(4)])
    two = board_payload("lever", [lv_record(str(i), f"T{i}", "<p>x</p>") for i in range(2)])
    one = board_payload("lever", [lv_record("0", "T0", "<p>x</p>")])
    empty = board_payload("lever", [])
    healths = []
    for n, body in enumerate([four, two, one, empty, empty]):
        r = ing.ingest(
            make_manifest(store, "lever", "palantir", day(n), body, registry_revision=rev)
        )
        assert r is not None
        healths.append((r.health, r.observed_count))
    pg.commit()
    # 4 -> 2: 2 < 0.5*4 is False -> ok ; 2 -> 1: 1 < 1 False -> ok ; 1 -> 0: suspect ; 0 -> 0: ok
    assert healths == [("ok", 4), ("ok", 2), ("ok", 1), ("suspect_drop", 0), ("ok", 0)]
    prevs = [
        a["prev_observed_count"]
        for a in q(pg, "SELECT prev_observed_count FROM fetch_attempts ORDER BY started_at")
    ]
    assert prevs == [None, 4, 2, 1, 0]


def test_idempotent_and_out_of_order(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store)
    body = board_payload("ashby", [ab_record("x", "T", "<p>t</p>")])
    m1 = make_manifest(store, "ashby", "ramp", day(1), body, registry_revision=rev)
    assert ing.ingest(m1) is not None
    assert ing.ingest(m1) is None
    m0 = make_manifest(store, "ashby", "ramp", day(0), body, registry_revision=rev)
    with pytest.raises(OutOfOrder):
        ing.ingest(m0)
    pg.commit()
    assert q(pg, "SELECT count(*) AS n FROM fetch_attempts")[0]["n"] == 1


def _events(pg: psycopg.Connection[dict[str, Any]]) -> list[tuple[str, str]]:
    rows = q(pg, "SELECT kind, uid FROM posting_events ORDER BY event_id")
    return [(e["kind"], e["uid"]) for e in rows]


def test_open_change_close_reopen(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store)
    v1 = board_payload("ashby", [ab_record("x", "T", "<p>t</p>"), ab_record("y", "U", "<p>u</p>")])
    v2 = board_payload(
        "ashby", [ab_record("x", "T v2", "<p>t</p>"), ab_record("y", "U", "<p>u</p>")]
    )
    v3 = board_payload("ashby", [ab_record("x", "T v2", "<p>t</p>")])
    v4 = board_payload(
        "ashby", [ab_record("x", "T v2", "<p>t</p>"), ab_record("y", "U v2", "<p>u</p>")]
    )
    results = []
    for n, body in enumerate([v1, v2, v3, v4]):
        r = ing.ingest(make_manifest(store, "ashby", "ramp", day(n), body, registry_revision=rev))
        assert r is not None
        results.append((r.opened, r.changed, r.closed, r.reopened))
    pg.commit()
    assert results == [(2, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)]
    assert _events(pg) == [
        ("opened", "ab:ramp:x"), ("opened", "ab:ramp:y"),
        ("changed", "ab:ramp:x"), ("closed", "ab:ramp:y"), ("reopened", "ab:ramp:y"),
    ]
    x = q(pg, "SELECT * FROM postings WHERE uid = 'ab:ramp:x'")[0]
    assert x["status"] == "open" and x["version_count"] == 2 and x["reopen_count"] == 0
    assert x["first_seen_at"] == day(0) and x["last_seen_at"] == day(3)
    y = q(pg, "SELECT * FROM postings WHERE uid = 'ab:ramp:y'")[0]
    assert y["status"] == "open" and y["reopen_count"] == 1 and y["version_count"] == 2
    assert y["closed_lower_at"] is None and y["closed_by_attempt"] is None
    closed = q(pg, "SELECT * FROM posting_events WHERE kind = 'closed'")[0]
    assert closed["closed_lower_at"] == day(1) and closed["closed_upper_at"] == day(2)
    reopened = q(pg, "SELECT * FROM posting_events WHERE kind = 'reopened'")[0]
    assert reopened["from_version"] != reopened["to_version"]
    changed = q(pg, "SELECT * FROM posting_events WHERE kind = 'changed'")[0]
    assert changed["from_version"] and changed["to_version"] and changed["at"] == day(1)


def test_suspect_drop_defers_closures_and_1_to_0_closes_next_run(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store)
    one = board_payload("lever", [lv_record("a", "A", "<p>a</p>")])
    empty = board_payload("lever", [])
    r0 = ing.ingest(make_manifest(store, "lever", "palantir", day(0), one, registry_revision=rev))
    r1 = ing.ingest(make_manifest(store, "lever", "palantir", day(1), empty, registry_revision=rev))
    r2 = ing.ingest(make_manifest(store, "lever", "palantir", day(2), empty, registry_revision=rev))
    pg.commit()
    assert r0 and r1 and r2
    assert (r1.health, r1.closed) == ("suspect_drop", 0)
    assert (r2.health, r2.closed) == ("ok", 1)
    p = q(pg, "SELECT * FROM postings")[0]
    assert p["status"] == "closed" and p["closed_lower_at"] == day(0)
    assert p["closed_upper_at"] == day(2)
    assert p["closed_by_attempt"] == r2.attempt_id


def test_partial_payload_defers_then_closes_with_true_lower_bound(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store)
    ids = [str(i) for i in range(10)]
    full = board_payload("lever", [lv_record(i, "T", "<p>x</p>") for i in ids])
    third = board_payload("lever", [lv_record(i, "T", "<p>x</p>") for i in ids[:3]])
    ing.ingest(make_manifest(store, "lever", "palantir", day(0), full, registry_revision=rev))
    r1 = ing.ingest(make_manifest(store, "lever", "palantir", day(1), third, registry_revision=rev))
    r2 = ing.ingest(make_manifest(store, "lever", "palantir", day(2), full, registry_revision=rev))
    pg.commit()
    assert r1 and r1.health == "suspect_drop" and r1.closed == 0
    assert r2 and r2.health == "ok" and r2.closed == 0 and r2.reopened == 0
    assert q(pg, "SELECT count(*) AS n FROM postings WHERE status = 'open'")[0]["n"] == 10


def test_failed_parse_keeps_posting_open(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store)
    good = board_payload("greenhouse", [gh_record(1, "A", "<p>a</p>")])
    broken = board_payload("greenhouse", [{"id": 1, "content": "x"}])  # no title -> NormalizeError
    ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(0), good, registry_revision=rev))
    r = ing.ingest(
        make_manifest(store, "greenhouse", "anthropic", day(1), broken, registry_revision=rev)
    )
    pg.commit()
    assert r and r.closed == 0 and r.failed_count == 1
    p = q(pg, "SELECT status, last_seen_at, current_version_hash FROM postings")[0]
    assert p["status"] == "open" and p["last_seen_at"] == day(1) and p["current_version_hash"]


def test_error_attempt_touches_nothing(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store)
    good = board_payload("greenhouse", [gh_record(1, "A", "<p>a</p>")])
    ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(0), good, registry_revision=rev))
    r = ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(1), None,
                                 transport="timeout", http_status=None,
                                 registry_revision=rev))
    pg.commit()
    assert r and r.health == "error"
    p = q(pg, "SELECT status, last_seen_at FROM postings")[0]
    assert p["status"] == "open" and p["last_seen_at"] == day(0)
    runs = q(pg, "SELECT boards_total, boards_ok, boards_error FROM runs")[0]
    assert (runs["boards_total"], runs["boards_ok"], runs["boards_error"]) == (2, 1, 1)


def test_source_updated_at_is_refreshed(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store)
    a = board_payload(
        "greenhouse", [gh_record(1, "A", "<p>a</p>", updated_at="2026-08-01T00:00:00Z")]
    )
    b = board_payload(
        "greenhouse", [gh_record(1, "A", "<p>a</p>", updated_at="2026-08-09T00:00:00Z")]
    )
    ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(0), a, registry_revision=rev))
    r = ing.ingest(
        make_manifest(store, "greenhouse", "anthropic", day(1), b, registry_revision=rev)
    )
    pg.commit()
    assert r and r.changed == 0  # updated_at is metadata, not identity
    updated = q(pg, "SELECT source_updated_at FROM postings")[0]["source_updated_at"]
    assert updated == datetime(2026, 8, 9, tzinfo=UTC)
    assert q(pg, "SELECT count(*) AS n FROM posting_versions")[0]["n"] == 1


def test_two_postings_with_identical_content_keep_two_version_rows(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    same = "<p>same body</p>"
    body = board_payload("greenhouse", [gh_record(1, "Same", same), gh_record(2, "Same", same)])
    r = Ingestor(pg, store).ingest(make_manifest(store, "greenhouse", "anthropic", day(0), body,
                                                 registry_revision=rev))
    pg.commit()
    assert r is not None and r.new_versions == 2 and r.new_documents == 1
    vs = q(pg, "SELECT uid, version_hash, url FROM posting_versions ORDER BY uid")
    assert [v["uid"] for v in vs] == ["gh:anthropic:1", "gh:anthropic:2"]
    assert vs[0]["version_hash"] == vs[1]["version_hash"]  # content identity is shared
    assert vs[0]["url"] != vs[1]["url"]  # but each posting keeps its own row
    assert q(pg, "SELECT count(*) AS n FROM documents")[0]["n"] == 1  # one canonical text


def test_versions_differing_only_in_metadata_each_get_a_document(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    body = board_payload("greenhouse", [
        gh_record(1, "T", "<p>same body</p>", location={"name": "SF"}),
        gh_record(2, "T", "<p>same body</p>", location={"name": "NYC"}),
    ])
    r = Ingestor(pg, store).ingest(make_manifest(store, "greenhouse", "anthropic", day(0), body,
                                                 registry_revision=rev))
    pg.commit()
    assert r is not None and r.new_versions == 2 and r.new_documents == 2
    docs = q(pg, "SELECT version_hash, document_hash FROM documents")
    assert len(docs) == 2 and docs[0]["document_hash"] == docs[1]["document_hash"]
    joined = q(pg, "SELECT count(*) AS n FROM posting_versions v JOIN documents d "
                   "ON d.version_hash = v.version_hash AND d.normalizer_version = 'md/1'")
    assert joined[0]["n"] == 2  # no version is left without a document


def test_document_conversion_is_skipped_when_document_exists(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    from jobhunter.markdown import to_markdown as real_to_markdown

    calls = {"n": 0}

    def counting(html: str) -> str:
        calls["n"] += 1
        return real_to_markdown(html)

    body = board_payload("greenhouse", [gh_record(1, "A", "<p>a</p>")])
    ing = Ingestor(pg, store, to_markdown=counting)
    ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(0), body, registry_revision=rev))
    assert calls["n"] == 1
    ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(1), body, registry_revision=rev))
    pg.commit()
    assert calls["n"] == 1  # unchanged posting: no reconversion on later attempts


def test_run_id_index_exists(pg: psycopg.Connection[dict[str, Any]]) -> None:
    row = pg.execute(
        "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() "
        "AND indexname = 'ix_attempts_run'"
    ).fetchone()
    assert row is not None


def test_registry_watermark_not_set_when_snapshot_missing(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS
) -> None:
    body = board_payload("ashby", [ab_record("x", "T", "<p>t</p>")])
    ing = Ingestor(pg, store)
    # revision whose snapshot object does not exist in the archive
    ing.ingest(make_manifest(store, "ashby", "ramp", day(0), body, registry_revision="missing"))
    assert q(pg, "SELECT count(*) AS n FROM panel")[0]["n"] == 0
    # the snapshot appears later under the same revision; the next attempt must apply it
    from jobhunter.registry import Registry

    reg = Registry(boards=(BOARDS[2],), revision="")
    from jobhunter.archive.keys import registry_key

    store.put(registry_key("missing"), reg.snapshot_json())
    ing2 = Ingestor(pg, store)
    ing2.ingest(make_manifest(store, "ashby", "ramp", day(1), body, registry_revision="missing"))
    pg.commit()
    assert q(pg, "SELECT count(*) AS n FROM panel")[0]["n"] == 1


# ---- two-phase boards (list + detail), spec 2026-09-04 §3.4 ------------------

WD_BOARD = Board("NVIDIA", "workday", "nvidia")


@pytest.fixture
def wd(monkeypatch: pytest.MonkeyPatch, store: LocalFS) -> str:
    """Register the fake list+detail adapter and return the registry revision naming it."""
    monkeypatch.setitem(sources.TWO_PHASE_SOURCES, "workday", FakeTwoPhase())
    monkeypatch.setitem(SOURCE_PREFIX, "workday", "wd")
    return write_registry(store, [*BOARDS, WD_BOARD])


def _presence(pg: psycopg.Connection[dict[str, Any]]) -> list[dict[str, Any]]:
    return q(
        pg,
        "SELECT uid, version_hash, parse_status, runs, first_at, last_at FROM presence "
        "ORDER BY uid, first_at",
    )


def test_two_phase_list_only_opens_postings_pending_detail(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, wd: str
) -> None:
    """The list alone establishes presence: a uid never seen before becomes an open posting
    with no version, no document, and presence parse_status pending_detail."""
    m = make_two_phase_manifest(
        store, "nvidia", day(0), [wd_row("j0"), wd_row("j1")], registry_revision=wd
    )
    r = Ingestor(pg, store).ingest(m)
    pg.commit()
    assert r is not None and r.health == "ok"
    assert (r.observed_count, r.parsed_count, r.pending_count, r.failed_count) == (2, 0, 2, 0)
    assert (r.opened, r.changed, r.closed) == (2, 0, 0)
    assert q(pg, "SELECT blob_sha256 FROM fetch_attempts")[0]["blob_sha256"] is None
    ps = q(pg, "SELECT * FROM postings ORDER BY uid")
    assert [p["uid"] for p in ps] == ["wd:nvidia:j0", "wd:nvidia:j1"]
    assert all(p["status"] == "open" for p in ps)
    assert all(p["current_version_hash"] is None and p["version_count"] == 0 for p in ps)
    assert all(p["source_id"] == p["uid"].rsplit(":", 1)[1] for p in ps)
    pres = _presence(pg)
    assert all(p["parse_status"] == PENDING_DETAIL and p["version_hash"] is None for p in pres)
    assert q(pg, "SELECT count(*) AS n FROM posting_versions")[0]["n"] == 0
    assert q(pg, "SELECT count(*) AS n FROM documents")[0]["n"] == 0
    assert _events(pg) == [("opened", "wd:nvidia:j0"), ("opened", "wd:nvidia:j1")]


def test_two_phase_pending_detail_presence_extends_while_the_detail_is_owed(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, wd: str
) -> None:
    """A uid listed run after run with no detail yet keeps one presence interval, so
    pending_detail does not churn the timeline while the budget works through the board."""
    ing = Ingestor(pg, store)
    for n in range(3):
        ing.ingest(make_two_phase_manifest(store, "nvidia", day(n), [wd_row("j0")],
                                           registry_revision=wd))
    pg.commit()
    pres = _presence(pg)
    assert len(pres) == 1
    assert pres[0]["parse_status"] == PENDING_DETAIL and pres[0]["runs"] == 3
    assert pres[0]["first_at"] == day(0) and pres[0]["last_at"] == day(2)


def test_two_phase_version_on_detail_flips_the_pending_posting(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, wd: str
) -> None:
    """A fetched detail creates the version+document exactly like a single-phase record and
    flips current_version_hash; the uid whose detail is still owed stays pending."""
    ing = Ingestor(pg, store)
    rows = [wd_row("j0"), wd_row("j1")]
    ing.ingest(make_two_phase_manifest(store, "nvidia", day(0), rows, registry_revision=wd))
    r = ing.ingest(make_two_phase_manifest(
        store, "nvidia", day(1), rows, {"j0": wd_detail("<p>hello</p>")}, registry_revision=wd
    ))
    pg.commit()
    assert r is not None and r.health == "ok"
    assert (r.observed_count, r.parsed_count, r.pending_count) == (2, 1, 1)
    assert (r.new_versions, r.new_documents, r.changed, r.opened) == (1, 1, 1, 0)
    v = q(pg, "SELECT * FROM posting_versions")[0]
    assert v["uid"] == "wd:nvidia:j0" and v["title"] == "Engineer j0" and v["company"] == "NVIDIA"
    assert store.exists(version_key(v["version_hash"]))
    docs = q(pg, "SELECT version_hash, normalizer_version, markdown FROM documents")
    assert len(docs) == 1 and docs[0]["markdown"] == "hello"
    assert docs[0]["version_hash"] == v["version_hash"] and docs[0]["normalizer_version"] == "md/1"
    ps = {p["uid"]: p for p in q(pg, "SELECT * FROM postings")}
    assert ps["wd:nvidia:j0"]["current_version_hash"] == v["version_hash"]
    assert ps["wd:nvidia:j0"]["version_count"] == 1
    assert ps["wd:nvidia:j1"]["current_version_hash"] is None
    pres = {(p["uid"], p["first_at"]): p for p in _presence(pg)}
    assert pres[("wd:nvidia:j0", day(0))]["parse_status"] == PENDING_DETAIL
    landed = pres[("wd:nvidia:j0", day(1))]
    assert landed["parse_status"] == "ok" and landed["version_hash"] == v["version_hash"]
    assert pres[("wd:nvidia:j1", day(0))]["runs"] == 2  # still one pending interval
    changed = q(pg, "SELECT * FROM posting_events WHERE kind = 'changed'")[0]
    assert changed["from_version"] is None and changed["to_version"] == v["version_hash"]


def test_two_phase_re_ingesting_the_same_detail_is_a_no_op(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, wd: str
) -> None:
    """Identity is the version_hash, as for every other version: the same detail body on a
    later attempt writes no version, no document, no event — the posting is only touched."""
    ing = Ingestor(pg, store)
    rows = [wd_row("j0")]
    detail = {"j0": wd_detail("<p>hello</p>")}
    ing.ingest(make_two_phase_manifest(store, "nvidia", day(0), rows, detail,
                                       registry_revision=wd))
    r = ing.ingest(make_two_phase_manifest(store, "nvidia", day(1), rows, detail,
                                           registry_revision=wd))
    pg.commit()
    assert r is not None
    assert (r.new_versions, r.new_documents, r.changed, r.opened, r.closed) == (0, 0, 0, 0, 0)
    assert q(pg, "SELECT count(*) AS n FROM posting_versions")[0]["n"] == 1
    assert q(pg, "SELECT count(*) AS n FROM documents")[0]["n"] == 1
    p = q(pg, "SELECT * FROM postings")[0]
    assert p["version_count"] == 1 and p["last_seen_at"] == day(1)
    pres = _presence(pg)
    assert len(pres) == 1 and pres[0]["runs"] == 2 and pres[0]["parse_status"] == "ok"
    assert _events(pg) == [("opened", "wd:nvidia:j0")]
    # a genuinely edited detail on the next attempt is a new version, as usual
    r2 = ing.ingest(make_two_phase_manifest(store, "nvidia", day(2), rows,
                                            {"j0": wd_detail("<p>hello again</p>")},
                                            registry_revision=wd))
    pg.commit()
    assert r2 is not None and r2.new_versions == 1 and r2.changed == 1


def test_two_phase_uid_absent_from_the_next_list_closes(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, wd: str
) -> None:
    """The list is the presence snapshot, so absence closes exactly as on a single-phase
    board — interval-censored, and for a pending posting too."""
    ing = Ingestor(pg, store)
    detail = {"j0": wd_detail("<p>a</p>"), "j1": wd_detail("<p>b</p>")}
    ing.ingest(make_two_phase_manifest(
        store, "nvidia", day(0), [wd_row("j0"), wd_row("j1"), wd_row("j2"), wd_row("j3")],
        detail, registry_revision=wd,
    ))
    r = ing.ingest(make_two_phase_manifest(
        store, "nvidia", day(1), [wd_row("j0"), wd_row("j2")], registry_revision=wd
    ))
    pg.commit()
    assert r is not None and r.health == "ok" and r.closed == 2
    closed = {p["uid"]: p for p in q(pg, "SELECT * FROM postings WHERE status = 'closed'")}
    assert sorted(closed) == ["wd:nvidia:j1", "wd:nvidia:j3"]  # a versioned and a pending one
    assert all(p["closed_lower_at"] == day(0) and p["closed_upper_at"] == day(1)
               for p in closed.values())
    assert closed["wd:nvidia:j3"]["current_version_hash"] is None
    assert [e for e in _events(pg) if e[0] == "closed"] == [
        ("closed", "wd:nvidia:j1"), ("closed", "wd:nvidia:j3")
    ]
    # and it reopens on the next list that names it again
    r2 = ing.ingest(make_two_phase_manifest(
        store, "nvidia", day(2), [wd_row("j0"), wd_row("j2"), wd_row("j3")], registry_revision=wd
    ))
    pg.commit()
    assert r2 is not None and r2.reopened == 1
    j3 = q(pg, "SELECT * FROM postings WHERE uid = 'wd:nvidia:j3'")[0]
    assert j3["status"] == "open" and j3["current_version_hash"] is None


def test_two_phase_truncated_list_is_an_error_attempt_that_closes_nothing(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, wd: str
) -> None:
    """A list that stopped at the page cap (or failed to parse) is not a snapshot: using it
    would close every posting it never reached."""
    ing = Ingestor(pg, store)
    ing.ingest(make_two_phase_manifest(store, "nvidia", day(0), [wd_row("j0"), wd_row("j1")],
                                       registry_revision=wd))
    r = ing.ingest(make_two_phase_manifest(store, "nvidia", day(1), [wd_row("j0")],
                                           registry_revision=wd,
                                           error="page cap: stopped after 3 pages"))
    pg.commit()
    assert r is not None and r.health == "error" and r.closed == 0
    assert q(pg, "SELECT count(*) AS n FROM postings WHERE status = 'open'")[0]["n"] == 2
    att = q(pg, "SELECT error FROM fetch_attempts ORDER BY started_at")[1]
    assert att["error"].startswith("page cap")
    assert all(p["last_at"] == day(0) for p in _presence(pg))


def test_two_phase_detail_that_fails_to_normalise_is_present_without_a_version(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, wd: str
) -> None:
    ing = Ingestor(pg, store)
    r = ing.ingest(make_two_phase_manifest(
        store, "nvidia", day(0), [wd_row("j0", title=""), wd_row("j1")],
        {"j0": wd_detail("<p>a</p>")}, failed_details={"j1": "HTTP 500"}, registry_revision=wd,
    ))
    pg.commit()
    assert r is not None
    assert (r.observed_count, r.parsed_count, r.failed_count, r.pending_count) == (2, 0, 1, 1)
    pres = {p["uid"]: p for p in _presence(pg)}
    assert pres["wd:nvidia:j0"]["parse_status"] == "failed"          # detail body, unusable
    assert pres["wd:nvidia:j1"]["parse_status"] == PENDING_DETAIL    # detail fetch failed
    assert q(pg, "SELECT count(*) AS n FROM posting_versions")[0]["n"] == 0
    assert q(pg, "SELECT count(*) AS n FROM postings WHERE status='open'")[0]["n"] == 2


def test_two_phase_pending_detail_posting_never_enters_the_l2_queue(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, wd: str
) -> None:
    """The queue keys on documents; a posting with no version has none to offer."""
    Ingestor(pg, store).ingest(make_two_phase_manifest(
        store, "nvidia", day(0), [wd_row("j0"), wd_row("j1")], {"j0": wd_detail("<p>a</p>")},
        registry_revision=wd,
    ))
    pg.commit()
    queued = extraction.queue(
        pg, prompt_version="p", schema_version="1", validator_version="1",
        model_regex=".*", normalizer_version="md/1", limit=10,
    )
    docs = q(pg, "SELECT d.document_hash FROM documents d JOIN posting_versions v "
                 "ON v.version_hash = d.version_hash WHERE v.uid = 'wd:nvidia:j0'")
    assert queued == [docs[0]["document_hash"]]  # j1 is pending_detail: not in the queue


def test_two_phase_rebuild_replays_list_presence_and_versions(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, wd: str
) -> None:
    """Both paths replay from the archive alone: the store after a rebuild is row for row
    the store the incremental ingest built."""
    from jobhunter.rebuild import rebuild
    from tests.conftest import TEST_DSN

    ing = Ingestor(pg, store)
    ing.ingest(make_two_phase_manifest(store, "nvidia", day(0),
                                       [wd_row("j0"), wd_row("j1"), wd_row("j2")],
                                       registry_revision=wd))
    ing.ingest(make_two_phase_manifest(store, "nvidia", day(1),
                                       [wd_row("j0"), wd_row("j1"), wd_row("j2")],
                                       {"j0": wd_detail("<p>a</p>")}, registry_revision=wd))
    ing.ingest(make_two_phase_manifest(store, "nvidia", day(2), [wd_row("j0"), wd_row("j1")],
                                       {"j1": wd_detail("<p>b</p>")}, registry_revision=wd))
    pg.commit()
    before = {
        "postings": q(pg, "SELECT uid, status, current_version_hash, version_count, "
                          "closed_lower_at, closed_upper_at FROM postings ORDER BY uid"),
        "presence": _presence(pg),
        "versions": q(pg, "SELECT uid, version_hash FROM posting_versions ORDER BY uid"),
        "documents": q(pg, "SELECT version_hash, markdown FROM documents ORDER BY markdown"),
        "events": _events(pg),
    }
    assert [p["parse_status"] for p in before["presence"]].count(PENDING_DETAIL) >= 2

    row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert row is not None
    target = str(row["s"])
    pg.commit()  # the swap renames the live schema: no reader may still hold it
    s = rebuild(store, TEST_DSN, schema=target, work_schema=f"{target}_new")
    assert s.swapped and s.ingested == 3
    check = db_connect(TEST_DSN, target)
    try:
        after = {
            "postings": q(check, "SELECT uid, status, current_version_hash, version_count, "
                                 "closed_lower_at, closed_upper_at FROM postings ORDER BY uid"),
            "presence": _presence(check),
            "versions": q(check, "SELECT uid, version_hash FROM posting_versions ORDER BY uid"),
            "documents": q(check, "SELECT version_hash, markdown FROM documents ORDER BY markdown"),
            "events": _events(check),
        }
        assert after == before
        check.execute(f'DROP SCHEMA "{target}_previous" CASCADE')
        check.commit()
    finally:
        check.close()


def db_connect(dsn: str, schema: str) -> psycopg.Connection[dict[str, Any]]:
    from jobhunter.store import db

    return db.connect(dsn, schema=schema)


class _CountingConn:
    """The real connection, plus a tally of the statements an ingest issues through it."""

    def __init__(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        self._conn = conn
        self.statements: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def execute(self, query: Any, params: Any = None, **kw: Any) -> Any:
        self.statements.append(" ".join(str(query).split())[:80])
        return self._conn.execute(query, params, **kw)

    def executemany(self, query: Any, params_seq: Any, **kw: Any) -> Any:
        self.statements.append(" ".join(str(query).split())[:80])
        return self._conn.executemany(query, params_seq, **kw)

    def reset(self) -> None:
        self.statements.clear()


def test_attempt_costs_a_bounded_number_of_statements(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    """Statements per attempt must not grow with the record count (a round trip is ~1 ms
    locally and ~10 ms to Neon; 918 records once cost 16.5 minutes)."""
    conn = _CountingConn(pg)
    ing = Ingestor(conn, store)  # type: ignore[arg-type]
    # Warm-up on another board: the panel snapshot and its watermark are then already applied,
    # so what is measured below is the record path alone.
    ing.ingest(make_manifest(store, "lever", "palantir", day(0), board_payload("lever", []),
                             registry_revision=rev))
    hundred = [gh_record(i, f"T{i}", f"<p>body {i}</p>") for i in range(100)]
    edited = [gh_record(i, f"T{i} v2", f"<p>body {i}</p>") for i in range(100)]
    costs = {}
    for n, (label, recs) in enumerate(
        [("new", hundred), ("unchanged", hundred), ("changed", edited)], start=1
    ):
        conn.reset()
        r = ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(n),
                                     board_payload("greenhouse", recs), registry_revision=rev))
        assert r is not None and r.health == "ok" and r.observed_count == 100
        costs[label] = (len(conn.statements), list(conn.statements))
    pg.commit()
    assert q(pg, "SELECT count(*) AS n FROM postings WHERE status = 'open'")[0]["n"] == 100
    assert q(pg, "SELECT count(*) AS n FROM posting_versions")[0]["n"] == 200
    for label, (n, issued) in costs.items():
        assert n <= 25, f"{label} attempt of 100 records issued {n} statements: {issued}"


class _RecordingStore:
    """LocalFS, plus a record of the version-HTML puts and proof they run concurrently."""

    def __init__(self, inner: LocalFS) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._barrier: threading.Barrier | None = None
        self.version_puts: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def expect_parallel(self, n: int) -> None:
        """Make the next n version puts block until all n are in flight (0 disarms)."""
        self._barrier = threading.Barrier(n, timeout=10) if n else None

    def put(self, key: str, data: bytes) -> bool:
        if key.startswith("versions/"):
            with self._lock:
                self.version_puts.append(key)
            if self._barrier is not None:
                self._barrier.wait()  # BrokenBarrierError if the puts are serialised
        return self._inner.put(key, data)


def test_version_html_is_put_once_per_globally_new_hash_and_in_parallel(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    rec = _RecordingStore(store)
    # ids 4 and 5 differ only in id/url, which are not part of the version identity
    five = [gh_record(1, "A", "<p>a</p>"), gh_record(2, "B", "<p>b</p>"),
            gh_record(3, "C", "<p>c</p>"), gh_record(4, "D", "<p>d</p>"),
            gh_record(5, "D", "<p>d</p>")]
    ing = Ingestor(pg, rec)  # type: ignore[arg-type]
    rec.expect_parallel(4)  # the four globally-new hashes must be in flight together
    r0 = ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(0),
                                  board_payload("greenhouse", five), registry_revision=rev))
    rec.expect_parallel(0)
    assert r0 is not None and r0.new_versions == 5 and r0.new_documents == 4
    hashes = {v["version_hash"] for v in q(pg, "SELECT version_hash FROM posting_versions")}
    assert len(hashes) == 4
    assert sorted(rec.version_puts) == sorted(version_key(h) for h in hashes)

    # nothing new: no put at all, not even an existence check
    rec.version_puts.clear()
    r1 = ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(1),
                                  board_payload("greenhouse", five), registry_revision=rev))
    assert r1 is not None and r1.new_versions == 0
    assert rec.version_puts == []

    # one edited record: exactly one put, for its hash alone
    edited = [*five[:4], gh_record(5, "D2", "<p>d</p>")]
    r2 = ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(2),
                                  board_payload("greenhouse", edited), registry_revision=rev))
    pg.commit()
    assert r2 is not None and r2.new_versions == 1 and r2.changed == 1
    new_hash = q(pg, "SELECT current_version_hash AS h FROM postings WHERE uid='gh:anthropic:5'")
    assert rec.version_puts == [version_key(new_hash[0]["h"])]


def test_event_order_is_payload_order_then_closed_events_by_uid(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    """Batched writes must not reorder events: event_id order is part of the store contract
    (a rebuild has to reproduce it row for row)."""
    ing = Ingestor(pg, store)
    def gh(i: int, title: str) -> dict[str, Any]:
        return gh_record(i, title, f"<p>body {i}</p>")

    d0 = [gh(3, "C"), gh(1, "A"), gh(2, "B"), gh(5, "E"), gh(4, "D")]
    d1 = [gh(5, "E2"), gh(3, "C"), gh(1, "A2")]          # 2 and 4 disappear -> closed
    d2 = [gh(1, "A2"), gh(5, "E2"), gh(4, "D"), gh(3, "C"), gh(2, "B")]  # 4 and 2 come back
    for n, recs in enumerate([d0, d1, d2]):
        r = ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(n),
                                     board_payload("greenhouse", recs), registry_revision=rev))
        assert r is not None and r.health == "ok"
    pg.commit()
    u = "gh:anthropic:"
    assert _events(pg) == [
        ("opened", f"{u}3"), ("opened", f"{u}1"), ("opened", f"{u}2"), ("opened", f"{u}5"),
        ("opened", f"{u}4"),
        ("changed", f"{u}5"), ("changed", f"{u}1"),      # payload order, not uid order
        ("closed", f"{u}2"), ("closed", f"{u}4"),        # reconcile closes in uid order
        ("reopened", f"{u}4"), ("reopened", f"{u}2"),    # payload order again
    ]
