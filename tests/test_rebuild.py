from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from jobhunter.archive.local import LocalFS
from jobhunter.cursors import Watermark
from jobhunter.models import Board
from jobhunter.rebuild import rebuild
from jobhunter.store import db, mcp_state
from tests.conftest import TEST_DSN
from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry

# The suite cannot CREATE ROLE (the local test user is neither superuser nor
# CREATEROLE), so the grants ride on a predefined role that exists in every
# supported Postgres and that nobody logs in as. `jobhunter_ro` and
# `jobhunter_mcp` (spec 2026-09-02 §3) carry exactly these privileges.
GRANTEE = "pg_monitor"


def _live_schema(conn: psycopg.Connection[dict[str, Any]]) -> str:
    row = conn.execute("SELECT current_schema() AS s").fetchone()
    assert row is not None
    return str(row["s"])


def _privileges(
    conn: psycopg.Connection[dict[str, Any]], schema: str, table: str | None = None
) -> set[str]:
    """What GRANTEE may do to `schema` itself, or to one of its tables."""
    if table is None:
        rows = conn.execute(
            "SELECT a.privilege_type AS p FROM pg_namespace n, aclexplode(n.nspacl) a "
            "WHERE n.nspname = %s AND pg_get_userbyid(a.grantee) = %s",
            (schema, GRANTEE),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT a.privilege_type AS p FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a "
            "WHERE n.nspname = %s AND c.relname = %s AND pg_get_userbyid(a.grantee) = %s",
            (schema, table, GRANTEE),
        ).fetchall()
    return {str(r["p"]) for r in rows}


def test_rebuild_builds_in_work_schema_and_swaps(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    store = LocalFS(tmp_path)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    make_manifest(store, "ashby", "ramp", t0,
                  board_payload("ashby", [ab_record("x", "T", "<p>t</p>")]),
                  registry_revision=rev)
    make_manifest(store, "ashby", "ramp", t0 + timedelta(days=1), board_payload("ashby", []),
                  registry_revision=rev)
    row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert row is not None
    target = str(row["s"])
    work, prev = f"{target}_new", f"{target}_previous"
    pg.execute("INSERT INTO schema_meta (key, value) VALUES ('marker', 'old')")
    pg.commit()
    s = rebuild(store, TEST_DSN, drop_ratio=0.5, schema=target, work_schema=work)
    assert s.swapped and s.ingested == 2 and s.work_schema == work
    check = db.connect(TEST_DSN, schema=target)
    try:
        n = check.execute("SELECT count(*) AS n FROM fetch_attempts").fetchone()
        assert n is not None and n["n"] == 2
        assert check.execute("SELECT value FROM schema_meta WHERE key='marker'").fetchone() is None
        assert db.schema_exists(check, prev) and not db.schema_exists(check, work)
        kept = check.execute(
            f'SELECT value FROM "{prev}".schema_meta WHERE key=%s', ("marker",)
        ).fetchone()
        assert kept is not None and kept["value"] == "old"
        check.execute(f'DROP SCHEMA "{prev}" CASCADE')
        check.commit()
    finally:
        check.close()


def test_rebuild_carries_the_mcp_watermarks_across_the_swap(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    """A watermark is the server's place in the event stream, not something the
    archive holds — a rebuild that dropped it would make the next `pulse` a first
    run and re-report the whole window."""
    store = LocalFS(tmp_path)
    target = _live_schema(pg)
    wm = Watermark(at="2026-09-01T06:00:00.123456+00:00", event_ids_at=(41, 42))
    mcp_state.write_cursor(pg, "hourly", wm)
    pg.commit()
    s = rebuild(store, TEST_DSN, drop_ratio=0.5, schema=target, work_schema=f"{target}_new")
    assert s.cursors_carried == 1
    check = db.connect(TEST_DSN, schema=target)
    try:
        assert mcp_state.read_cursor(check, "hourly") == wm
    finally:
        check.close()


def test_rebuild_reapplies_the_role_grants_to_the_schema_it_swaps_in(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    """The swap installs a schema built from scratch. Without carrying the ACLs the
    hosted server's role loses USAGE and every tool answers `permission denied`."""
    store = LocalFS(tmp_path)
    target = _live_schema(pg)
    pg.execute(f'GRANT USAGE ON SCHEMA "{target}" TO {GRANTEE}')
    pg.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{target}" TO {GRANTEE}')
    pg.execute(f'GRANT INSERT, UPDATE, DELETE ON "{target}".mcp_cursors TO {GRANTEE}')
    pg.commit()
    s = rebuild(store, TEST_DSN, drop_ratio=0.5, schema=target, work_schema=f"{target}_new")
    assert s.grants_reapplied > 0
    check = db.connect(TEST_DSN, schema=target)
    try:
        assert _privileges(check, target) == {"USAGE"}
        assert _privileges(check, target, "postings") == {"SELECT"}
        assert _privileges(check, target, "mcp_cursors") == {
            "SELECT", "INSERT", "UPDATE", "DELETE"
        }
    finally:
        check.close()


def test_rebuild_grants_nothing_when_nothing_was_granted(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    """An owner-only store stays owner-only: the carry adds no privileges of its own."""
    store = LocalFS(tmp_path)
    target = _live_schema(pg)
    s = rebuild(store, TEST_DSN, drop_ratio=0.5, schema=target, work_schema=f"{target}_new")
    assert s.grants_reapplied == 0 and s.cursors_carried == 0
    check = db.connect(TEST_DSN, schema=target)
    try:
        row = check.execute(
            "SELECT nspacl IS NULL AS bare FROM pg_namespace WHERE nspname = %s", (target,)
        ).fetchone()
        assert row is not None and row["bare"] is True
    finally:
        check.close()


def test_lock_contention_is_a_distinct_exception(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    import pytest

    from jobhunter.rebuild import LockHeld

    store = LocalFS(tmp_path)
    target = pg.execute("SELECT current_schema() AS s").fetchone()["s"]
    assert db.try_lock(pg)
    try:
        with pytest.raises(LockHeld):
            rebuild(store, TEST_DSN, drop_ratio=0.5, schema=target, work_schema=f"{target}_new")
    finally:
        db.unlock(pg)
