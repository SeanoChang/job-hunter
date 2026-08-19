from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from jobhunter.archive.local import LocalFS
from jobhunter.ingest import replay_pending
from jobhunter.models import Board
from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry


def test_replay_pending_is_incremental_and_idempotent(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    store = LocalFS(tmp_path)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    body = board_payload("ashby", [ab_record("x", "T", "<p>t</p>")])
    make_manifest(store, "ashby", "ramp", t0, body, registry_revision=rev)
    make_manifest(store, "ashby", "ramp", t0 + timedelta(days=1), body, registry_revision=rev)
    s1 = replay_pending(pg, store)
    pg.commit()
    assert (s1.ingested, s1.skipped) == (2, 0)
    make_manifest(store, "ashby", "ramp", t0 + timedelta(days=2), body, registry_revision=rev)
    s2 = replay_pending(pg, store)
    pg.commit()
    assert (s2.ingested, s2.skipped) == (1, 0)
    s3 = replay_pending(pg, store)
    assert (s3.ingested, s3.skipped) == (0, 0)
    row = pg.execute("SELECT count(*) AS n FROM fetch_attempts").fetchone()
    assert row is not None and row["n"] == 3
