from typing import Any

import psycopg

from jobhunter.store import db
from tests.conftest import TEST_DSN

EXPECTED_TABLES = {
    "fetch_attempts", "posting_versions", "documents", "presence", "runs", "panel",
    "postings", "posting_events", "schema_meta",
    "extraction_attempts", "extraction_reviews", "extractions", "profile_mentions",
    "mcp_cursors",
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


def test_capture_grants_of_an_owner_only_schema_is_empty(
    pg: psycopg.Connection[dict[str, Any]],
) -> None:
    """The owner's implicit privileges are not grants; carrying them would be noise."""
    assert db.capture_grants(pg, _schema_of(pg)) == []


def test_capture_grants_of_a_missing_schema_is_empty(
    pg: psycopg.Connection[dict[str, Any]],
) -> None:
    """The first rebuild of a database has no live schema to copy privileges from."""
    assert db.capture_grants(pg, "jobhunter_no_such_schema") == []


def test_a_privilege_the_carry_cannot_spell_is_refused_loudly() -> None:
    """Better a rebuild that stops than one that swaps in a half-privileged schema."""
    import pytest

    with pytest.raises(ValueError, match="ZAPHOD"):
        db.Grant("jobhunter_ro", ("SELECT", "ZAPHOD"), "postings")


def test_grants_survive_capture_and_apply(pg: psycopg.Connection[dict[str, Any]]) -> None:
    src = _schema_of(pg)
    dst = f"{src}_new"
    db.init(pg, dst)
    pg.execute(f'GRANT USAGE ON SCHEMA "{src}" TO pg_monitor')
    pg.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{src}" TO pg_monitor')
    pg.execute(f'GRANT INSERT, UPDATE, DELETE ON "{src}".mcp_cursors TO pg_monitor')
    pg.commit()
    grants = db.capture_grants(pg, src)
    assert db.Grant("pg_monitor", ("USAGE",), None) in grants
    assert db.apply_grants(pg, dst, grants) == len(grants)
    pg.commit()
    assert db.capture_grants(pg, dst) == grants


def test_apply_grants_skips_a_table_the_new_schema_does_not_have(
    pg: psycopg.Connection[dict[str, Any]],
) -> None:
    """Schema versions drop tables as well as add them; a stale grant is not fatal."""
    src = _schema_of(pg)
    dst = f"{src}_new"
    db.init(pg, dst)
    pg.execute(f'CREATE TABLE "{src}".retired (x int)')
    pg.execute(f'GRANT SELECT ON "{src}".retired TO pg_monitor')
    pg.commit()
    applied = db.apply_grants(pg, dst, db.capture_grants(pg, src))
    pg.commit()
    assert applied == 0


def test_init_surfaces_the_real_ddl_error(pg: psycopg.Connection[dict[str, Any]]) -> None:
    """A DDL conflict must not be masked by InFailedSqlTransaction from the path restore."""
    import pytest

    from tests.conftest import TEST_DSN

    schema = f"{_schema_of(pg)}_v"
    other = db.connect(TEST_DSN, schema=schema)
    try:
        other.execute(
            f'CREATE SCHEMA "{schema}"; CREATE VIEW "{schema}".presence AS SELECT 1 AS x'
        )
        other.commit()
        with pytest.raises(psycopg.Error) as exc:
            db.init(other, schema)
        assert "presence" in str(exc.value)
        assert not isinstance(exc.value, psycopg.errors.InFailedSqlTransaction)
    finally:
        other.rollback()
        other.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        other.commit()
        other.close()


def test_init_refuses_schema_version_mismatch(pg: psycopg.Connection[dict[str, Any]]) -> None:
    import pytest

    from jobhunter.store.db import SchemaMismatch

    db.set_meta(pg, "schema_version", "0")
    pg.commit()
    with pytest.raises(SchemaMismatch, match="0"):
        db.init(pg, _schema_of(pg))
    pg.rollback()


def test_additive_upgrade_from_v1_stamps_version(pg: psycopg.Connection[dict[str, Any]]) -> None:
    schema = _schema_of(pg)
    db.set_meta(pg, "schema_version", "1")
    pg.commit()
    db.init(pg, schema)  # must upgrade in place, not raise SchemaMismatch
    pg.commit()
    assert db.stored_schema_version(pg) == db.SCHEMA_VERSION


def test_additive_upgrade_from_v2_stamps_version(pg: psycopg.Connection[dict[str, Any]]) -> None:
    """v2 -> v3 adds profile_mentions and nothing else, so schema.sql IS the migration."""
    schema = _schema_of(pg)
    db.set_meta(pg, "schema_version", "2")
    pg.execute("DROP TABLE profile_mentions")
    pg.commit()
    db.init(pg, schema)
    pg.commit()
    assert db.stored_schema_version(pg) == db.SCHEMA_VERSION
    assert "profile_mentions" in _tables(pg, schema)


def test_additive_upgrade_from_v3_stamps_version(pg: psycopg.Connection[dict[str, Any]]) -> None:
    """v3 -> v4 adds mcp_cursors and nothing else, so schema.sql IS the migration."""
    schema = _schema_of(pg)
    db.set_meta(pg, "schema_version", "3")
    pg.execute("DROP TABLE mcp_cursors")
    pg.commit()
    db.init(pg, schema)
    pg.commit()
    assert db.stored_schema_version(pg) == db.SCHEMA_VERSION
    assert "mcp_cursors" in _tables(pg, schema)


def test_non_additive_mismatch_still_raises(pg: psycopg.Connection[dict[str, Any]]) -> None:
    import pytest

    schema = _schema_of(pg)
    db.set_meta(pg, "schema_version", "0")
    pg.commit()
    with pytest.raises(db.SchemaMismatch):
        db.init(pg, schema)
