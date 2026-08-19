"""Replay archive manifests newer than the last ingested one (repair path)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.keys import ATTEMPTS_PREFIX, parse_attempt_key
from jobhunter.models import AttemptManifest
from jobhunter.store import db
from jobhunter.store.db import Conn
from jobhunter.store.lifecycle import Ingestor
from jobhunter.timeutil import parse_iso


@dataclass(slots=True)
class ReplaySummary:
    ingested: int = 0
    skipped: int = 0
    last_attempt: str | None = None
    gaps: list[str] = field(default_factory=list)
    """Manifests older than the watermark that are absent from fetch_attempts.

    Incremental ingest cannot insert them without corrupting lifecycle order;
    only `rebuild` repairs them, so they are surfaced loudly, never dropped."""


def replay_pending(conn: Conn, store: ArchiveStore, *, drop_ratio: float = 0.5) -> ReplaySummary:
    last_at_raw = db.get_meta(conn, "last_ingested_at")
    last_at = parse_iso(last_at_raw) if last_at_raw else None
    last_id = db.get_meta(conn, "last_ingested_attempt")
    out = ReplaySummary()

    # Decide everything from KEYS first: the manifest key encodes (source, board,
    # started_at), so no body fetch is needed behind the watermark (spec cost note:
    # a GET per historical manifest per daily run would grow without bound).
    old_keys: list[str] = []
    pending: list[tuple[Any, str]] = []  # (started_at, key)
    for key in store.list(ATTEMPTS_PREFIX):
        parsed = parse_attempt_key(key)
        if parsed is None:
            continue
        _, _, started_at = parsed
        if last_at is not None and (started_at < last_at or key == last_id):
            if key != last_id:
                old_keys.append(key)
            continue
        pending.append((started_at, key))

    if old_keys:
        known = {
            r["attempt_id"]
            for r in conn.execute(
                "SELECT attempt_id FROM fetch_attempts WHERE attempt_id = ANY(%s)", (old_keys,)
            ).fetchall()
        }
        out.gaps = sorted(k for k in old_keys if k not in known)

    ing = Ingestor(conn, store, drop_ratio=drop_ratio)
    for _, key in sorted(pending):
        m = AttemptManifest.from_json(store.get(key))
        if ing.ingest(m) is None:
            out.skipped += 1
        else:
            out.ingested += 1
            out.last_attempt = m.attempt_id
        # One transaction per attempt: bounds subtransaction depth on long replays and
        # makes a crash resume cleanly at the watermark.
        conn.commit()
    conn.commit()  # close the read-only transaction opened by get_meta when nothing was pending
    return out
