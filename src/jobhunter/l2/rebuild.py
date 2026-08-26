"""Replay the extraction surface from the archive. The LLM is never called.

Provenance rows are restored exactly as archived (historical facts). The
DERIVED fold is another matter: for attempts under the CURRENT prompt/schema
config, the current validators are re-run over each archived raw response
(spec §4.3 step 2) — so a validator bump, or a validator bugfix, re-judges the
whole corpus for $0, and the derived row lands under today's
validator_version. Attempts under historical configs fold as archived, per
their own config."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.l2 import verify
from jobhunter.l2.assemble import AssembleError, assemble
from jobhunter.l2.attempts import Attempt, derived_error_detail, from_bytes
from jobhunter.l2.prompt import PROMPT_VERSION
from jobhunter.l2.schemas import validate_emit
from jobhunter.l2.state import Review, derive_state
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.store import extraction
from jobhunter.store.extraction import Conn
from jobhunter.timeutil import iso, utcnow

SCHEMA_VERSION = "1"
_CONTENT_OUTCOMES = {"ok", "schema_invalid", "attribution_failed"}


def _rejudge(attempt: Attempt, markdown: str) -> Attempt:
    """Derive the current validator's verdict from the archived raw response.
    The archived object is untouched; only the in-memory fold event changes."""
    raw = attempt.raw_response
    assert raw is not None
    base = replace(attempt, validator_version=VALIDATOR_VERSION, record=None)
    try:
        emit = json.loads(raw)
        if not isinstance(emit, dict):
            raise ValueError("top level is not an object")
    except ValueError as exc:
        return replace(base, outcome="schema_invalid",
                       validation=[{"error": f"response is not valid JSON: {exc}"}])
    if schema_errors := validate_emit(emit, attempt.schema_version):
        return replace(base, outcome="schema_invalid",
                       validation=[{"error": e} for e in schema_errors])
    try:
        record = assemble(
            emit, markdown, document_hash=attempt.document_hash,
            normalizer_version=attempt.normalizer_version,
            observed_model=attempt.observed_model or "", at=attempt.started_at,
            prompt_version=attempt.prompt_version, schema_version=attempt.schema_version,
        )
    except AssembleError as exc:
        return replace(base, outcome="attribution_failed",
                       validation=[{"error": e} for e in exc.errors])
    report = verify(record, markdown)
    findings: list[dict[str, Any]] = [
        {"check": f.check, "path": f.path, "code": f.code,
         "severity": f.severity, "detail": f.detail}
        for f in report.findings
    ]
    if report.status == "fail":
        return replace(base, outcome="attribution_failed", validation=findings)
    return replace(base, outcome="ok", record=record, validation=findings)


def _fold_and_upsert(
    conn: Conn,
    dh: str,
    pv: str,
    sv: str,
    vv: str,
    events: list[Attempt],
    reviews: list[Review],
    globs: tuple[str, ...],
    updated_at: str,
) -> None:
    state = derive_state(events, reviews, globs)
    by_key = {a.attempt_key: a for a in events}
    chosen = by_key.get(state.chosen_attempt or "")
    model_col = (
        (chosen.observed_model if chosen else None)
        or (events[-1].requested_model if events else None)
    )
    if model_col is None:
        return
    profile = None
    if state.status in ("validated", "needs_review") and chosen and chosen.record:
        profile = {"facts": chosen.record["facts"],
                   "demand_profile": chosen.record["demand_profile"]}
    extraction.upsert_state(
        conn, document_hash=dh, model=model_col, prompt_version=pv,
        schema_version=sv, validator_version=vv, state=state, profile=profile,
        reviewed_by=reviews[-1].actor if reviews else None, updated_at=updated_at,
    )


def rebuild_extractions(
    conn: Conn, store: ArchiveStore, accepted_globs: tuple[str, ...]
) -> tuple[int, int]:
    """Truncate + replay. Returns (attempts_replayed, reviews_replayed)."""
    conn.execute("TRUNCATE extraction_attempts, extraction_reviews, extractions")
    attempts_by_group: dict[tuple[str, str, str], list[Attempt]] = {}
    reviews_by_group: dict[tuple[str, str, str], list[Review]] = {}
    n_attempts = n_reviews = 0
    for key in store.list(keys.X_ATTEMPTS_PREFIX):
        if keys.parse_x_attempt_key(key) is None:
            continue
        attempt = from_bytes(store.get(key))
        extraction.record_attempt(conn, attempt, derived_error_detail(attempt))
        n_attempts += 1
        group = (attempt.document_hash, attempt.prompt_version, attempt.schema_version)
        attempts_by_group.setdefault(group, []).append(attempt)
    for key in store.list(keys.X_REVIEWS_PREFIX):
        event = json.loads(store.get(key))
        extraction.record_review(conn, **event)
        n_reviews += 1
        group = (event["document_hash"], event["prompt_version"], event["schema_version"])
        reviews_by_group.setdefault(group, []).append(
            Review(verb=event["verb"], at=event["at"], actor=event["actor"],
                   key=event["review_key"])
        )

    now = iso(utcnow())
    for group in sorted(set(attempts_by_group) | set(reviews_by_group)):
        dh, pv, sv = group
        attempts = attempts_by_group.get(group, [])
        reviews = reviews_by_group.get(group, [])
        markdown = (
            extraction.markdown_for(conn, dh, attempts[0].normalizer_version)
            if attempts else None
        )
        if (pv, sv) == (PROMPT_VERSION, SCHEMA_VERSION) and markdown is not None:
            events = [
                _rejudge(a, markdown)
                if a.raw_response is not None and a.outcome in _CONTENT_OUTCOMES
                else replace(a, validator_version=VALIDATOR_VERSION, record=None)
                for a in attempts
            ]
            _fold_and_upsert(conn, dh, pv, sv, VALIDATOR_VERSION, events, reviews,
                             accepted_globs, now)
        else:
            # historical config, or document no longer materialized: fold the
            # archived verdicts per their own validator version
            by_vv: dict[str, list[Attempt]] = {}
            for a in attempts:
                by_vv.setdefault(a.validator_version, []).append(a)
            for vv, group_attempts in by_vv.items():
                _fold_and_upsert(conn, dh, pv, sv, vv, group_attempts, reviews,
                                 accepted_globs, now)
    return n_attempts, n_reviews
