"""Replay the extraction surface from the archive. The LLM is never called:
attempts and review events are historical facts; only their fold (the derived
state) is recomputed, per (document, config), through the same `settle` the
live runner uses — so a rebuilt surface is bit-identical to the live one."""

from __future__ import annotations

import json

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.l2.attempts import derived_error_detail, from_bytes
from jobhunter.l2.runner import settle
from jobhunter.store import extraction
from jobhunter.store.extraction import Conn
from jobhunter.timeutil import iso, utcnow


def rebuild_extractions(
    conn: Conn, store: ArchiveStore, accepted_globs: tuple[str, ...]
) -> tuple[int, int]:
    """Truncate + replay. Returns (attempts_replayed, reviews_replayed)."""
    conn.execute("TRUNCATE extraction_attempts, extraction_reviews, extractions")
    touched: set[tuple[str, str, str, str]] = set()
    n_attempts = n_reviews = 0
    for key in store.list(keys.X_ATTEMPTS_PREFIX):
        if keys.parse_x_attempt_key(key) is None:
            continue
        attempt = from_bytes(store.get(key))
        extraction.record_attempt(conn, attempt, derived_error_detail(attempt))
        n_attempts += 1
        touched.add(
            (attempt.document_hash, attempt.prompt_version, attempt.schema_version,
             attempt.validator_version)
        )
    for key in store.list(keys.X_REVIEWS_PREFIX):
        event = json.loads(store.get(key))
        extraction.record_review(conn, **event)
        n_reviews += 1
        touched.add(
            (event["document_hash"], event["prompt_version"], event["schema_version"],
             event["validator_version"])
        )
    now = iso(utcnow())
    for document_hash, pv, sv, vv in touched:
        settle(conn, store, document_hash, accepted_globs, now,
               prompt_version=pv, schema_version=sv, validator_version=vv)
    return n_attempts, n_reviews
