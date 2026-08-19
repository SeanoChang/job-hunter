from typing import Any

import psycopg

from jobhunter.store import db
from tests.conftest import TEST_DSN

EXPECTED_TABLES = {
    "fetch_attempts", "posting_versions", "documents", "presence", "runs", "panel",
    "postings", "posting_events", "schema_meta",
}


def _tables(conn: psycopg.Connection[dict[str, Any]], schema: str) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)
    ).fetchall()
    return {r["table_name"] for r in rows}


def _schema_of(conn: psycopg.Connection[dict[str, Any]]) -> str:
    row = conn.execute("SELECT current_schema()").fetchone()
    assert row is not None
    return str(row["current_schema"])


def test_init_creates_all_tables_and_version(pg: psycopg.Connection[dict[str, Any]]) -> None:
    schema = _schema_of(pg)
    assert _tables(pg, schema) == EXPECTED_TABLES
    assert db.stored_schema_version(pg) == db.SCHEMA_VERSION


def test_init_is_idempotent(pg: psycopg.Connection[dict[str, Any]]) -> None:
    schema = _schema_of(pg)
    db.init(pg, schema)
    pg.commit()
    assert _tables(pg, schema) == EXPECTED_TABLES


def test_meta_roundtrip(pg: psycopg.Connection[dict[str, Any]]) -> None:
    assert db.get_meta(pg, "nope") is None
    db.set_meta(pg, "k", "1")
    db.set_meta(pg, "k", "2")
    assert db.get_meta(pg, "k") == "2"


def test_advisory_lock_is_exclusive_across_connections(
    pg: psycopg.Connection[dict[str, Any]],
) -> None:
    other = db.connect(TEST_DSN, schema=_schema_of(pg))
    try:
        assert db.try_lock(pg) is True
        assert db.try_lock(other) is False
        db.unlock(pg)
        assert db.try_lock(other) is True
        db.unlock(other)
    finally:
        other.close()


def test_swap_schema(pg: psycopg.Connection[dict[str, Any]]) -> None:
    target = _schema_of(pg)
    new, prev = f"{target}_new", f"{target}_previous"
    db.init(pg, new)
    pg.execute(f'INSERT INTO "{new}".schema_meta (key, value) VALUES (%s, %s)', ("marker", "new"))
    pg.commit()
    db.swap_schema(pg, new=new, target=target, previous=prev)
    pg.commit()
    assert db.schema_exists(pg, target) and db.schema_exists(pg, prev)
    assert not db.schema_exists(pg, new)
    row = pg.execute(
        f'SELECT value FROM "{target}".schema_meta WHERE key = %s', ("marker",)
    ).fetchone()
    assert row is not None and row["value"] == "new"
    pg.execute(f'DROP SCHEMA "{prev}" CASCADE')
    pg.commit()
