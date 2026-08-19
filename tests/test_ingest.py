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


def test_gap_manifests_are_detected_not_silently_dropped(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    from jobhunter.store.lifecycle import Ingestor

    store = LocalFS(tmp_path)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    body = board_payload("ashby", [ab_record("x", "T", "<p>t</p>")])
    ing = Ingestor(pg, store)
    ing.ingest(make_manifest(store, "ashby", "ramp", t0, body, registry_revision=rev))
    # day 1 is archived while the DB was down; day 2 advances the watermark past it
    gap = make_manifest(store, "ashby", "ramp", t0 + timedelta(days=1), body, registry_revision=rev)
    m2 = make_manifest(store, "ashby", "ramp", t0 + timedelta(days=2), body,
                       registry_revision=rev)
    ing.ingest(m2)
    pg.commit()
    s = replay_pending(pg, store)
    pg.commit()
    assert s.ingested == 0
    assert s.gaps == [gap.attempt_id]  # loud, not silent: the store is missing this attempt


class _CountingStore(LocalFS):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.gets: list[str] = []

    def get(self, key: str) -> bytes:
        self.gets.append(key)
        return super().get(key)


def test_replay_does_not_fetch_manifest_bodies_behind_the_watermark(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    from jobhunter.store.lifecycle import Ingestor

    store = _CountingStore(tmp_path)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    body = board_payload("ashby", [ab_record("x", "T", "<p>t</p>")])
    ing = Ingestor(pg, store)
    for n in range(5):
        ing.ingest(make_manifest(store, "ashby", "ramp", t0 + timedelta(days=n), body,
                                 registry_revision=rev))
    pg.commit()
    store.gets.clear()
    s = replay_pending(pg, store)
    assert (s.ingested, s.gaps) == (0, [])
    manifest_gets = [k for k in store.gets if k.startswith("attempts/")]
    assert manifest_gets == []  # keys alone decide; no body fetch behind the watermark
    assert pg.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
