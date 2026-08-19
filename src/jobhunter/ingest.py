"""Replay archive manifests newer than the last ingested one (repair path)."""

from __future__ import annotations

from dataclasses import dataclass

from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.manifests import all_sorted_by_time
from jobhunter.store import db
from jobhunter.store.db import Conn
from jobhunter.store.lifecycle import Ingestor
from jobhunter.timeutil import parse_iso


@dataclass(slots=True)
class ReplaySummary:
    ingested: int = 0
    skipped: int = 0
    last_attempt: str | None = None


def replay_pending(conn: Conn, store: ArchiveStore, *, drop_ratio: float = 0.5) -> ReplaySummary:
    last_at_raw = db.get_meta(conn, "last_ingested_at")
    last_at = parse_iso(last_at_raw) if last_at_raw else None
    last_id = db.get_meta(conn, "last_ingested_attempt")
    ing = Ingestor(conn, store, drop_ratio=drop_ratio)
    out = ReplaySummary()
    for m in all_sorted_by_time(store):
        # Strictly older attempts are behind the watermark; the watermark attempt itself is
        # done. Siblings sharing its instant (one run, several boards) are still candidates —
        # a crash mid-run can leave them uningested.
        if last_at is not None and (m.started_at < last_at or m.attempt_id == last_id):
            continue
        if ing.ingest(m) is None:
            out.skipped += 1
        else:
            out.ingested += 1
            out.last_attempt = m.attempt_id
    return out
