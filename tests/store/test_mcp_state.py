"""The hosted MCP server's watermarks: same semantics as `cursors.py`, stored
in Postgres because a Cloud Run instance has no disk to remember on."""

from typing import Any

import psycopg

from jobhunter.cursors import Watermark
from jobhunter.store import mcp_state

Conn = psycopg.Connection[dict[str, Any]]


def test_unknown_name_reads_as_no_cursor(pg: Conn) -> None:
    assert mcp_state.read_cursor(pg, "hourly") is None


def test_roundtrip_preserves_the_instant_and_the_tie_break_ids(pg: Conn) -> None:
    wm = Watermark(at="2026-09-01T06:00:00+00:00", event_ids_at=(41, 42))
    mcp_state.write_cursor(pg, "hourly", wm)
    pg.commit()
    assert mcp_state.read_cursor(pg, "hourly") == wm


def test_ids_come_back_as_a_tuple(pg: Conn) -> None:
    """psycopg hands back a list; the Watermark contract says tuple."""
    mcp_state.write_cursor(pg, "hourly", Watermark("2026-09-01T06:00:00+00:00", (7,)))
    pg.commit()
    got = mcp_state.read_cursor(pg, "hourly")
    assert got is not None and got.event_ids_at == (7,)
    assert isinstance(got.event_ids_at, tuple)


def test_empty_ids_roundtrip(pg: Conn) -> None:
    mcp_state.write_cursor(pg, "hourly", Watermark("2026-09-01T06:00:00+00:00", ()))
    pg.commit()
    assert mcp_state.read_cursor(pg, "hourly") == Watermark("2026-09-01T06:00:00+00:00", ())


def test_sub_second_precision_survives(pg: Conn) -> None:
    """A second-truncated watermark would never match its own instant again."""
    wm = Watermark("2026-09-01T06:00:00.123456+00:00", (1,))
    mcp_state.write_cursor(pg, "hourly", wm)
    pg.commit()
    assert mcp_state.read_cursor(pg, "hourly") == wm


def test_read_is_utc_whatever_the_session_timezone(pg: Conn) -> None:
    wm = Watermark("2026-09-01T06:00:00+00:00", ())
    mcp_state.write_cursor(pg, "hourly", wm)
    pg.commit()
    pg.execute("SET TIME ZONE 'America/New_York'")
    assert mcp_state.read_cursor(pg, "hourly") == wm


def test_write_upserts_and_leaves_other_names_alone(pg: Conn) -> None:
    mcp_state.write_cursor(pg, "hourly", Watermark("2026-09-01T06:00:00+00:00", (1,)))
    mcp_state.write_cursor(pg, "smoketest", Watermark("2026-09-02T06:00:00+00:00", (2, 3)))
    mcp_state.write_cursor(pg, "hourly", Watermark("2026-09-03T06:00:00+00:00", (9,)))
    pg.commit()
    assert mcp_state.read_cursor(pg, "hourly") == Watermark("2026-09-03T06:00:00+00:00", (9,))
    assert mcp_state.read_cursor(pg, "smoketest") == Watermark(
        "2026-09-02T06:00:00+00:00", (2, 3)
    )


def _schema_of(conn: Conn) -> str:
    row = conn.execute("SELECT current_schema() AS s").fetchone()
    assert row is not None
    return str(row["s"])


def test_carry_cursors_copies_every_watermark_into_the_new_schema(pg: Conn) -> None:
    """`rebuild` replays the archive into a fresh schema; the watermarks are not in
    the archive, so they have to be carried over by hand or they are lost."""
    from jobhunter.store import db

    src = _schema_of(pg)
    dst = f"{src}_new"
    db.init(pg, dst)
    mcp_state.write_cursor(pg, "hourly", Watermark("2026-09-01T06:00:00+00:00", (1, 2)))
    mcp_state.write_cursor(pg, "smoketest", Watermark("2026-09-02T06:00:00+00:00", ()))
    pg.commit()
    assert mcp_state.carry_cursors(pg, src=src, dst=dst) == 2
    pg.commit()
    rows = pg.execute(
        f'SELECT name, at, event_ids_at FROM "{dst}".mcp_cursors ORDER BY name'
    ).fetchall()
    assert [r["name"] for r in rows] == ["hourly", "smoketest"]
    assert rows[0]["event_ids_at"] == [1, 2]


def test_carry_cursors_from_a_store_older_than_v4_is_a_no_op(pg: Conn) -> None:
    """A v3 store has no `mcp_cursors`; rebuilding it must still work."""
    from jobhunter.store import db

    src = _schema_of(pg)
    dst = f"{src}_new"
    db.init(pg, dst)
    pg.execute(f'DROP TABLE "{src}".mcp_cursors')
    pg.commit()
    assert mcp_state.carry_cursors(pg, src=src, dst=dst) == 0


def test_the_caller_commits(pg: Conn) -> None:
    """The watermark advances only once the payload is out, so the write joins
    the caller's transaction and a rollback loses it."""
    mcp_state.write_cursor(pg, "hourly", Watermark("2026-09-01T06:00:00+00:00", ()))
    pg.rollback()
    assert mcp_state.read_cursor(pg, "hourly") is None
