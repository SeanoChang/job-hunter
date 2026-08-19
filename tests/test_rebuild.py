from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from jobhunter.archive.local import LocalFS
from jobhunter.models import Board
from jobhunter.rebuild import rebuild
from jobhunter.store import db
from tests.conftest import TEST_DSN
from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry


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
