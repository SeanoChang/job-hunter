"""Spec §9 integration: four scripted days through fetch.run, then rebuild == incremental."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg

from jobhunter.archive.local import LocalFS
from jobhunter.config import Settings
from jobhunter.fetch import RunSummary, run
from jobhunter.http import Fetcher
from jobhunter.rebuild import rebuild
from jobhunter.store import db
from tests.conftest import TEST_DSN
from tests.store.helpers import ab_record, board_payload, gh_record, lv_record

REG = """
[[boards]]
company="Anthropic"
source="greenhouse"
board="anthropic"
[[boards]]
company="Palantir"
source="lever"
board="palantir"
[[boards]]
company="Ramp"
source="ashby"
board="ramp"
"""

T0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
GH = [gh_record(i, f"GH {i}", f"<p>gh {i}</p>") for i in range(4)]
LV = [lv_record(f"l{i}", f"LV {i}", f"<p>lv {i}</p>") for i in range(4)]
AB = [ab_record(f"a{i}", f"AB {i}", f"<p>ab {i}</p>") for i in range(2)]

DAYS: dict[int, dict[str, bytes | None]] = {
    0: {"greenhouse": board_payload("greenhouse", GH), "lever": board_payload("lever", LV),
        "ashby": board_payload("ashby", AB)},
    # day 1: GH edits one posting; Lever drops one (4->3 is not a >50% drop -> closes);
    # Ashby unchanged
    1: {"greenhouse": board_payload("greenhouse",
                                    [gh_record(0, "GH 0 edited", "<p>gh 0</p>"), *GH[1:]]),
        "lever": board_payload("lever", LV[:3]), "ashby": board_payload("ashby", AB)},
    # day 2: Lever returns [] (suspect_drop, closes nothing); Ashby returns half
    # (1 of 2 -> not < 50%: closes 1); GH returns 500 (error attempt)
    2: {"greenhouse": None, "lever": board_payload("lever", []),
        "ashby": board_payload("ashby", AB[:1])},
    # day 3: Lever [] again (ok -> closes remaining 3 with lower bound day 1);
    # GH back with 2 (2 < 0.5*4? no: 2 -> ok, closes 2)
    3: {"greenhouse": board_payload("greenhouse", GH[:2]), "lever": board_payload("lever", []),
        "ashby": board_payload("ashby", AB[:1])},
}


def _handler_for(day: int) -> Any:
    def h(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        src = "greenhouse" if "greenhouse" in host else "lever" if "lever" in host else "ashby"
        body = DAYS[day][src]
        if body is None:
            return httpx.Response(500, content=b"down")
        return httpx.Response(200, content=body)
    return h


def _fetcher(day: int) -> Fetcher:
    client = httpx.Client(transport=httpx.MockTransport(_handler_for(day)))
    return Fetcher(client, sleep=lambda s: None)


def _clock(at: datetime) -> Callable[[], datetime]:
    return lambda: at


def _one(conn: psycopg.Connection[dict[str, Any]], sql: str) -> dict[str, Any]:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return row


TABLES = ["fetch_attempts", "posting_versions", "documents", "presence", "runs", "panel",
          "postings", "posting_events", "schema_meta"]


def _dump(
    conn: psycopg.Connection[dict[str, Any]], schema: str
) -> dict[str, list[tuple[Any, ...]]]:
    out: dict[str, list[tuple[Any, ...]]] = {}
    for t in TABLES:
        cols = [r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s "
            "ORDER BY ordinal_position", (schema, t)).fetchall()]
        if t == "schema_meta":
            cols = ["key", "value"]
        rows = conn.execute(f'SELECT {", ".join(cols)} FROM "{schema}".{t}').fetchall()
        out[t] = sorted(tuple(str(r[c]) for c in cols) for r in rows)
    return out


def test_four_days_then_rebuild_matches(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    (tmp_path / "companies.toml").write_text(REG)
    settings = Settings(archive_url=f"file://{tmp_path / 'archive'}",
                        registry_path=tmp_path / "companies.toml",
                        home=tmp_path, database_url=TEST_DSN, drop_ratio=0.5)
    schema = str(_one(pg, "SELECT current_schema() AS s")["s"])
    summaries: list[RunSummary] = []
    for day in range(4):
        t = T0 + timedelta(days=day)
        fetcher = _fetcher(day)
        try:
            summaries.append(run(settings, fetcher=fetcher, now=_clock(t), schema=schema))
        finally:
            fetcher.close()
    assert all(s.db_error is None for s in summaries)

    events = pg.execute("SELECT kind, uid, at FROM posting_events ORDER BY event_id").fetchall()
    kinds = [(e["kind"], e["uid"]) for e in events]
    opened = [("opened", f"gh:anthropic:{i}") for i in range(4)] \
        + [("opened", f"lv:palantir:l{i}") for i in range(4)] \
        + [("opened", "ab:ramp:a0"), ("opened", "ab:ramp:a1")]
    assert kinds[:10] == opened or sorted(kinds[:10]) == sorted(opened)
    day1 = [k for k in kinds[10:] if k[0] in ("changed", "closed")][:2]
    assert ("changed", "gh:anthropic:0") in day1 and ("closed", "lv:palantir:l3") in day1
    healths = {(r["board"], r["started_at"].day - T0.day): r["health"] for r in
               pg.execute("SELECT board, started_at, health FROM fetch_attempts").fetchall()}
    assert healths[("palantir", 2)] == "suspect_drop" and healths[("palantir", 3)] == "ok"
    assert healths[("anthropic", 2)] == "error" and healths[("ramp", 2)] == "ok"
    lever_closed = pg.execute(
        "SELECT uid, closed_lower_at, closed_upper_at FROM postings "
        "WHERE source='lever' AND status='closed' ORDER BY uid"
    ).fetchall()
    assert len(lever_closed) == 4
    l0 = [r for r in lever_closed if r["uid"] == "lv:palantir:l0"][0]
    assert l0["closed_lower_at"] == T0 + timedelta(days=1)
    assert l0["closed_upper_at"] == T0 + timedelta(days=3)
    # gh0, gh1, a0
    assert _one(pg, "SELECT count(*) AS n FROM postings WHERE status='open'")["n"] == 3
    # 4+4+2 + gh0 edit
    assert _one(pg, "SELECT count(*) AS n FROM posting_versions")["n"] == 11
    incremental = _dump(pg, schema)
    # Release this connection's read locks: the swap below renames the schema these rows
    # live in, and the DROP of "<schema>_previous" would otherwise wait on them forever.
    pg.commit()

    work = f"{schema}_new"
    s = rebuild(LocalFS(tmp_path / "archive"), TEST_DSN, drop_ratio=0.5, schema=schema,
                work_schema=work)
    assert s.swapped and s.ingested == 12
    check = db.connect(TEST_DSN, schema=schema)
    try:
        rebuilt = _dump(check, schema)
        check.execute(f'DROP SCHEMA IF EXISTS "{schema}_previous" CASCADE')
        check.commit()
    finally:
        check.close()
    for t in TABLES:
        assert rebuilt[t] == incremental[t], f"table {t} differs after rebuild"
