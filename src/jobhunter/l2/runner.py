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

Archive I/O never happens inside a transaction (spec amendment [A1]). A GET is
a network round trip, and a managed Postgres kills a session that waits on one
with a transaction open (SQLSTATE 25P03, canary run 33666006472) — the same
kill the engine call already commits around. So `settle` closes the read
transaction before it fetches archived records, and `_catch_up` scans the
archive in committed chunks of `CATCH_UP_CHUNK` keys, holding neither a
transaction nor the chunk's bytes across the next batch.

Nothing here names a prompt, a schema or a validator any more: the six things
that define an engine tuple arrive as a `Bundle` (`l2/bundles.py`), so the
loop — ladder, breaker, caps, catch-up, k-sampling, settle — is the same code
whichever contract is being extracted under.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from itertools import batched
from typing import Any

import psycopg

from jobhunter import __version__
from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.config import Settings
from jobhunter.l2.agreement import cohort_hook
from jobhunter.l2.assemble import AssembleError
from jobhunter.l2.attempts import Attempt, derived_error_detail, from_bytes, to_bytes
from jobhunter.l2.bundles import DEFAULT_BUNDLE, Bundle, get_bundle, get_bundle_for_tuple
from jobhunter.l2.engines import (
    Engine,
    EngineFatalError,
    EngineModelNotFound,
    EngineResult,
    EngineThrottled,
    EngineTransportError,
)
from jobhunter.l2.prompt import PROMPT_VERSION
from jobhunter.l2.schemas import emit_schema, normalize_emit, validate_emit
from jobhunter.l2.state import DerivedState, derive_state, globs_to_regex, model_matches
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.markdown import NORMALIZER_VERSION
from jobhunter.store import db, extraction
from jobhunter.store.extraction import Conn
from jobhunter.timeutil import iso, utcnow_precise

# The v1 engine tuple's public home. `pulse`, `views` and `cli` import
# SCHEMA_VERSION from here, and rebinding these three names pins a run to a
# different tuple without touching the bundle's behaviour (`_pinned`).
SCHEMA_VERSION = "1"
MAX_DOC_CHARS = 60_000
CONTENT_ATTEMPTS = 3
TRANSPORT_RETRIES = 3
BREAKER_LIMIT = 5
# Keys per catch-up batch ([A1]): the scan's transaction and its loaded bytes
# both end at the batch boundary. A 7k-attempt replay held ~500MB when the
# whole scan was materialised at once.
CATCH_UP_CHUNK = 200


def _pinned(bundle: Bundle) -> Bundle:
    """Honour a module-level pin of the v1 identity.

    `PROMPT_VERSION`/`SCHEMA_VERSION`/`VALIDATOR_VERSION` are v1's identity and
    predate the bundle; a version bump is exercised by rebinding them. A v1 run
    therefore takes its identity from this module and its behaviour from the
    bundle. Every other bundle owns both, and the usual case — the names still
    spelling exactly what the v1 bundle carries — returns the bundle untouched.
    """
    here = (PROMPT_VERSION, SCHEMA_VERSION, VALIDATOR_VERSION)
    theirs = (bundle.prompt_version, bundle.schema_version, bundle.validator_version)
    if bundle.name != "v1" or here == theirs:
        return bundle
    return replace(
        bundle, prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
        validator_version=VALIDATOR_VERSION,
    )


def _bundle_for(prompt_version: str | None, schema_version: str | None) -> Bundle:
    """The bundle whose shapes a fold over `(prompt_version, schema_version)` uses.

    Catch-up and replay settle whatever tuples the archive holds, including
    historical prompt versions no bundle claims any more. Those fold under the
    default bundle's shapes — exactly what they did when the shapes were
    constants in this module — while a tuple a bundle does claim folds under
    that bundle, which is what makes a mixed archive replay correctly.
    """
    if prompt_version is not None and schema_version is not None:
        with contextlib.suppress(KeyError):
            return get_bundle_for_tuple(prompt_version, schema_version)
    return _pinned(get_bundle(DEFAULT_BUNDLE))


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
        """Re-apply onto a fresh connection, then re-settle everything it wrote.

        The re-settle is unconditional, not limited to the rows that were
        actually lost: since [A1] an event row can survive a kill its derived
        row does not — `settle` commits the caller's pending insert at its
        archive boundary, and the fold that follows dies with the connection —
        so "the insert was a no-op" no longer implies "its `extractions` row is
        current". Re-settling is a pure re-derivation of rows this run touched
        anyway, and the fold is idempotent.
        """
        touched: set[tuple[str, str, str, str]] = set()
        for attempt in self._attempts.values():
            extraction.record_attempt(conn, attempt, derived_error_detail(attempt))
            touched.add((attempt.document_hash, attempt.prompt_version,
                         attempt.schema_version, attempt.validator_version))
        for event in self._reviews.values():
            extraction.record_review(conn, **event)
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
        except psycopg.errors.IdleInTransactionSessionTimeout:
            # A managed Postgres that kills a session for holding a transaction
            # open too long raises SQLSTATE 25P03, an InternalError — NOT the
            # OperationalError above. It ends the connection exactly like the idle
            # suspend, so recover the same way, but ONLY when the backend is truly
            # gone: a 25P03 on a still-live connection would be a genuine query bug
            # to surface, and reviving on it would loop. The runner also commits
            # before every model call so this timeout should never fire; this is
            # the backstop for any transaction that slips across an engine call.
            if self._connect is None or not self.conn.closed:
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
    # why the run stopped early, when it stopped for a reason the counters do
    # not carry. "lock_lost": the extract lock moved to another writer mid-run,
    # so `lock_held` here does NOT mean nothing happened — docs_attempted and
    # spend_usd already left the account.
    aborted: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "lock_held": self.lock_held,
            "docs_attempted": self.docs_attempted, "validated": self.validated,
            "quarantined": self.quarantined, "pending": self.pending,
            "throttled": self.throttled, "breaker_abort": self.breaker_abort,
            "replayed": self.replayed, "spend_usd": round(self.spend_usd, 5),
            "queued": self.queued, "aborted": self.aborted,
        }


def _ensure_write_once(store: ArchiveStore, bundle: Bundle) -> None:
    pk = keys.x_prompt_key(bundle.prompt_version)
    if not store.exists(pk):
        store.put(pk, bundle.template.encode("utf-8"))
    sk = keys.x_schema_key(bundle.schema_version)
    if not store.exists(sk):
        store.put(
            sk, json.dumps(emit_schema(bundle.schema_version), sort_keys=True).encode("utf-8")
        )


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
    bundle: Bundle | None = None,
) -> DerivedState:
    # call-time resolution: definition-time defaults would freeze the constants
    # and silently ignore a version bump
    active = bundle if bundle is not None else _bundle_for(prompt_version, schema_version)
    prompt_version = active.prompt_version if prompt_version is None else prompt_version
    schema_version = active.schema_version if schema_version is None else schema_version
    validator_version = (
        active.validator_version if validator_version is None else validator_version
    )
    attempts = extraction.attempts_for(
        conn, dh, prompt_version=prompt_version, schema_version=schema_version,
        validator_version=validator_version,
    )
    reviews = extraction.reviews_for(
        conn, dh, prompt_version=prompt_version, schema_version=schema_version,
        validator_version=validator_version,
    )
    # [A1] The transaction ends HERE, before a single archive read. Everything
    # between this line and `upsert_state` is archive traffic — the agreement
    # gate loads each ok sample's record, then the chosen attempt is fetched
    # whole — and a managed Postgres kills a backend that sits idle in a
    # transaction across a round trip (SQLSTATE 25P03, canary run 33666006472).
    # The commit also lands whatever the caller had pending, which is safe for
    # the one thing a caller ever holds here: an insert-only attempt or review
    # row, already in the archive and idempotent on its key. The write below
    # then opens a fresh transaction, so `settle`'s own row still commits
    # atomically with the caller's per-document commit.
    conn.commit()

    # The ONE gate every fold shares (review P0-1): live settlement loads each
    # ok sample's record from its archived attempt object; rebuild passes the
    # same hook over its in-memory re-judged records.
    def _archived_record(a: Attempt) -> dict[str, Any] | None:
        loaded = from_bytes(store.get(a.attempt_key))
        return active.profile_of(loaded.record) if loaded.record is not None else None

    state = derive_state(attempts, reviews, globs, cohort_hook(_archived_record))
    chosen = {a.attempt_key: a for a in attempts}.get(state.chosen_attempt or "")
    model_col = (
        (chosen.observed_model if chosen else None)
        or (attempts[-1].requested_model if attempts else None)
    )
    if model_col is None:
        return state  # nothing decisive ever happened; no row to write
    profile: dict[str, Any] | None = None
    mentions: list[tuple[str, str, str]] | None = None
    if state.status in ("validated", "needs_review") and state.chosen_attempt:
        # the profile is the CHOSEN attempt's archived record — never whatever
        # record the caller happened to hold (a later ok attempt, or nothing)
        chosen_obj = from_bytes(store.get(state.chosen_attempt))
        if chosen_obj.record is not None:
            profile = active.profile_of(chosen_obj.record)
            # the aggregate's rows come from the same record and the same
            # bundle as the blob, so the two can never describe different shapes
            mentions = active.mention_rows(chosen_obj.record)
    extraction.upsert_state(
        conn, document_hash=dh, model=model_col, prompt_version=prompt_version,
        schema_version=schema_version, validator_version=validator_version,
        state=state, profile=profile, mentions=mentions, k=state.k,
        agreement=state.agreement,
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
    # [A1] the scan below is archive traffic: end the watermark read's
    # transaction before the first listing page, and end each batch's with the
    # batch. Chunking is what makes the direct `extraction.record_*` calls safe
    # here — a committed row cannot be rolled back by a later reconnect, which
    # is the whole reason this scan used to write through the journal, and
    # keeping 7k replayed attempts in it cost ~500MB besides. A reconnect
    # mid-scan re-runs `_catch_up` whole; the batches commit in key order and
    # keys sort chronologically, so the new watermark lands before every batch
    # that did not commit and the rescan finds them all again.
    conn.commit()
    replayed = 0
    touched: set[tuple[str, str, str, str]] = set()
    for batch in batched(store.list(keys.X_ATTEMPTS_PREFIX, start_after=start_after),
                         CATCH_UP_CHUNK):
        loaded: list[Attempt] = []
        for key in batch:
            parsed = keys.parse_x_attempt_key(key)
            if parsed is None:
                continue
            if mark is not None and parsed[0] < mark:
                continue
            loaded.append(from_bytes(store.get(key)))
        for attempt in loaded:
            if extraction.record_attempt(conn, attempt, derived_error_detail(attempt)):
                replayed += 1
                touched.add(
                    (attempt.document_hash, attempt.prompt_version, attempt.schema_version,
                     attempt.validator_version)
                )
        conn.commit()
        loaded.clear()  # the batch's bytes go with its transaction
    # review events are archived BEFORE their DB row (archive-as-truth): a crash
    # between the two must not leave a human decision unapplied until a manual
    # rebuild. The prefix is tiny (human verbs), so a full idempotent scan is fine.
    for review_batch in batched(store.list(keys.X_REVIEWS_PREFIX), CATCH_UP_CHUNK):
        events = [json.loads(store.get(key)) for key in review_batch]
        for event in events:
            if extraction.record_review(conn, **event):
                replayed += 1
                touched.add(
                    (event["document_hash"], event["prompt_version"], event["schema_version"],
                     event["validator_version"])
                )
        conn.commit()
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
    bundle: Bundle | None = None,
) -> ExtractSummary:
    # the engine tuple travels as a parameter, never as module state: the
    # parallel drain runs this loop on several threads and the tests re-enter it
    active = _pinned(get_bundle(settings.l2_bundle) if bundle is None else bundle)
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
                c, prompt_version=active.prompt_version, schema_version=active.schema_version,
                validator_version=active.validator_version, model_regex=model_regex,
                normalizer_version=NORMALIZER_VERSION, limit=max_docs,
            )

        if dry_run:
            # strictly read-only: no write-once objects, no catch-up replay
            summary.queued = [only_doc] if only_doc else session.do(queue)
            return summary
        _ensure_write_once(store, active)
        summary.replayed = session.do(lambda c: _catch_up(c, journal, iso(now())))
        session.do(lambda c: c.commit())
        docs = [only_doc] if only_doc else session.do(queue)
        summary.queued = docs
        breaker = 0
        if settings.l2_concurrency <= 1:
            for dh in docs:
                # strict >: a cap of 0 means "free work only" (the documented
                # subscription-backfill mode), not "stop before the first document"
                if summary.docs_attempted >= max_docs or summary.spend_usd > max_usd:
                    break
                result = _extract_doc(settings, session, journal, engine, dh, summary,
                                      breaker, now, active)
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
        else:
            # Parallel drain (T-20260906-SVY4): workers overlap ONLY the engine
            # waits. One gate serializes every DB/journal/summary/breaker touch
            # (released inside _extract_doc strictly around engine.complete), so
            # the single-writer semantics are those of the serial loop. The
            # breaker's "consecutive" is approximate across workers — five
            # rejections with no success between them still abort. A side
            # benefit: some worker is almost always mid-transactionless DB work,
            # so the connection never goes quiet enough for Neon to reap.
            gate = threading.RLock()
            stop = threading.Event()
            doc_iter = iter(docs)
            breaker_box = [breaker]

            def _worker() -> None:
                while not stop.is_set():
                    with gate:
                        if (summary.docs_attempted >= max_docs
                                or summary.spend_usd > max_usd or stop.is_set()):
                            return
                        dh = next(doc_iter, None)
                        b = breaker_box[0]
                    if dh is None:
                        return
                    result = _extract_doc(settings, session, journal, engine, dh,
                                          summary, b, now, active, gate)
                    with gate:
                        if result is None:
                            continue
                        disposition, b_after = result
                        breaker_box[0] = b_after
                        session.do(lambda c: c.commit())
                        if disposition == "validated":
                            summary.validated += 1
                        elif disposition == "quarantined":
                            summary.quarantined += 1
                        elif disposition == "pending":
                            summary.pending += 1
                        elif disposition == "throttled":
                            summary.throttled = True
                            stop.set()
                            return
                        elif disposition == "breaker":
                            summary.breaker_abort = True
                            stop.set()
                            return

            from concurrent.futures import ThreadPoolExecutor

            workers = min(settings.l2_concurrency, max(len(docs), 1))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_worker) for _ in range(workers)]
                for f in futures:
                    f.result()  # a worker's LockLost or bug must surface, not vanish
    except LockLost:
        # another writer owns the drain now; our uncommitted work died with the
        # connection and the archive lets the next run replay it. The counters
        # stay as they are — validated/quarantined/pending are incremented only
        # after their document's commit, and the engine was paid for the rest —
        # but `aborted` has to say so, or "lock_held" reads as "nothing done".
        summary.lock_held = True
        summary.aborted = "lock_lost"
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
    bundle: Bundle,
    gate: threading.RLock | None = None,
) -> tuple[str, int] | None:
    """With a gate (parallel drain): the gate is held for ALL state — DB,
    journal, summary, breaker — and released strictly around engine calls, so
    the semantics stay those of the serial drain while the waiting overlaps."""
    if gate is None:
        return _extract_doc_inner(settings, session, journal, engine, dh, summary,
                                  breaker, now, bundle, None)
    with gate:
        return _extract_doc_inner(settings, session, journal, engine, dh, summary,
                                  breaker, now, bundle, gate)


def _ungated[T](gate: threading.RLock | None, fn: Callable[[], T]) -> T:
    """Run an engine call with the gate released; everything else stays gated."""
    if gate is None:
        return fn()
    gate.release()
    try:
        return fn()
    finally:
        gate.acquire()


def _extract_doc_inner(
    settings: Settings,
    session: _Session,
    journal: _Journal,
    engine: Engine,
    dh: str,
    summary: ExtractSummary,
    breaker: int,
    now: Callable[[], datetime],
    bundle: Bundle,
    gate: threading.RLock | None,
) -> tuple[str, int] | None:
    store = journal.store
    markdown = session.do(lambda c: extraction.markdown_for(c, dh, NORMALIZER_VERSION))
    if markdown is None:
        return None
    summary.docs_attempted += 1
    seq = session.do(lambda c: extraction.next_attempt_no(c, dh)) - 1
    schema = emit_schema(bundle.schema_version)

    def archive_attempt(
        *, requested_model: str, observed_model: str | None, outcome: str,
        raw_response: str | None, fed: list[str], produced: list[str],
        ladder_exhausted: bool, findings: list[dict[str, Any]] | None = None,
        tokens: tuple[int | None, int | None] = (None, None),
        cost: float | None = None, started_at: datetime | None = None,
        record: dict[str, Any] | None = None, sample_slot: int = 1,
    ) -> Attempt:
        # `fed` reproduces the rendered prompt (spec §4.2: prior_errors is the
        # non-reconstructible part of the request); `produced` is what this
        # attempt's validation yielded, stored in the trace + DB error_detail.
        nonlocal seq
        seq += 1
        t0 = started_at or now()
        validation = list(findings or []) + [{"error": e} for e in produced]
        attempt = Attempt(
            attempt_key=keys.x_attempt_key(t0, dh, sample_slot, seq),
            run_id=summary.run_id, cli_version=__version__, document_hash=dh,
            normalizer_version=NORMALIZER_VERSION, sample_slot=sample_slot, attempt_no=seq,
            requested_engine=engine.name, requested_model=requested_model,
            observed_model=observed_model, prompt_version=bundle.prompt_version,
            prompt_sha256=bundle.prompt_sha(), schema_version=bundle.schema_version,
            validator_version=bundle.validator_version, prior_errors=list(fed),
            raw_response=raw_response, validation=validation, outcome=outcome,
            ladder_exhausted=ladder_exhausted, input_tokens=tokens[0],
            output_tokens=tokens[1], cost_usd=cost, started_at=t0.isoformat(),
            finished_at=now().isoformat(), record=record,
        )
        store.put(attempt.attempt_key, to_bytes(attempt))
        session.do(lambda c: journal.record_attempt(c, attempt))
        return attempt

    def settle_and_disposition() -> tuple[str, int]:
        state = session.do(
            lambda c: settle(c, store, dh, settings.l2_models, iso(now()), bundle=bundle)
        )
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
            prompt = bundle.render(markdown, prior_errors)
            # Hold NO open transaction while the model runs (a minute-plus): end the
            # pre-call reads (markdown, next_attempt_no) and any prior attempt's
            # insert-only write here, so the connection is transaction-idle — the
            # idle-suspend shape #7 already reconnects from — not idle-IN-transaction,
            # which a managed Postgres kills with SQLSTATE 25P03 (canary run
            # 33666006472). A bare commit ends the read transaction and is a no-op
            # when nothing is pending; committing attempts early is safe (insert-only
            # + ON CONFLICT DO NOTHING) — `settle`'s [A1] boundary lands the last one
            # the same way — and settle's own row still commits atomically with this
            # document's commit in the caller.
            session.do(lambda c: c.commit())
            try:
                def _call(p: str = prompt, m: str = model) -> EngineResult:
                    return engine.complete(p, schema, m)

                result = _ungated(gate, _call)
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
                try:
                    archive_attempt(requested_model=model, observed_model=None,
                                    outcome="engine_fatal", raw_response=None,
                                    fed=prior_errors, produced=[str(exc)],
                                    ladder_exhausted=False, started_at=t0)
                except LockLost as lost:
                    # bookkeeping for the failure must never become the failure:
                    # run()'s `except LockLost` would turn a 402 into a normal
                    # `lock_held` summary and exit 0 saying "nothing done". The
                    # attempt object is already archived, so the lost row is
                    # re-recorded by a later catch-up scan.
                    raise exc from lost
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
                emit = normalize_emit(emit, bundle.schema_version)
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
            if schema_errors := validate_emit(emit, bundle.schema_version):
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="schema_invalid", raw_response=result.raw_text,
                                fed=prior_errors, produced=schema_errors,
                                ladder_exhausted=exhausted, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                prior_errors = schema_errors
                continue
            try:
                record = bundle.assemble(emit, markdown, document_hash=dh,
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
            report = bundle.verify(record, markdown)
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
            # k-sampling (spec §4.5): 5% deterministic audit by hash slot, plus
            # any document whose slot-1 pass needed a reprompt — the cheapest
            # predictor of a hard document. Samples are single-shot generations
            # under their own slots; the agreement gate settles the verdict.
            audit = settings.l2_audit_mod <= 1 or (
                int(dh[:8], 16) % settings.l2_audit_mod == 0
            )
            reprompted = bool(prior_errors) or content_no > 1
            if audit or reprompted:
                verdict = _take_samples(
                    settings, session, engine, model, markdown, schema,
                    summary, archive_attempt, now, dh, bundle, gate,
                )
                if verdict is not None:
                    return verdict, breaker
            return settle_and_disposition()

    return settle_and_disposition()


def _take_samples(
    settings: Settings,
    session: _Session,
    engine: Engine,
    model: str,
    markdown: str,
    schema: dict[str, Any],
    summary: ExtractSummary,
    archive_attempt: Callable[..., Attempt],
    now: Callable[[], datetime],
    dh: str,
    bundle: Bundle,
    gate: threading.RLock | None = None,
) -> str | None:
    """Slots 2 and 3: one fresh single-shot generation each, archived whatever
    the outcome. A transport failure leaves that slot without a record — the
    agreement hook reads that as sample_failed. Returns "throttled" to abort
    the batch, else None (the caller settles)."""
    for slot in (2, 3):
        t0 = now()
        prompt = bundle.render(markdown, [])
        session.do(lambda c: c.commit())  # transaction-idle while the model runs
        try:
            def _call(p: str = prompt, m: str = model) -> EngineResult:
                return engine.complete(p, schema, m)

            result = _ungated(gate, _call)
        except EngineThrottled as exc:
            archive_attempt(requested_model=model, observed_model=None,
                            outcome="throttled", raw_response=None, fed=[],
                            produced=[str(exc)], ladder_exhausted=False,
                            started_at=t0, sample_slot=slot)
            return "throttled"
        except EngineTransportError as exc:
            archive_attempt(requested_model=model, observed_model=None,
                            outcome="transport", raw_response=None, fed=[],
                            produced=[str(exc)], ladder_exhausted=False,
                            started_at=t0, sample_slot=slot)
            continue
        except (EngineModelNotFound, EngineFatalError) as exc:
            archive_attempt(requested_model=model, observed_model=None,
                            outcome="engine_fatal", raw_response=None, fed=[],
                            produced=[str(exc)], ladder_exhausted=False,
                            started_at=t0, sample_slot=slot)
            continue
        summary.spend_usd += result.cost_usd or 0.0
        observed = result.observed_model
        common: dict[str, Any] = {
            "requested_model": model, "observed_model": observed,
            "raw_response": result.raw_text, "fed": [], "ladder_exhausted": False,
            "started_at": t0, "sample_slot": slot,
            "tokens": (result.input_tokens, result.output_tokens),
            "cost": result.cost_usd,
        }
        if not model_matches(observed, settings.l2_models):
            archive_attempt(outcome="model_rejected",
                            produced=[f"observed model {observed!r} outside globs"],
                            **common)
            continue
        assert observed is not None  # model_matches guarantees it
        try:
            emit = json.loads(result.raw_text)
            if not isinstance(emit, dict):
                raise ValueError("top level is not an object")
            emit = normalize_emit(emit, bundle.schema_version)
        except ValueError as exc:
            archive_attempt(outcome="schema_invalid",
                            produced=[f"response is not valid JSON: {exc}"], **common)
            continue
        if schema_errors := validate_emit(emit, bundle.schema_version):
            archive_attempt(outcome="schema_invalid", produced=schema_errors, **common)
            continue
        try:
            record = bundle.assemble(emit, markdown, document_hash=dh,
                                     normalizer_version=NORMALIZER_VERSION,
                                     observed_model=observed, at=iso(t0))
        except AssembleError as exc:
            archive_attempt(outcome="attribution_failed", produced=exc.errors, **common)
            continue
        report = bundle.verify(record, markdown)
        findings = [
            {"check": f.check, "path": f.path, "code": f.code,
             "severity": f.severity, "detail": f.detail}
            for f in report.findings
        ]
        if report.status == "fail":
            errors = [f"{f.check}:{f.code} at {f.path}"
                      for f in report.findings if f.severity == "error"]
            archive_attempt(outcome="attribution_failed", produced=errors,
                            findings=findings, **common)
            continue
        archive_attempt(outcome="ok", produced=[], findings=findings,
                        record=record, **common)
    return None
