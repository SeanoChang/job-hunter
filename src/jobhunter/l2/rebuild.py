"""Replay the extraction surface from the archive. The LLM is never called:
attempts and review events are historical facts; only their fold (the derived
state) is recomputed, through the same pure function the live runner uses."""

from __future__ import annotations

import json
from typing import Any

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.l2.attempts import derived_error_detail, from_bytes
from jobhunter.l2.runner import _profile_of, _settle
from jobhunter.store import extraction
from jobhunter.store.extraction import Conn
from jobhunter.timeutil import iso, utcnow

X_REVIEWS_PREFIX = "extractions/reviews/"


def rebuild_extractions(
    conn: Conn, store: ArchiveStore, accepted_globs: tuple[str, ...]
) -> tuple[int, int]:
    """Truncate + replay. Returns (attempts_replayed, reviews_replayed)."""
    conn.execute("TRUNCATE extraction_attempts, extraction_reviews, extractions")
    profiles: dict[str, dict[str, Any] | None] = {}
    n_attempts = n_reviews = 0
    for key in store.list(keys.X_ATTEMPTS_PREFIX):
        if keys.parse_x_attempt_key(key) is None:
            continue
        attempt = from_bytes(store.get(key))
        extraction.record_attempt(conn, attempt, derived_error_detail(attempt))
        n_attempts += 1
        if attempt.outcome == "ok" and attempt.record is not None:
            profiles[attempt.document_hash] = _profile_of(attempt.record)
        else:
            profiles.setdefault(attempt.document_hash, None)
    for key in store.list(X_REVIEWS_PREFIX):
        event = json.loads(store.get(key))
        extraction.record_review(conn, **event)
        n_reviews += 1
        profiles.setdefault(event["document_hash"], None)
    now = iso(utcnow())
    for document_hash, profile in profiles.items():
        _settle(conn, document_hash, accepted_globs, profile, now)
    return n_attempts, n_reviews
