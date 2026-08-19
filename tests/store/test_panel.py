from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from jobhunter.models import Board
from jobhunter.registry import Registry
from jobhunter.store.panel import apply_snapshot, boards_from_snapshot

B1 = Board("Anthropic", "greenhouse", "anthropic")
B2 = Board("Ramp", "ashby", "ramp", country="US", tags=("fintech",))
B3 = Board("Palantir", "lever", "palantir")


def _rows(conn: psycopg.Connection[dict[str, Any]]) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT source, board, company, added_at, removed_at, registry_revision "
        "FROM panel ORDER BY source, board, added_at"
    ).fetchall()


def test_snapshot_roundtrip() -> None:
    reg = Registry(boards=(B2, B1), revision="r")
    assert boards_from_snapshot(reg.snapshot_json()) == (B2, B1)


def test_add_remove_readd(pg: psycopg.Connection[dict[str, Any]]) -> None:
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    d = apply_snapshot(pg, [B1, B2], t0, "rev1")
    assert sorted(d.added) == ["ashby:ramp", "greenhouse:anthropic"] and d.removed == []
    d = apply_snapshot(pg, [B1, B2], t0 + timedelta(days=1), "rev1")
    assert d.added == [] and d.removed == []  # idempotent
    d = apply_snapshot(pg, [B1, B3], t0 + timedelta(days=2), "rev2")
    assert d.added == ["lever:palantir"] and d.removed == ["ashby:ramp"]
    d = apply_snapshot(pg, [B1, B2, B3], t0 + timedelta(days=3), "rev3")
    assert d.added == ["ashby:ramp"] and d.removed == []
    rows = _rows(pg)
    ramp = [r for r in rows if r["board"] == "ramp"]
    assert len(ramp) == 2
    assert ramp[0]["removed_at"] == t0 + timedelta(days=2)
    assert ramp[0]["registry_revision"] == "rev1"
    assert ramp[1]["removed_at"] is None and ramp[1]["registry_revision"] == "rev3"
    assert [r for r in rows if r["board"] == "anthropic"][0]["removed_at"] is None
