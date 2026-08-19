from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jobhunter.archive.keys import version_key
from jobhunter.archive.local import LocalFS
from jobhunter.models import Board
from jobhunter.store.lifecycle import Ingestor, OutOfOrder
from tests.store.helpers import (
    ab_record,
    board_payload,
    gh_record,
    lv_record,
    make_manifest,
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
