"""The extraction drain loop (harness spec §4.4/§4.6). Serial in M2.

Archive-before-DB per attempt; a crash between the two is healed by the next
run's catch-up scan. The ladder (`l2_model_candidates`) escalates on content
failure and falls through on model-not-found; quarantine only after the ladder
is exhausted. `observed_model` gates everything: out-of-glob responses are
`model_rejected`, and five consecutive rejections abort the run.

A batch is long and mostly spent waiting on an engine, so the database
connection under it is expected to die (Neon suspends an idle project): every
DB touch goes through `_Session`, which reconnects, re-takes the extract lock
and replays the one failed call. Cleanup on a dead connection is best-effort so
it cannot overwrite the failure that ended the run.

`settle` is the ONLY writer of the derived `extractions` row — the runner, the
catch-up scan, the review verbs and `extract rebuild` all fold the same
config-scoped event streams through it, and the stored profile always comes
from the chosen attempt's archived record, never from caller context.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg

from jobhunter import __version__
from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.config import Settings
from jobhunter.l2 import verify
from jobhunter.l2.assemble import AssembleError, assemble
from jobhunter.l2.attempts import Attempt, derived_error_detail, from_bytes, to_bytes
from jobhunter.l2.engines import (
    Engine,
    EngineFatalError,
    EngineModelNotFound,
    EngineThrottled,
    EngineTransportError,
)
from jobhunter.l2.prompt import PROMPT_VERSION, TEMPLATE, prompt_sha, render
from jobhunter.l2.schemas import emit_schema, validate_emit
from jobhunter.l2.state import DerivedState, derive_state, globs_to_regex, model_matches
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.markdown import NORMALIZER_VERSION
from jobhunter.store import db, extraction
from jobhunter.store.extraction import Conn
from jobhunter.timeutil import iso, utcnow_precise

SCHEMA_VERSION = "1"
MAX_DOC_CHARS = 60_000
CONTENT_ATTEMPTS = 3
TRANSPORT_RETRIES = 3
BREAKER_LIMIT = 5


class LockLost(RuntimeError):
    """The extract lock moved to another writer while our connection was down."""


class _Journal:
    """Everything this run has written to the extraction surface, replayable.

    A dropped backend rolls back every uncommitted row, including the rows
    written before the statement that died. The next run's catch-up scan cannot
    heal those: the watermark is max(started_at) over the rows that DID commit,
    so once the replacement connection commits a later attempt the orphans sit
    behind the watermark forever and only `extract rebuild` would find them. The
    run therefore re-applies its own writes onto the new connection. Both
    `record_*` are ON CONFLICT DO NOTHING, so a row that survived is a no-op and
    only what was actually lost is re-derived through `settle`.
    """

    def __init__(
        self, store: ArchiveStore, globs: tuple[str, ...], now: Callable[[], datetime]
    ) -> None:
        self.store = store
        self.globs = globs
        self._now = now
        self._attempts: dict[str, Attempt] = {}
        self._reviews: dict[str, dict[str, Any]] = {}

    def record_attempt(self, conn: Conn, attempt: Attempt) -> bool:
        self._attempts[attempt.attempt_key] = attempt
        return extraction.record_attempt(conn, attempt, derived_error_detail(attempt))

    def record_review(self, conn: Conn, event: dict[str, Any]) -> bool:
        self._reviews[event["review_key"]] = event
        return extraction.record_review(conn, **event)

    def replay(self, conn: Conn) -> None:
        """Re-apply onto a fresh connection, then re-settle only what was lost."""
        touched: set[tuple[str, str, str, str]] = set()
        for attempt in self._attempts.values():
            if extraction.record_attempt(conn, attempt, derived_error_detail(attempt)):
                touched.add((attempt.document_hash, attempt.prompt_version,
                             attempt.schema_version, attempt.validator_version))
        for event in self._reviews.values():
            if extraction.record_review(conn, **event):
                touched.add((event["document_hash"], event["prompt_version"],
                             event["schema_version"], event["validator_version"]))
        updated_at = iso(self._now())
        for dh, pv, sv, vv in touched:
            # the derived row died with the attempt rows: without this a run can
            # report a validation the store does not hold (death on the per-doc
            # commit, after settle has already written it)
            settle(conn, self.store, dh, self.globs, updated_at,
                   prompt_version=pv, schema_version=sv, validator_version=vv)


class _Session:
    """The runner's connection, replaceable mid-run.

    A managed Postgres (Neon) suspends an idle project after ~5 minutes and
    drops its connections; the next statement raises OperationalError. Every DB
    touch goes through `do`, which reconnects once, re-takes the extract lock,
    re-applies the run's journal and replays that one call. Replay is safe
    because an attempt is archived BEFORE it is recorded and `record_attempt` is
    idempotent on attempt_key.
    """

    def __init__(
        self,
        conn: Conn,
        connect: Callable[[], Conn] | None,
        journal: _Journal | None = None,
    ) -> None:
        self.conn = conn
        self.holds_lock = False
        self._connect = connect
        self._journal = journal
        self._owned = False  # the caller's connection stays the caller's to close

    def do[T](self, op: Callable[[Conn], T]) -> T:
        try:
            return op(self.conn)
        except psycopg.OperationalError:
            if self._connect is None:
                raise
            self._revive()
            return op(self.conn)

    def _revive(self) -> None:
        assert self._connect is not None  # `do` checks before calling
        with contextlib.suppress(psycopg.Error):
            self.conn.close()
        self.conn = self._connect()  # restores search_path with the schema
        self._owned = True
        # the dead backend released its session-scoped advisory lock: another
        # writer may hold it now, and two drains must never write at once
        self.holds_lock = db.try_lock(self.conn, db.EXTRACT_LOCK_KEY)
        if not self.holds_lock:
            raise LockLost("the extract lock is held by another writer")
        if self._journal is not None:
            self._journal.replay(self.conn)

    def release(self) -> None:
        """Commit, unlock, and drop a connection we opened — best effort.

        Cleanup against a corpse must never replace the exception that killed
        the run: an engine failure reported as "database error" hides the only
        fact worth acting on (CI run 33632605810).
        """
        if self.holds_lock:
            with contextlib.suppress(psycopg.OperationalError):
                self.conn.commit()
                db.unlock(self.conn, db.EXTRACT_LOCK_KEY)
                self.conn.commit()
        if self._owned:
            with contextlib.suppress(psycopg.Error):
                self.conn.close()


@dataclass
class ExtractSummary:
    run_id: str
    lock_held: bool = False
    docs_attempted: int = 0
    validated: int = 0
    quarantined: int = 0
    pending: int = 0
    throttled: bool = False
    breaker_abort: bool = False
    replayed: int = 0
    spend_usd: float = 0.0
    queued: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "lock_held": self.lock_held,
            "docs_attempted": self.docs_attempted, "validated": self.validated,
            "quarantined": self.quarantined, "pending": self.pending,
            "throttled": self.throttled, "breaker_abort": self.breaker_abort,
            "replayed": self.replayed, "spend_usd": round(self.spend_usd, 5),
            "queued": self.queued,
        }


def _ensure_write_once(store: ArchiveStore) -> None:
    pk = keys.x_prompt_key(PROMPT_VERSION)
    if not store.exists(pk):
        store.put(pk, TEMPLATE.encode("utf-8"))
    sk = keys.x_schema_key(SCHEMA_VERSION)
    if not store.exists(sk):
        store.put(sk, json.dumps(emit_schema(SCHEMA_VERSION), sort_keys=True).encode("utf-8"))


def _profile_of(record: dict[str, Any]) -> dict[str, Any]:
    return {"facts": record["facts"], "demand_profile": record["demand_profile"]}


def settle(
    conn: Conn,
    store: ArchiveStore,
    dh: str,
    globs: tuple[str, ...],
    updated_at: str,
    *,
    prompt_version: str | None = None,
    schema_version: str | None = None,
    validator_version: str | None = None,
) -> DerivedState:
    # call-time resolution: definition-time defaults would freeze the constants
    # and silently ignore a version bump
    prompt_version = PROMPT_VERSION if prompt_version is None else prompt_version
    schema_version = SCHEMA_VERSION if schema_version is None else schema_version
    validator_version = (
        VALIDATOR_VERSION if validator_version is None else validator_version
    )
    attempts = extraction.attempts_for(
        conn, dh, prompt_version=prompt_version, schema_version=schema_version,
        validator_version=validator_version,
    )
    reviews = extraction.reviews_for(
        conn, dh, prompt_version=prompt_version, schema_version=schema_version,
        validator_version=validator_version,
    )
    state = derive_state(attempts, reviews, globs)
    chosen = {a.attempt_key: a for a in attempts}.get(state.chosen_attempt or "")
    model_col = (
        (chosen.observed_model if chosen else None)
        or (attempts[-1].requested_model if attempts else None)
    )
    if model_col is None:
        return state  # nothing decisive ever happened; no row to write
    profile: dict[str, Any] | None = None
    if state.status in ("validated", "needs_review") and state.chosen_attempt:
        # the profile is the CHOSEN attempt's archived record — never whatever
        # record the caller happened to hold (a later ok attempt, or nothing)
        chosen_obj = from_bytes(store.get(state.chosen_attempt))
        if chosen_obj.record is not None:
            profile = _profile_of(chosen_obj.record)
    extraction.upsert_state(
        conn, document_hash=dh, model=model_col, prompt_version=prompt_version,
        schema_version=schema_version, validator_version=validator_version,
        state=state, profile=profile,
        reviewed_by=reviews[-1].actor if reviews else None,
        updated_at=updated_at,
    )
    return state


def _catch_up(conn: Conn, journal: _Journal, updated_at: str) -> int:
    store, globs = journal.store, journal.globs
    mark = extraction.watermark(conn)
    mark = mark.replace(microsecond=0) if mark is not None else None
    start_after = None
    if mark is not None:
        # one second BEFORE the watermark: keys stamp whole seconds, and an
        # orphan written in the same second as the watermark must be replayed
        # (record_attempt is idempotent, so re-listing that second is free)
        stamp = iso(mark - timedelta(seconds=1))
        start_after = (
            f"{keys.X_ATTEMPTS_PREFIX}{stamp[0:4]}/{stamp[5:7]}/"
            f"{stamp[8:10]}T{stamp[11:19].replace(':', '')}Z"
        )
    replayed = 0
    touched: set[tuple[str, str, str, str]] = set()
    for key in store.list(keys.X_ATTEMPTS_PREFIX, start_after=start_after):
        parsed = keys.parse_x_attempt_key(key)
        if parsed is None:
            continue
        if mark is not None and parsed[0] < mark:
            continue
        attempt = from_bytes(store.get(key))
        # through the journal: a reconnect later in this run must not roll the
        # replay back into the same invisibility it just healed
        if journal.record_attempt(conn, attempt):
            replayed += 1
            touched.add(
                (attempt.document_hash, attempt.prompt_version, attempt.schema_version,
                 attempt.validator_version)
            )
    # review events are archived BEFORE their DB row (archive-as-truth): a crash
    # between the two must not leave a human decision unapplied until a manual
    # rebuild. The prefix is tiny (human verbs), so a full idempotent scan is fine.
    for key in store.list(keys.X_REVIEWS_PREFIX):
        event = json.loads(store.get(key))
        if journal.record_review(conn, event):
            replayed += 1
            touched.add(
                (event["document_hash"], event["prompt_version"], event["schema_version"],
                 event["validator_version"])
            )
    for dh, pv, sv, vv in touched:
        settle(conn, store, dh, globs, updated_at,
               prompt_version=pv, schema_version=sv, validator_version=vv)
    return replayed


def run(
    settings: Settings,
    conn: Conn,
    store: ArchiveStore,
    *,
    engine: Engine,
    max_docs: int,
    max_usd: float,
    only_doc: str | None = None,
    dry_run: bool = False,
    now: Callable[[], datetime] = utcnow_precise,
    connect: Callable[[], Conn] | None = None,
) -> ExtractSummary:
    started = now()
    summary = ExtractSummary(run_id=f"x-{iso(started).replace(':', '').replace('-', '')}")
    if not settings.l2_model_candidates:
        raise ValueError("l2_model_candidates is empty; require_l2() must run before extraction")
    if not db.try_lock(conn, db.EXTRACT_LOCK_KEY):
        summary.lock_held = True
        return summary
    journal = _Journal(store, settings.l2_models, now)
    session = _Session(conn, connect, journal)
    session.holds_lock = True
    try:
        # done = a row exists under any ACCEPTED model (l2_models) at the current
        # versions; candidates are what we ask for, not what satisfies (spec §4.1)
        model_regex = globs_to_regex(settings.l2_models)

        def queue(c: Conn) -> list[str]:
            return extraction.queue(
                c, prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
                validator_version=VALIDATOR_VERSION, model_regex=model_regex,
                normalizer_version=NORMALIZER_VERSION, limit=max_docs,
            )

        if dry_run:
            # strictly read-only: no write-once objects, no catch-up replay
            summary.queued = [only_doc] if only_doc else session.do(queue)
            return summary
        _ensure_write_once(store)
        summary.replayed = session.do(lambda c: _catch_up(c, journal, iso(now())))
        session.do(lambda c: c.commit())
        docs = [only_doc] if only_doc else session.do(queue)
        summary.queued = docs
        breaker = 0
        for dh in docs:
            # strict >: a cap of 0 means "free work only" (the documented
            # subscription-backfill mode), not "stop before the first document"
            if summary.docs_attempted >= max_docs or summary.spend_usd > max_usd:
                break
            result = _extract_doc(settings, session, journal, engine, dh, summary, breaker, now)
            if result is None:
                continue  # document vanished (normalizer bump mid-flight)
            disposition, breaker = result
            session.do(lambda c: c.commit())
            if disposition == "validated":
                summary.validated += 1
            elif disposition == "quarantined":
                summary.quarantined += 1
            elif disposition == "pending":
                summary.pending += 1
            elif disposition == "throttled":
                summary.throttled = True
                break
            elif disposition == "breaker":
                summary.breaker_abort = True
                break
    except LockLost:
        # another writer owns the drain now; our uncommitted work died with the
        # connection and the archive lets the next run replay it
        summary.lock_held = True
    finally:
        session.release()
    return summary


def _extract_doc(
    settings: Settings,
    session: _Session,
    journal: _Journal,
    engine: Engine,
    dh: str,
    summary: ExtractSummary,
    breaker: int,
    now: Callable[[], datetime],
) -> tuple[str, int] | None:
    store = journal.store
    markdown = session.do(lambda c: extraction.markdown_for(c, dh, NORMALIZER_VERSION))
    if markdown is None:
        return None
    summary.docs_attempted += 1
    seq = session.do(lambda c: extraction.next_attempt_no(c, dh)) - 1
    schema = emit_schema(SCHEMA_VERSION)

    def archive_attempt(
        *, requested_model: str, observed_model: str | None, outcome: str,
        raw_response: str | None, fed: list[str], produced: list[str],
        ladder_exhausted: bool, findings: list[dict[str, Any]] | None = None,
        tokens: tuple[int | None, int | None] = (None, None),
        cost: float | None = None, started_at: datetime | None = None,
        record: dict[str, Any] | None = None,
    ) -> Attempt:
        # `fed` reproduces the rendered prompt (spec §4.2: prior_errors is the
        # non-reconstructible part of the request); `produced` is what this
        # attempt's validation yielded, stored in the trace + DB error_detail.
        nonlocal seq
        seq += 1
        t0 = started_at or now()
        validation = list(findings or []) + [{"error": e} for e in produced]
        attempt = Attempt(
            attempt_key=keys.x_attempt_key(t0, dh, 1, seq),
            run_id=summary.run_id, cli_version=__version__, document_hash=dh,
            normalizer_version=NORMALIZER_VERSION, sample_slot=1, attempt_no=seq,
            requested_engine=engine.name, requested_model=requested_model,
            observed_model=observed_model, prompt_version=PROMPT_VERSION,
            prompt_sha256=prompt_sha(), schema_version=SCHEMA_VERSION,
            validator_version=VALIDATOR_VERSION, prior_errors=list(fed),
            raw_response=raw_response, validation=validation, outcome=outcome,
            ladder_exhausted=ladder_exhausted, input_tokens=tokens[0],
            output_tokens=tokens[1], cost_usd=cost, started_at=t0.isoformat(),
            finished_at=now().isoformat(), record=record,
        )
        store.put(attempt.attempt_key, to_bytes(attempt))
        session.do(lambda c: journal.record_attempt(c, attempt))
        return attempt

    def settle_and_disposition() -> tuple[str, int]:
        state = session.do(lambda c: settle(c, store, dh, settings.l2_models, iso(now())))
        # the summary must agree with the fold: model_rejected fall-throughs
        # settle to no row (pending), not quarantine
        if state.status == "quarantined":
            return "quarantined", breaker
        if state.status == "validated":
            return "validated", breaker
        return "pending", breaker

    candidates = settings.l2_model_candidates
    if len(markdown) > MAX_DOC_CHARS:
        archive_attempt(
            requested_model=candidates[0], observed_model=None,
            outcome="over_budget", raw_response=None, fed=[],
            produced=[f"document {len(markdown)} chars > {MAX_DOC_CHARS}"],
            ladder_exhausted=True,
        )
        return settle_and_disposition()

    for rung_i, model in enumerate(candidates):
        last_rung = rung_i == len(candidates) - 1
        prior_errors: list[str] = []
        transports = 0
        content_no = 0
        while content_no < CONTENT_ATTEMPTS:
            t0 = now()
            prompt = render(markdown, prior_errors)
            try:
                result = engine.complete(prompt, schema, model)
            except EngineThrottled as exc:
                archive_attempt(requested_model=model, observed_model=None,
                                outcome="throttled", raw_response=None,
                                fed=prior_errors, produced=[str(exc)],
                                ladder_exhausted=False, started_at=t0)
                return "throttled", breaker
            except EngineFatalError as exc:
                # credentials, payment, malformed request: nothing about this
                # document caused it and no rung or retry fixes it. Record the
                # evidence, then let the engine's own words reach the caller —
                # they must never come back as a database error.
                archive_attempt(requested_model=model, observed_model=None,
                                outcome="engine_fatal", raw_response=None,
                                fed=prior_errors, produced=[str(exc)],
                                ladder_exhausted=False, started_at=t0)
                raise
            except EngineModelNotFound:
                archive_attempt(requested_model=model, observed_model=None,
                                outcome="model_rejected", raw_response=None,
                                fed=prior_errors, produced=["model not found"],
                                ladder_exhausted=False, started_at=t0)
                breaker += 1
                if breaker >= BREAKER_LIMIT:
                    return "breaker", breaker
                break  # next rung
            except EngineTransportError as exc:
                transports += 1
                archive_attempt(requested_model=model, observed_model=None,
                                outcome="transport", raw_response=None,
                                fed=prior_errors, produced=[str(exc)],
                                ladder_exhausted=False, started_at=t0)
                if transports >= TRANSPORT_RETRIES:
                    return "pending", breaker  # transport says nothing about the doc
                continue

            summary.spend_usd += result.cost_usd or 0.0
            observed = result.observed_model
            if not model_matches(observed, settings.l2_models):
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="model_rejected", raw_response=result.raw_text,
                                fed=prior_errors,
                                produced=[f"observed model {observed!r} outside globs"],
                                ladder_exhausted=False, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                breaker += 1
                if breaker >= BREAKER_LIMIT:
                    return "breaker", breaker
                break  # next rung
            assert observed is not None  # model_matches guarantees it
            breaker = 0
            content_no += 1
            exhausted = last_rung and content_no == CONTENT_ATTEMPTS

            try:
                emit = json.loads(result.raw_text)
                if not isinstance(emit, dict):
                    raise ValueError("top level is not an object")
            except ValueError as exc:
                errors = [f"response is not valid JSON: {exc}"]
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="schema_invalid", raw_response=result.raw_text,
                                fed=prior_errors, produced=errors,
                                ladder_exhausted=exhausted, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                prior_errors = errors
                continue
            if schema_errors := validate_emit(emit, SCHEMA_VERSION):
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="schema_invalid", raw_response=result.raw_text,
                                fed=prior_errors, produced=schema_errors,
                                ladder_exhausted=exhausted, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                prior_errors = schema_errors
                continue
            try:
                record = assemble(emit, markdown, document_hash=dh,
                                  normalizer_version=NORMALIZER_VERSION,
                                  observed_model=observed, at=iso(t0))
            except AssembleError as exc:
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="attribution_failed", raw_response=result.raw_text,
                                fed=prior_errors, produced=exc.errors,
                                ladder_exhausted=exhausted, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                prior_errors = exc.errors
                continue
            report = verify(record, markdown)
            findings: list[dict[str, Any]] = [
                {"check": f.check, "path": f.path, "code": f.code,
                 "severity": f.severity, "detail": f.detail}
                for f in report.findings
            ]
            if report.status == "fail":
                errors = [
                    f"{f.check}:{f.code} at {f.path}"
                    for f in report.findings if f.severity == "error"
                ]
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="attribution_failed", raw_response=result.raw_text,
                                fed=prior_errors, produced=errors, findings=findings,
                                ladder_exhausted=exhausted, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                prior_errors = errors
                continue
            archive_attempt(requested_model=model, observed_model=observed, outcome="ok",
                            raw_response=result.raw_text, fed=prior_errors, produced=[],
                            findings=findings, ladder_exhausted=False, started_at=t0,
                            tokens=(result.input_tokens, result.output_tokens),
                            cost=result.cost_usd, record=record)
            return settle_and_disposition()

    return settle_and_disposition()
