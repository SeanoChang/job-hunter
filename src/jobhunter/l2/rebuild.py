"""Replay the extraction surface from the archive. The LLM is never called.

Provenance rows are restored exactly as archived (historical facts). The
DERIVED fold is another matter: for attempts under a tuple a BUNDLE still
claims, that bundle's validators are re-run over each archived raw response
(spec §4.3 step 2) — so a validator bump, or a validator bugfix, re-judges the
whole corpus for $0, and the derived row lands under today's validator_version.
Attempts under historical configs fold as archived, per their own config.

Every fold goes through the bundle that owns the tuple, and that is what lets
one replay cover a mixed archive: v1 rows through v1's shapes, schema-2 rows
through the v2 contract — its assembly, its verifier, its projections, and the
audit and repair artifacts that decide what a v2 candidate publishes. A replay
that assumed v1 could not fold a schema-2 record at all (it carries no
`demand_profile`), which is what kept `extract rebuild` off the v2 corpus.

Two rules make the replay trustworthy where the live path reads keys.

A re-judge that changes nothing about a candidate leaves it alone. A settlement
policy bump still stamps its own version into the extraction envelope, and
folding that in would re-key every candidate in the corpus — orphaning the
audit and repair artifacts keyed by those hashes, which are exactly what a
re-settle campaign is replaying for. A candidate that re-derives identically IS
the candidate the archive holds (`_same_candidate`).

And phase artifacts are joined by CANDIDATE HASH, never by attempt key: an
audit describes the candidate it named. When today's validators re-derive that
candidate, the artifact still describes it and carries; when they re-derive a
different one, it does not, and the record settles honestly unaudited.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.l2.agreement import cohort_hook
from jobhunter.l2.assemble import AssembleError
from jobhunter.l2.attempts import Attempt, derived_error_detail, from_bytes
from jobhunter.l2.bundles import Bundle
from jobhunter.l2.runner import _ArchivedPhases, _bundle_for, _upsert_fold
from jobhunter.l2.schemas import normalize_emit, validate_emit
from jobhunter.l2.state import AuditView, Review, derive_state
from jobhunter.store import extraction
from jobhunter.store.extraction import Conn
from jobhunter.timeutil import iso, parse_iso, utcnow

_CONTENT_OUTCOMES = {"ok", "schema_invalid", "attribution_failed"}
#: the two extraction-envelope fields a validator bump moves on its own
_STAMPED = ("validator_version", "candidate_hash")


def _content(record: dict[str, Any]) -> dict[str, Any]:
    """The candidate minus the stamps a bump moves by itself."""
    envelope = record.get("extraction")
    if not isinstance(envelope, dict):
        return record  # a record shape with no envelope (v1) is all content
    return {
        **record,
        "extraction": {k: v for k, v in envelope.items() if k not in _STAMPED},
    }


def _same_candidate(archived: dict[str, Any] | None, rederived: dict[str, Any]) -> bool:
    """Is the re-judged record the candidate the archive already holds?

    Equal everywhere the extraction means anything. The archived record wins
    when it is, because its identity is what the corpus's audit and repair
    artifacts are keyed by, and a version stamp is not a reason to re-key a
    candidate whose content never moved. Anything the re-judge really changed
    lands as a different candidate, exactly as it should — with no verdict.
    """
    return archived is not None and _content(archived) == _content(rederived)


def _rejudge(attempt: Attempt, markdown: str, bundle: Bundle) -> Attempt:
    """Derive the current validator's verdict from the archived raw response.
    The archived object is untouched; only the in-memory fold event changes."""
    raw = attempt.raw_response
    assert raw is not None
    base = replace(attempt, validator_version=bundle.validator_version, record=None)
    try:
        emit = json.loads(raw)
        if not isinstance(emit, dict):
            raise ValueError("top level is not an object")
        emit = normalize_emit(emit, attempt.schema_version)
    except ValueError as exc:
        return replace(base, outcome="schema_invalid",
                       validation=[{"error": f"response is not valid JSON: {exc}"}])
    if schema_errors := validate_emit(emit, attempt.schema_version):
        return replace(base, outcome="schema_invalid",
                       validation=[{"error": e} for e in schema_errors])
    try:
        record = bundle.assemble(
            emit, markdown, document_hash=attempt.document_hash,
            normalizer_version=attempt.normalizer_version,
            observed_model=attempt.observed_model or "",
            # the live path assembles with `iso(t0)` and archives the attempt
            # with `t0.isoformat()`, two spellings of one instant. Re-derive
            # from the SPELLING assembly saw: a schema-2 candidate hash covers
            # its extraction envelope, so feeding the other one re-keys every
            # replayed candidate and orphans its audit.
            at=iso(parse_iso(attempt.started_at)),
        )
    except AssembleError as exc:
        return replace(base, outcome="attribution_failed",
                       validation=[{"error": e} for e in exc.errors])
    report = bundle.verify(record, markdown)
    findings: list[dict[str, Any]] = [
        {"check": f.check, "path": f.path, "code": f.code,
         "severity": f.severity, "detail": f.detail}
        for f in report.findings
    ]
    if report.status == "fail":
        return replace(base, outcome="attribution_failed", validation=findings)
    archived = attempt.record
    if archived is not None and _same_candidate(archived, record):
        record = archived  # same candidate: keep the identity its artifacts key on
    return replace(base, outcome="ok", record=record, validation=findings)


def _fold_and_upsert(
    conn: Conn,
    store: ArchiveStore,
    dh: str,
    pv: str,
    sv: str,
    vv: str,
    bundle: Bundle,
    events: list[Attempt],
    reviews: list[Review],
    globs: tuple[str, ...],
    updated_at: str,
    phases: _ArchivedPhases,
) -> None:
    # THE SAME gate as live settlement (review P0-1): replay must derive the
    # identical verdict, k, agreement and audit dimensions, or rebuild silently
    # promotes what the live path demoted. Records here are the in-memory
    # re-judged ones, and the artifacts are the archive's own.
    by_key = {a.attempt_key: a for a in events}

    def _record(a: Attempt) -> dict[str, Any] | None:
        return bundle.profile_of(a.record) if a.record is not None else None

    def _audit(attempt_key: str) -> AuditView | None:
        found = by_key.get(attempt_key)
        return phases.published(found.record if found is not None else None).audit

    def _record_of(attempt_key: str) -> dict[str, Any] | None:
        found = by_key.get(attempt_key)
        if found is None or found.record is None:
            return None
        repaired = phases.published(found.record).record
        return repaired if repaired is not None else found.record

    state = derive_state(
        events, reviews, globs, cohort_hook(_record, f1_min=bundle.agreement_f1_min),
        _audit if bundle.audit_version is not None else None,
    )
    _upsert_fold(
        conn, store, dh, bundle, prompt_version=pv, schema_version=sv,
        validator_version=vv, attempts=events, reviews=reviews, state=state,
        updated_at=updated_at, record_of=_record_of,
    )


def rebuild_extractions(
    conn: Conn, store: ArchiveStore, accepted_globs: tuple[str, ...]
) -> tuple[int, int]:
    """Truncate + replay. Returns (attempts_replayed, reviews_replayed)."""
    # profile_mentions is derived from extractions.profile, so it is emptied with
    # them and refilled by the same upserts the replay drives.
    conn.execute(
        "TRUNCATE extraction_attempts, extraction_reviews, extractions, profile_mentions"
    )
    attempts_by_group: dict[tuple[str, str, str], list[Attempt]] = {}
    reviews_by_group: dict[tuple[str, str, str], list[tuple[str, Review]]] = {}
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
            (event.get("validator_version", ""),
             Review(verb=event["verb"], at=event["at"], actor=event["actor"],
                    key=event["review_key"]))
        )

    now = iso(utcnow())
    phases = _ArchivedPhases(store)  # one scan, shared by every group below
    for group in sorted(set(attempts_by_group) | set(reviews_by_group)):
        dh, pv, sv = group
        attempts = attempts_by_group.get(group, [])
        tagged_reviews = reviews_by_group.get(group, [])
        bundle = _bundle_for(pv, sv)
        markdown = (
            extraction.markdown_for(conn, dh, attempts[0].normalizer_version)
            if attempts else None
        )
        claimed = (bundle.prompt_version, bundle.schema_version) == (pv, sv)
        if claimed and markdown is not None:
            vv = bundle.validator_version
            events = [
                _rejudge(a, markdown, bundle)
                if a.raw_response is not None and a.outcome in _CONTENT_OUTCOMES
                else replace(a, validator_version=vv, record=None)
                for a in attempts
            ]
            # a review speaks only for the validator version it addressed
            # (review P0-1): re-judging under today's validator folds only the
            # reviews given under it
            scoped = [r for rvv, r in tagged_reviews if rvv == vv]
            _fold_and_upsert(conn, store, dh, pv, sv, vv, bundle, events, scoped,
                             accepted_globs, now, phases)
        else:
            # historical config, or document no longer materialized: fold the
            # archived verdicts per their own validator version, under whichever
            # bundle still knows the record shape (`_bundle_for`)
            by_vv: dict[str, list[Attempt]] = {}
            for a in attempts:
                by_vv.setdefault(a.validator_version, []).append(a)
            for vv, group_attempts in by_vv.items():
                scoped = [r for rvv, r in tagged_reviews if rvv == vv]
                _fold_and_upsert(conn, store, dh, pv, sv, vv, bundle, group_attempts,
                                 scoped, accepted_globs, now, phases)
    return n_attempts, n_reviews
