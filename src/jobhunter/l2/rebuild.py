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

--- the v20 schema migration -----------------------------------------------

One thing here is not a re-judge. Parsing contract v3 changed the RECORD SHAPE
(schema 2 -> 3: verdicts out, a code-derived `section_heading` and a quoted
`modality_evidence` in), and a shape change is not something re-running a
validator over an archived emit can produce. It is something `l2/v2/migrate`
derives, offline, from what the archive holds — so the corpus reaches the
active contract with zero engine calls instead of 17k re-extractions.

So a replay of a FROZEN schema-2 v2 tuple does its usual fold and then a second
one: every re-judged candidate is derived into its schema-3 form
(`derive_schema3`), verified under the active bundle, and settled as its own
cohort under the active bundle's gate. Both rows stand. The frozen partition is
the honest record of what was extracted and judged under `(v10, 2)`; the
derived partition is what the read surface answers from.

Three decisions that partition needed, and the reasons they went this way.

THE ROW KEY IS THE ACTIVE TUPLE, INCLUDING ITS PROMPT VERSION. Every reader —
`views`, `pulse`, `q claims`, the extraction queue — scopes on the active
bundle's `(prompt_version, schema_version, validator_version)`, and a row keyed
with any other prompt version is invisible to all of them, which would make the
migration a no-op at the only place it is supposed to show. The row key is a
PARTITION SELECTOR, not a provenance claim. Authorship lives in
`extraction_attempts`, which replay restores byte-for-byte from the archive and
which says `demand-profile/v10` for every migrated document; and in the derived
record's own `extraction` envelope, which `derive_schema3` never restamps — it
moves the two identifiers that describe the SHAPE (schema, validator) and
leaves the prompt version exactly as the archived candidate carried it. (That
stamp is assembly's own, `l2/v2/assemble.PROMPT_VERSION`, so the whole v2
corpus reads `demand-profile/v6` there; the point is that the migration adds no
new claim to it, not that the envelope is the authority on what ran.) No
migrated row anywhere asserts that `demand-profile/v11` produced it.

That row key has a cost, and paying it is the other half of the decision. Every
live path re-reads a row by folding `extraction_attempts` scoped to the row's
own tuple, and the migrated attempts are not there and never can be: the
attempt key is the ARCHIVE key and the table's primary key at once, so an
attempt cannot be filed a second time under the partition its derived record
lives in. A migrated row would therefore be a dead projection — `settle` a
no-op, the re-audit queue holding it forever with an `audit_retry` no pass can
clear, a human `reject` silently dropped — which on a 17k-document corpus is
the whole corpus. So the active bundle DECLARES what it adopts
(`Bundle.migrated_from` / `adopt`), and the live fold reads the migrated
attempts as a fallback and derives their records forward through this same
derivation (`runner._Records`). One candidate, one hash, whichever path folded
it. What the drain then buys — a re-audit of the derived candidate, a repair
round on it — is archived against the DERIVED hash, so `_MigratedPhases` looks
there before translating back.

THE AUDIT IS JOINED ON THE SCHEMA-2 CANDIDATE (`_MigratedPhases`). Deriving a
record re-hashes it, so joining artifacts on the derived hash would find
nothing and hand every migrated document `not_checked`: a re-audit bill for the
whole corpus, for a derivation that moved no bound span and changed no quoted
evidence. The artifact describes the extraction, and the derived candidate is
that same extraction in the new shape, so the join runs on the hash the
artifact actually names — the schema-2 one — and `_ArchivedPhases`' own rules
(hash join, audit version in force) decide the rest, unchanged. A repaired
candidate the archive holds is migrated the same way; one that cannot be
derived or verified publishes nothing at all rather than lending its verdict to
a record it is not.

REVIEWS CARRY. A human ruling addressed this document's extraction, and the
derivation neither re-reads the posting nor moves a span — it drops the two
fields the contract retired. Dropping the rulings would re-publish content a
reviewer rejected; keeping them is what makes the migrated partition the same
settlement the schema-2 one reached, under the schema-3 shape. Rulings given
AFTER the migration name the tuple the reviewer saw — the active one — and fold
here too (`_rulings_on`), and the derived row is stamped one second after the
frozen one so that the row `extract review` picks (`ORDER BY updated_at DESC
LIMIT 1`, across all tuples) is the row the read surface answers from rather
than an arbitrary tie-break between the two.

What is NOT migrated: an attempt under a RETIRED schema-2 prompt (v6..v9), which
already folds down the historical branch below — archived verdicts under their
own validator, never re-judged. Deriving a new shape from a candidate this
replay does not re-judge would assert more than the archive supports, and every
such document was re-extracted under v10 anyway. The migration report counts
what is left behind (`documents_not_migrated`) rather than quietly widening,
and gates `--check` on the narrower `documents_owed_migration` — the tuples the
active bundle actually claims — so "total" means total of what was promised.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.l2.agreement import cohort_hook
from jobhunter.l2.assemble import AssembleError
from jobhunter.l2.attempts import Attempt, derived_error_detail, from_bytes
from jobhunter.l2.bundles import Bundle, get_bundle
from jobhunter.l2.runner import _ArchivedPhases, _bundle_for, _hash_of, _Published, _upsert_fold
from jobhunter.l2.schemas import normalize_emit, validate_emit, validate_record
from jobhunter.l2.state import AuditView, Review, derive_state
from jobhunter.l2.v2 import migrate
from jobhunter.l2.v2.assemble import control_char_errors
from jobhunter.l2.v2.source import annotate
from jobhunter.l2.v2.types import Block
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


def _storable_event(attempt: Attempt) -> Attempt:
    """An archived `ok` whose record no store can hold, filed as the defect it is.

    The historical branch folds an archived verdict as-is — it is not re-judged
    and must not be — but "as-is" has one floor: a record carrying a control
    character is not a record the derived surface can express at all (jsonb has
    no NUL), and two attempts sealed under validators 15 and 16, before
    validator/17 added assembly's scan, carry one in a statement topic. Any
    full rebuild since crashed at `upsert_state` rather than settling.

    That defect has a name already, and it is the one validator/17 gives it:
    `attribution_failed`, with the same error string naming the same path. The
    archived attempt object is never rewritten and the tuple never moves — only
    this in-memory fold event changes, and the document's row then settles by
    the ordinary fold rules with no candidate to publish.
    """
    if attempt.outcome != "ok" or attempt.record is None:
        return attempt
    errors = control_char_errors("record", attempt.record)
    if not errors:
        return attempt
    return replace(attempt, outcome="attribution_failed", record=None,
                   validation=[{"error": e} for e in errors])


def derive_schema3(record2: dict[str, Any], blocks: list[Block]) -> dict[str, Any]:
    """A schema-2 record's schema-3 form — the migration's one derivation.

    `l2/v2/migrate.record3_of` owns the rule (the code-owned modal lexicon, the
    heading assembly re-derives, the re-seal under validator 20); this is the
    name the replay calls it by, and the place the provenance promise is
    written down: the derivation moves the two identifiers that describe the
    SHAPE and leaves `extraction.prompt_version` exactly as the archived
    candidate carried it, so no migrated record ever claims the active prompt
    produced it. Raises `migrate.MigrateError` for anything that is not a
    schema-2 record, which is the whole reason the derivation is not attempted
    on shapes the replay has not established.
    """
    return migrate.record3_of(record2, blocks)


def _migration_target(bundle: Bundle) -> Bundle | None:
    """The active bundle a replayed fold owes a DERIVED row to, or None.

    Only one transition exists and only one is claimed: a frozen schema-2 v2
    registration replayed while its family's registered bundle runs schema 3.
    Resolved through the registry by the bundle's own family name rather than
    from a constant, so the day schema 3 is itself frozen behind a schema 4 this
    returns None for it instead of deriving `record3_of` onto a shape it has
    never seen (`migrate` would refuse, but silently owing every document a row
    that never appears is worse than refusing).
    """
    with contextlib.suppress(KeyError):
        active = get_bundle(bundle.name)
        pair = (bundle.schema_version, active.schema_version)
        if pair == (migrate.SOURCE_SCHEMA_VERSION, migrate.SCHEMA_VERSION):
            return active
    return None


def _derive3(
    attempt: Attempt,
    blocks: list[Block],
    markdown: str,
    target: Bundle,
    sources: dict[str, dict[str, Any]],
) -> Attempt:
    """One re-judged schema-2 event as its schema-3 self, judged by `target`.

    The event, not the archived attempt: `_rejudge` has already decided which
    candidate this attempt holds today, and that candidate — the one the
    corpus's artifacts are keyed by — is what the derivation reads. Everything
    that is not a settled record (a schema-invalid response, an exhausted
    ladder, a transport failure) carries its outcome across untouched, because
    the ladder state a fold reads is the same in either shape.

    A derivation that cannot be verified under the active contract is an
    `attribution_failed` event, never a published one: the migrated corpus is
    judged by validator 20, exactly like a freshly extracted document.

    `sources` is the audit join's other half, filled here because this is the
    only place that knows both hashes (`_MigratedPhases`).
    """
    base = replace(
        attempt,
        prompt_version=target.prompt_version,
        schema_version=target.schema_version,
        validator_version=target.validator_version,
        record=None,
    )
    if attempt.outcome != "ok" or attempt.record is None:
        return base
    try:
        record = derive_schema3(attempt.record, blocks)
    except migrate.MigrateError as exc:
        return replace(base, outcome="attribution_failed",
                       validation=[{"error": f"schema-3 derivation refused: {exc}"}])
    if schema_errors := validate_record(record, target.schema_version):
        return replace(base, outcome="schema_invalid",
                       validation=[{"error": e} for e in schema_errors])
    report = target.verify(record, markdown)
    findings: list[dict[str, Any]] = [
        {"check": f.check, "path": f.path, "code": f.code,
         "severity": f.severity, "detail": f.detail}
        for f in report.findings
    ]
    if report.status == "fail":
        return replace(base, outcome="attribution_failed", validation=findings)
    sources[_hash_of(record)] = attempt.record
    return replace(base, outcome="ok", record=record, validation=findings)


class _MigratedPhases:
    """`_ArchivedPhases`, asked about the candidate an artifact actually names.

    The derived record is a re-shaped schema-2 candidate with a new hash, so
    every lookup is translated back to the schema-2 candidate it came from and
    answered by the real `_ArchivedPhases` — same hash join, same audit-version
    rule, same repair-acceptance policy. The only thing added is the return
    trip: a repaired record it publishes is itself schema 2, and what settles
    here has to be schema 3, so it is derived and verified like any other
    candidate. One that will not derive or will not verify publishes nothing —
    borrowing the repaired candidate's audit for the unrepaired record would
    report a verdict on a record no auditor saw.
    """

    def __init__(
        self,
        phases: _ArchivedPhases,
        sources: dict[str, dict[str, Any]],
        blocks: list[Block],
        markdown: str,
        target: Bundle,
    ) -> None:
        self._phases = phases
        self._sources = sources
        self._blocks = blocks
        self._markdown = markdown
        self._target = target

    def _migrated(self, record2: dict[str, Any]) -> dict[str, Any] | None:
        try:
            derived = derive_schema3(record2, self._blocks)
        except migrate.MigrateError:
            return None
        if validate_record(derived, self._target.schema_version):
            return None
        if self._target.verify(derived, self._markdown).status == "fail":
            return None
        return derived

    def published(self, record: dict[str, Any] | None) -> _Published:
        if record is None:
            return self._phases.published(None)
        # the DERIVED hash first. Once the migrated partition is live, its own
        # re-audits and repair rounds run on the derived candidate and name that
        # hash (`runner._Records`), so a replay that only ever translated back
        # to the schema-2 candidate would throw away every verdict the drain
        # bought after the migration — and bill the campaign again.
        direct = self._phases.published(record)
        if direct != _Published():
            return direct
        source = self._sources.get(_hash_of(record))
        if source is None:
            return direct
        found = self._phases.published(source)
        if found.record is None:
            return found
        repaired = self._migrated(found.record)
        return _Published(audit=found.audit, record=repaired) if repaired else _Published()


def _rulings_on(
    reviews_by_group: dict[tuple[str, str, str], list[tuple[str, Review]]],
    dh: str,
    target: Bundle,
    validator_version: str,
) -> list[Review]:
    """Reviews archived against the MIGRATED partition itself.

    A ruling given after the migration names the tuple the reviewer was looking
    at — the active one — so its archived event groups under `(dh, v11, 3)`,
    which holds no attempts and folds to nothing on its own. Replaying it here
    is what makes the derived row recomputable: without it a rebuild silently
    re-publishes a record a human had just parked, which is the one thing the
    module docstring promises reviews never do.
    """
    group = (dh, target.prompt_version, target.schema_version)
    return [r for rvv, r in reviews_by_group.get(group, []) if rvv == validator_version]


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
    phases: _ArchivedPhases | _MigratedPhases,
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
            target = _migration_target(bundle)
            if target is not None:
                # the v20 migration (module docstring): the same events, one
                # shape forward, settled as their own cohort under the active
                # bundle's gate — the agreement comparator sees the DERIVED
                # profiles, which is what validator 20 judges
                blocks = annotate(markdown)
                sources: dict[str, dict[str, Any]] = {}
                derived = [_derive3(e, blocks, markdown, target, sources) for e in events]
                _fold_and_upsert(
                    conn, store, dh, target.prompt_version, target.schema_version,
                    target.validator_version, target, derived,
                    scoped + _rulings_on(
                        reviews_by_group, dh, target, target.validator_version
                    ),
                    accepted_globs,
                    # ONE SECOND AFTER the partition it derives from, and that
                    # is a decision, not a detail: `extract review` picks the
                    # row a human verb addresses with `ORDER BY updated_at DESC
                    # LIMIT 1` over every tuple (cli.py), and two rows stamped
                    # in the same second make that an arbitrary tie-break
                    # between the partition the read surface answers from and
                    # the frozen history beside it. The derived fold genuinely
                    # happens after the fold it derives from; stamping it so is
                    # what puts the reviewer on the row their ruling has to
                    # move.
                    iso(parse_iso(now) + timedelta(seconds=1)),
                    _MigratedPhases(phases, sources, blocks, markdown, target),
                )
        else:
            # historical config, or document no longer materialized: fold the
            # archived verdicts per their own validator version, under whichever
            # bundle still knows the record shape (`_bundle_for`)
            by_vv: dict[str, list[Attempt]] = {}
            for a in attempts:
                # the one thing an unre-judged verdict is still checked for:
                # a record the store cannot hold (`_storable_event`)
                by_vv.setdefault(a.validator_version, []).append(_storable_event(a))
            for vv, group_attempts in by_vv.items():
                scoped = [r for rvv, r in tagged_reviews if rvv == vv]
                _fold_and_upsert(conn, store, dh, pv, sv, vv, bundle, group_attempts,
                                 scoped, accepted_globs, now, phases)
    return n_attempts, n_reviews
