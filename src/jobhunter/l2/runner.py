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
transaction before it fetches archived records, `_extract_doc_inner` closes the
one its pre-flight reads open before the first PUT, and `_catch_up` scans the
archive in committed chunks of `CATCH_UP_CHUNK` keys, holding neither a
transaction nor the chunk's bytes across the next batch.

The parallel drain shares ONE connection, so the property is about the gate as
much as about any single call path: `_extract_doc` commits the document before
it releases the gate, because the worker that takes the gate next goes straight
to the archive. A commit made after re-acquiring the gate would leave
`upsert_state`'s transaction live across someone else's round trip.

A chunk that commits also SETTLES — before the next chunk's first archive read,
and for every tuple it saw, not only the rows it inserted. The watermark is
max(started_at) over committed attempt rows, so a row that lands without its
derived row sits ON the watermark: later scans skip its key, and for the one
key still inside the window `record_attempt` answers "known". Neither signal
says whether the fold ran, so the scan re-derives what it read and every chunk
leaves the surface consistent. The review scan carries the same lesson with a
different bound: `settle`'s boundary commits its caller's decision, so a
decision whose derived row does not post-date it is re-folded whether or not
the scan inserted it.

Nothing here names a prompt, a schema or a validator any more: the six things
that define an engine tuple arrive as a `Bundle` (`l2/bundles.py`), so the
loop — ladder, breaker, caps, catch-up, k-sampling, settle — is the same code
whichever contract is being extracted under. A bundle that carries a semantic
audit phase (spec §4) gets one more step in the same shape: `_audit_candidate`
runs after the samples and BEFORE `settle`, archives its artifact under the
candidate attempt it audited, and settlement reads it back out of the archive.
A bundle without one (v1) skips the step and folds exactly as it always has.

That phase can fail, and a failure that stuck would be permanent: a settled
document is never re-queued for extraction, so its row would carry an
`audit_error` — and everything it contributes would stay out of the aggregate —
for the life of the tuple. Hence validator/18's two additions here. `_reaudit_pass`
is the ONE re-audit spec §5 allows: it finds those documents through the derived
row's `flags`, runs the audit phase again with no extraction and no new sample,
and settles; two errors and the document waits for a human. And every fold now
names the repair the record needs (`_repair_trigger`) — on the document being
drained, for the repair round that runs before settlement, and in `flags`, for
the campaign that comes back for the backlog.

Three rules bound that pass, because a retry budget is exactly the kind of
thing a bad afternoon spends for nothing. A call that never reached the auditor
archives no verdict and costs no pass (`AuditPhase.unreachable`), and a
throttled one ends the run the same way a throttled extraction does. A
candidate whose passes are gone leaves the queue (`audit_retry` in `flags`), or
the spent pool — the oldest rows there are — fills every window. And only a
`validated` row is re-audited: spec §6 lets no automated phase promote a
published terminal state, so a parked disagreement waits for a human `retry`,
while a re-audit that can only restore ELIGIBILITY is the permanence this pass
was built to remove.

The repair round (`_repair_candidate`, spec §4) is what that trigger is for,
and it is a third phase in the same shape: one engine call, one write-once
artifact keyed by the candidate it repaired, and settlement reading the answer
back out of the archive. It runs in the window between the audit and the single
settlement — repairing a candidate before anything is published, never
correcting a record afterwards — and the repaired candidate settles in the base
candidate's place only if it verifies and its own audit is no worse
(`_repair_accepted`). A failed round leaves the base to settle exactly as it
would have. It is not a sample: the cohort never sees it, and no `k` moves.
Its two writes are separately owed, though — the repaired record is archived
before the audit that judges it — so a call that reaches nobody leaves that one
audit for `_reaudit_pass` (`_repair_audit_owed`) instead of orphaning the
candidate the round paid for.

Two passes reach documents the drain is done with, and both are bounded by what
spec §6 allows an automated phase to do. `_reaudit_pass` finishes an audit that
errored; `_repair_pass` takes the repair backlog — `validated` records whose
blocking findings are all that keep them out of the aggregates, one round each,
found through `flags` rather than by probing the archive per row. Neither
touches a parked record: that is a published terminal state, and its exit is a
human.

Every audit probe here reads the ACTIVE bundle's audit version, because the
artifact keys carry it (`keys.x_audit_key`). An artifact written by an earlier
audit version judged the same candidate under a contract that has since been
replaced, so it is history rather than this tuple's verdict: the fold sees no
audit (`not_checked`, never a pass) and the document is owed one. That is what
makes an `AUDIT_VERSION` bump recoverable instead of a no-op — the previous
keying made "audited" mean "some version audited it", so a bump could reach
nothing already settled. Settled rows get there through `_reaudit_pass`'s own
queue, one call per document inside the same per-pass budget an `audit_error`
takes, because the fold records which audit version it answered for
(`flags.audit_version`) and a row whose recorded version is not the active one
is a row that owes an audit. Replay has no key to derive — it re-judges the
candidate — so it reads the version out of each artifact's body instead and
drops the ones no bundle in force would read (`_ArchivedPhases`); the selection
is the same, and so is the answer. Nothing is swept and nothing is rewritten:
the older artifact stays where it was written, the new one lands beside it, and
a row a human has ruled on is still out of reach (spec §6).
"""

from __future__ import annotations

import contextlib
import gzip
import json
import re
import threading
from collections.abc import Callable, Sequence
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
from jobhunter.l2.bundles import (
    DEFAULT_BUNDLE,
    Bundle,
    get_bundle,
    get_bundle_for_tuple,
    registered,
)
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
from jobhunter.l2.state import (
    AuditView,
    DerivedState,
    Review,
    derive_state,
    globs_to_regex,
    model_matches,
)
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.l2.v2 import repair as _v2_repair
from jobhunter.markdown import NORMALIZER_VERSION
from jobhunter.store import db, extraction
from jobhunter.store.extraction import Conn
from jobhunter.timeutil import iso, parse_iso, utcnow_precise

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
# content attempts per k-sample slot (slot 1 keeps CONTENT_ATTEMPTS)
SAMPLE_CONTENT_ATTEMPTS = 2
# Audit calls per candidate (spec §5: "one audit and one subsequent audit" is
# the whole budget — the re-audit is for an audit that ERRORED, never for one
# whose verdict the record dislikes). A completed audit ends the passes at one.
AUDIT_PASSES = 2


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
        # a retired prompt version (a bumped v2 prompt, say) still has a record
        # SHAPE, and the shape is the schema's, not the prompt's: folding a
        # schema-2 record under v1's projections raised KeyError('demand_profile')
        # on the first mixed-archive catch-up. Match by schema before defaulting.
        for name in registered():
            candidate = get_bundle(name)
            if candidate.schema_version == schema_version:
                return candidate
    return _pinned(get_bundle(DEFAULT_BUNDLE))


# --- the retry delta check (plan Task 4) -----------------------------------
# A retry is handed the candidate it is editing (prompt v10) and is asked to
# change only what the errors name. This is the half that checks it got one:
# the top-level collections a retry can quietly empty, walked as (dotted path,
# accessor). Anything here that was in the prior emit, is gone from the retry,
# and is named by none of the fed errors is `retry:unexplained_deletion` — the
# retry-collapse shape the 2026-09-14 analysis found dominating the
# relationship cluster, where the verifier sees a smaller, perfectly clean
# record and has nothing to complain about.
_RETRY_TRACKED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("statements", ("statements",)),
    ("relations.groups", ("relations", "groups")),
    ("relations.conditions", ("relations", "conditions")),
    ("relations.example_sets", ("relations", "example_sets")),
    ("facts.entries", ("facts", "entries")),
    ("mentions", ("mentions",)),
)


def _tracked(emit: dict[str, Any], path: tuple[str, ...]) -> list[Any]:
    node: Any = emit
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return node if isinstance(node, list) else []


def _object_ids(items: list[Any]) -> set[str]:
    return {i["id"] for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)}


def _named_by(errors: list[str], tokens: tuple[str, ...]) -> bool:
    """Whether any fed error names one of `tokens` — an object's id or the path
    it sat at in the prior emit, in either spelling the error strings use
    (`statements[3]` from the verifier and the binder, `statements/3` from the
    JSON-schema validator). Bounded on both sides so `s1` does not match `s10`
    and `facts/entries/1` does not match `facts/entries/12`: a loose match here
    silently disarms the check.
    """
    for token in tokens:
        # hyphen is in the id alphabet (emit schema: ^[A-Za-z0-9_-]{1,40}$),
        # so it bounds nothing: `s1` must not be named by an error about
        # `s1-alt` (2026-09-16 review)
        pattern = rf"(?<![0-9A-Za-z_-]){re.escape(token)}(?![0-9A-Za-z_-])"
        if any(re.search(pattern, error) for error in errors):
            return True
    return False


def unexplained_deletions(
    prior: dict[str, Any], current: dict[str, Any], fed: list[str]
) -> list[str]:
    """Content errors for objects this retry dropped without being asked to.

    `prior` is the last emit that PARSED — the one the retry prompt showed —
    and `fed` is exactly the error list that prompt listed, so the index paths
    in those errors address `prior`'s own objects. An object is a candidate for
    this check when it is usable in `prior` (an object with an id — anything
    else the retry cannot be asked to carry forward) and no fed error names it;
    objects the errors DID name may change or vanish freely, since fixing them
    is what the retry was for.
    """
    errors: list[str] = []
    for dotted, path in _RETRY_TRACKED:
        before = _tracked(prior, path)
        kept = _object_ids(_tracked(current, path))
        for index, item in enumerate(before):
            if not isinstance(item, dict):
                continue
            oid = item.get("id")
            if not isinstance(oid, str) or not oid or oid in kept:
                continue
            here = (oid, f"{dotted}[{index}]", "/".join((*path, str(index))))
            if _named_by(fed, here):
                continue
            errors.append(f"retry:unexplained_deletion at {dotted}[{index}] (id={oid})")
    return errors


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
        # the derived row died with the attempt rows: without this a run can
        # report a validation the store does not hold (death on the per-doc
        # commit, after settle has already written it)
        _settle_all(conn, self.store, self.globs, iso(self._now()), touched)


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
    # documents this run took back through the audit phase after an
    # `audit_error` (`_reaudit_pass`), and documents whose settled verdict asks
    # for a repair round. Neither is an extraction: they never move
    # `docs_attempted`, and their engine calls land in `spend_usd` like any other.
    reaudited: int = 0
    repair_triggered: int = 0
    # repair rounds whose candidate actually settled: a round is asked for on
    # the verdict, spent on the engine, and accepted only if it verifies and
    # audits no worse than the base (`_repair_accepted`), so the three counts
    # are three different facts about the same phase.
    repaired: int = 0
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
            "replayed": self.replayed, "reaudited": self.reaudited,
            "repair_triggered": self.repair_triggered, "repaired": self.repaired,
            "spend_usd": round(self.spend_usd, 5),
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


@dataclass(frozen=True)
class _ArchivedAudit:
    """One archived `semantic-audit/v2` artifact, as settlement reads it.

    `state.AuditView` (the three dimensions eligibility turns on) and
    `state.AuditDetail` (the two lists validator/18 scopes a disagreement
    with) at once. The lists travel because the alternative is settlement
    re-deriving what the judge already decided: severity, dimension and the
    ids each finding bound to are the audit phase's output, archived with it.
    """

    semantics: str
    completeness: str
    blocking: int
    findings: tuple[dict[str, Any], ...] = ()
    unresolved: tuple[dict[str, Any], ...] = ()


#: what a dimension may say in an artifact; anything else reads as `error`
_AUDIT_DIMENSIONS = ("no_findings", "findings", "error")
#: the tail every audit artifact key ends with (`x_audit_key` writes gzip JSON)
_AUDIT_SUFFIX = keys.X_ARTIFACT_SUFFIX


def _audit_bytes(artifact: dict[str, Any]) -> bytes:
    return gzip.compress(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True).encode("utf-8"), mtime=0
    )


def _audit_pass_key(attempt_key: str, audit_version: str | None, pass_no: int) -> str:
    """Where pass `pass_no` of this candidate's audit under `audit_version` is
    archived.

    Pass 1 is `keys.x_audit_key` — which is where the version enters the key —
    and the re-audit lands BESIDE it under a suffixed key in the same namespace.
    Beside, never over: the archive is write-once, and an error that a retry
    overwrote would be an error nothing can account for afterwards (spec §5.6
    archives every phase artifact, not the last one). The version segment sits
    between the candidate and the pass mark, so each (candidate, version, pass)
    has a key of its own and no bump can ever land on another version's verdict.
    """
    key = keys.x_audit_key(attempt_key, audit_version)
    if pass_no <= 1:
        return key
    return f"{key.removesuffix(_AUDIT_SUFFIX)}-p{pass_no}{_AUDIT_SUFFIX}"


def _artifact(store: ArchiveStore, key: str) -> dict[str, Any]:
    """One archived phase artifact, as an object — anything else is nothing."""
    loaded = json.loads(gzip.decompress(store.get(key)))
    return loaded if isinstance(loaded, dict) else {}


def _audit_view(store: ArchiveStore, key: str) -> _ArchivedAudit:
    return _audit_view_of(_artifact(store, key))


def _audit_view_of(artifact: dict[str, Any]) -> _ArchivedAudit:
    """One artifact, read the way the fold reads it.

    Nothing an artifact can say makes a damaged audit a pass: a dimension this
    reader does not recognise — a truncated write, a future vocabulary, a field
    that never made it — is `error` (spec §4: invalid audit output yields
    `audit_error`, not a pass), and an item that is not an object is dropped,
    which leaves the `blocking` count unreconcilable and stops validator/18
    scoping anything against a list it cannot fully see.
    """
    blocking = artifact.get("blocking")
    return _ArchivedAudit(
        semantics=_dimension(artifact.get("semantics")),
        completeness=_dimension(artifact.get("completeness")),
        blocking=blocking if isinstance(blocking, int) and blocking is not True else 0,
        findings=_objects(artifact.get("findings")),
        unresolved=_objects(artifact.get("unresolved")),
    )


def _objects(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _errored(audit: _ArchivedAudit) -> bool:
    return "error" in (audit.semantics, audit.completeness)


def _latest_audit(
    store: ArchiveStore, attempt_key: str, audit_version: str | None
) -> _ArchivedAudit | None:
    """This candidate's verdict under `audit_version`, out of the archive (spec §5).

    Derived keys and an `exists`, never a table: the artifacts live under
    `x_audit_key(candidate_attempt, audit_version)`, so the live drain, the
    catch-up scan and `extract rebuild` read the same answer for the same
    candidate, no migration exists to go wrong, and replaying an audited archive
    costs zero model calls.

    Only THIS version's artifacts answer. An audit written by an older version
    judged the same candidate under a contract the active tuple has replaced, so
    it is history, not the verdict in force: the fold reads no audit at all
    (`not_checked`, never a pass) and the re-audit queue comes back for it. That
    is the whole of "an `AUDIT_VERSION` bump means audit owed".

    The LAST pass is the verdict: a re-audit exists only because the pass before
    it errored, so reading it is what "an error is not the last word" means on
    the read side. An absent first key is `None` (`not_checked` in the fold) —
    never a pass — and the walk stops at the first completed audit, so the
    common case is the one probe and one fetch it always was.
    """
    latest: _ArchivedAudit | None = None
    for pass_no in range(1, AUDIT_PASSES + 1):
        key = _audit_pass_key(attempt_key, audit_version, pass_no)
        if not store.exists(key):
            break
        latest = _audit_view(store, key)
        if not _errored(latest):
            break
    return latest


def _next_audit_pass(
    store: ArchiveStore, attempt_key: str, audit_version: str | None
) -> tuple[int, str] | None:
    """The pass number and key this candidate's next audit artifact takes under
    `audit_version`, or `None` when it has had its passes.

    Three ways to be done, and only one of them spends anything: a completed
    audit is written once (write-once, and re-auditing a verdict until it reads
    better is exactly the backdoor spec §6 forbids), `AUDIT_PASSES` errors is
    the budget, and a candidate nothing has audited yet takes pass 1.

    The budget is per version, because the keys are: a candidate whose v2 passes
    are gone still has its v3 passes, which is what lets a bump recover the
    documents an old contract could not judge. It is not a bigger budget — each
    version is still two passes, and a version is bumped by code, not by a run.
    """
    for pass_no in range(1, AUDIT_PASSES + 1):
        key = _audit_pass_key(attempt_key, audit_version, pass_no)
        if not store.exists(key):
            return pass_no, key
        if not _errored(_audit_view(store, key)):
            return None
    return None


def _dimension(value: Any) -> str:
    return str(value) if value in _AUDIT_DIMENSIONS else "error"


#: the repaired candidate's audit sits in the audit namespace, marked — it is an
#: audit of a DIFFERENT candidate than the base's passes, so it may never take
#: one of their keys (write-once, and a verdict that overwrote another would be
#: a verdict nothing can account for afterwards).
_REPAIRED_MARK = "-r1"


def _repair_audit_key(attempt_key: str, audit_version: str | None) -> str:
    """Where the one audit of this candidate's repaired successor is archived.

    The repair round itself is version-free — one round per candidate, whatever
    judged it — but the verdict ON the repaired candidate is an audit like any
    other, so it composes with the version segment exactly as the passes do
    (`…-s1a2.a3-r1.json.gz`). A bump leaves the old `-r1` where it was and owes
    the repaired candidate a verdict of its own.
    """
    key = keys.x_audit_key(attempt_key, audit_version)
    return f"{key.removesuffix(_AUDIT_SUFFIX)}{_REPAIRED_MARK}{_AUDIT_SUFFIX}"


def _repaired_record(store: ArchiveStore, key: str) -> dict[str, Any] | None:
    """The candidate one archived repair round produced.

    A whole assembled record with its own candidate hash and a
    `parent_candidate_hash` pointing at the base — and present only when the
    operations applied AND the result re-verified. `None` is what "a failed
    repair does not erase the base candidate" (spec §4) looks like from the read
    side: the artifact holds the judge's defect list, and there is simply
    nothing here to publish.
    """
    record = _artifact(store, key).get("record")
    return record if isinstance(record, dict) else None


def _repair_audit_owed(
    store: ArchiveStore, attempt_key: str, audit_version: str | None
) -> bool:
    """Is the candidate a repair round produced still waiting for its one audit?

    A round is TWO archived writes, not one — spec §5 allows "one semantic
    repair round and one subsequent audit" — and only the second of them decides
    which candidate settles. The first is written before the audit call ([A1],
    archive-as-truth), and a call that never reached the auditor archives
    nothing, so a repaired record with no verdict beside it is a round the
    archive shows as UNFINISHED, not as refused.

    Nothing else can see that. The write-once repair key says the round is spent
    (`_unspent` drops the trigger), the base candidate's own audit completed (so
    no pass is owed on it), and `_published` reads a missing audit as no pass and
    settles the base — which is right, and would also be permanent: the repaired
    candidate would be orphaned for the life of the tuple, and the round that
    paid for it wasted. This is the one question that says a pass remains.

    Cheapest probe first, because every fold of every settled row asks it: a
    candidate with no round at all costs one `exists`, a finished round two, and
    only a round whose verdict is missing is worth reading an artifact for.

    "Finished" is per audit version, like every other audit probe: a repaired
    candidate an older version cleared has no verdict the active tuple can
    publish, so its one subsequent audit is owed again. The round is not — the
    repair key carries no version, and no candidate ever gets a second round.
    """
    if not store.exists(keys.x_repair_key(attempt_key)):
        return False  # no round: there is no second candidate to audit
    if store.exists(_repair_audit_key(attempt_key, audit_version)):
        return False  # the round's own verdict is on record; write-once ends it
    # a round the judge refused produced no candidate either, and its base
    # settles exactly as it would have without the round
    return _repaired_record(store, keys.x_repair_key(attempt_key)) is not None


def _repair_accepted(base: _ArchivedAudit | None, repaired: _ArchivedAudit | None) -> bool:
    """Does the repaired candidate settle in the base candidate's place?

    The plan's rule — "repaired if it verifies and audits no-worse, else base" —
    with its verifying half already spent: a candidate that failed `verify` was
    never archived with a record at all. What is left is the auditing half, and
    it is the same bar every published candidate clears: the repaired candidate
    must have been audited, that audit must have COMPLETED (an errored or
    missing one is never a pass, spec §4), and it must leave no more blocking
    work than the findings it was asked to clear. A repair that made things
    worse loses to the candidate it came from — which is still on record,
    audited, and perfectly publishable.
    """
    if repaired is None or _errored(repaired):
        return False
    if base is None or _errored(base):
        return False  # nothing to have been repaired against; the base stands
    return repaired.blocking <= base.blocking


@dataclass(frozen=True)
class _Published:
    """What settlement publishes for one settled candidate: the audit that
    judged it, and the repaired record standing in for the attempt's own when a
    repair round produced one the policy accepts (`record is None` means the
    attempt's own archived record, which is the usual case)."""

    audit: _ArchivedAudit | None = None
    record: dict[str, Any] | None = None


def _published(
    store: ArchiveStore, attempt_key: str, audit_version: str | None
) -> _Published:
    """One candidate's phase artifacts, read the way every fold reads them.

    Derived keys and `exists` probes, exactly like the audit phase, so live
    settlement, the catch-up scan and replay reach the same answer for the same
    candidate with no table and no migration between them. The base candidate's
    verdict stands unless a repair round replaced the candidate itself; then the
    repaired record settles under the repaired candidate's own audit, and the
    base's findings stay in the archive as the history of why.

    Both verdicts are read under the ACTIVE audit version: a repaired candidate
    only settles in the base's place on a verdict the tuple in force can
    publish, so a bump leaves the base settling until the round's audit is
    re-taken, never on the strength of a retired contract's pass.
    """
    base = _latest_audit(store, attempt_key, audit_version)
    key = keys.x_repair_key(attempt_key)
    if not store.exists(key):
        return _Published(audit=base)
    replacement = _repaired_record(store, key)
    if replacement is None:
        return _Published(audit=base)  # the judge refused the patch
    audit_key = _repair_audit_key(attempt_key, audit_version)
    repaired = _audit_view(store, audit_key) if store.exists(audit_key) else None
    if not _repair_accepted(base, repaired):
        return _Published(audit=base)
    return _Published(audit=repaired, record=replacement)


class _Phases:
    """The archived phase artifacts one fold needs, read once per candidate.

    Settlement asks two questions of the same objects — which verdict gates
    this candidate (the fold's probe, once per candidate the agreement gate
    considers) and which record it publishes (the chosen one) — and answering
    them independently would probe a repaired document's four keys twice per
    fold. A catch-up scan over a repaired corpus pays that on every settled
    document, so they are read once here and shared.

    A bundle with no audit phase reads nothing at all: there are no artifacts
    its contract could have produced, and validator/15 folded without a probe.
    """

    def __init__(self, store: ArchiveStore, bundle: Bundle) -> None:
        self._store = store
        self._version = bundle.audit_version
        self._on = bundle.audit_version is not None
        self._seen: dict[str, _Published] = {}

    @property
    def on(self) -> bool:
        return self._on

    def _of(self, attempt_key: str) -> _Published:
        if attempt_key not in self._seen:
            self._seen[attempt_key] = (
                _published(self._store, attempt_key, self._version)
                if self._on else _Published()
            )
        return self._seen[attempt_key]

    def audit(self, attempt_key: str) -> AuditView | None:
        return self._of(attempt_key).audit

    def record(self, attempt_key: str) -> dict[str, Any] | None:
        """The repaired candidate this attempt publishes, or None for its own."""
        return self._of(attempt_key).record


def _artifact_audit_version(artifact: dict[str, Any]) -> str:
    """Which audit contract wrote this artifact.

    Every artifact the phase writes records its own `audit_version`, and one
    that does not is an artifact from before the phase was versioned — which
    the bare key it sits at says is a `semantic-audit/v2`
    (`keys.LEGACY_AUDIT_VERSION`), by the same definition the key derivation
    uses. Read from the BODY, because replay reaches artifacts by scanning a
    namespace rather than by deriving the key it wants.
    """
    version = artifact.get("audit_version")
    return version if isinstance(version, str) and version else keys.LEGACY_AUDIT_VERSION


def _audit_versions_in_force() -> frozenset[str]:
    """Every audit contract some registered bundle would read at today.

    A version no bundle names has been superseded, and its verdicts are history
    — exactly what the live path expresses by deriving the active version's key
    and finding nothing at it. The SET is what replay can use, because it holds
    one scan for a whole mixed corpus, and it is not looser than the live rule:
    a candidate hash covers the tuple that sealed it, so an artifact written by
    another bundle's audit version can only ever join that bundle's candidates.
    """
    return frozenset(
        version
        for version in (get_bundle(name).audit_version for name in registered())
        if version is not None
    )


class _ArchivedPhases:
    """Replay's `_Phases`: every audit and repair artifact, by CANDIDATE HASH.

    The live path derives an artifact's key from the attempt it belongs to.
    Replay cannot, and must not: it re-judges each archived response under
    today's validators, so what it holds is a re-derived candidate, and the
    only thing that says an audit describes THAT candidate is the candidate
    hash the audit itself names. The join is the hash in both directions — a
    candidate the archive already audited keeps its verdict for free (which is
    what makes re-settling a corpus under a new policy cost zero model calls),
    and one today's validators changed settles honestly unaudited rather than
    borrowing the verdict of a candidate that no longer exists.

    The hash says which candidate an artifact describes and nothing about which
    contract judged it, so the version is the second half of the selection here:
    an artifact of a retired audit version is skipped, and the candidate settles
    unaudited — the same answer the live path reaches by deriving the active
    version's key. Without it `extract rebuild` would publish a retired pass as
    the verdict in force and stamp the row with an active version nothing read,
    which takes the document out of the re-audit queue for good: one replay
    after a bump would strand precisely the documents the bump exists to
    recover.

    One scan per namespace, and only what settlement reads is kept.
    """

    def __init__(self, store: ArchiveStore) -> None:
        self._audits: dict[str, _ArchivedAudit] = {}
        self._repairs: dict[str, dict[str, Any]] = {}
        in_force = _audit_versions_in_force()
        for key in store.list(keys.X_AUDITS_PREFIX):
            artifact = _artifact(store, key)
            candidate = artifact.get("candidate_hash")
            if not isinstance(candidate, str) or not candidate:
                continue  # an artifact that names no candidate describes none
            if _artifact_audit_version(artifact) not in in_force:
                continue  # a retired contract's verdict: audit owed, not done
            view = _audit_view_of(artifact)
            # the live walk's rule, without the pass order (artifact keys sort
            # by suffix, not by pass): a COMPLETED audit is the verdict, and a
            # candidate has at most one, so errors only ever stand in for each
            # other — and an errored view says the same thing either way.
            standing = self._audits.get(candidate)
            if standing is None or (_errored(standing) and not _errored(view)):
                self._audits[candidate] = view
        for key in store.list(keys.X_REPAIRS_PREFIX):
            artifact = _artifact(store, key)
            base = artifact.get("candidate_hash")
            record = artifact.get("record")
            if isinstance(base, str) and base and isinstance(record, dict):
                self._repairs[base] = record

    def published(self, record: dict[str, Any] | None) -> _Published:
        """What this re-derived candidate publishes — `_published`'s rules, read
        by hash instead of by key."""
        if record is None:
            return _Published()
        base = self._audits.get(_hash_of(record))
        replacement = self._repairs.get(_hash_of(record))
        if replacement is None:
            return _Published(audit=base)
        repaired = self._audits.get(_hash_of(replacement))
        if not _repair_accepted(base, repaired):
            return _Published(audit=base)
        return _Published(audit=repaired, record=replacement)


def _hash_of(record: dict[str, Any]) -> str:
    """A candidate's own hash, or "" for a record shape that has none (v1)."""
    return str((record.get("extraction") or {}).get("candidate_hash") or "")


@dataclass(frozen=True)
class _RepairContract:
    """The `semantic-repair/v1` pieces the repair phase drives (spec §4)."""

    version: str
    render: Callable[[str, str, dict[str, Any], list[dict[str, Any]]], str]
    emit_schema: Callable[[], dict[str, Any]]
    apply: Callable[[dict[str, Any], dict[str, Any], str, str], dict[str, Any]]


#: Repair contracts by SCHEMA version, because that is what a typed repair
#: operation addresses: the operations name schema-2 objects (`statement`,
#: `fact_entry`, `presence`) and carry the old-object hashes of that record
#: shape, so the contract belongs to the shape rather than to a prompt or a
#: validator — the same reason `_bundle_for` falls back to matching by schema.
#: A bundle with an audit phase but no contract for its shape simply never
#: repairs; its findings still gate eligibility, exactly as they did.
_REPAIR_CONTRACTS: dict[str, _RepairContract] = {
    "2": _RepairContract(
        version=_v2_repair.REPAIR_VERSION,
        render=_v2_repair.render,
        emit_schema=_v2_repair.emit_schema,
        apply=_v2_repair.apply,
    ),
}


def _repair_contract(bundle: Bundle) -> _RepairContract | None:
    if bundle.audit_version is None:
        return None  # no audit, no validated findings, nothing to repair against
    return _REPAIR_CONTRACTS.get(bundle.schema_version)


def _judge_errors(exc: Exception) -> list[str]:
    """Every concrete defect a judge collected, or the exception itself.

    `RepairJudgeError` carries the whole list at once (assemble.py's discipline,
    and what makes an archived failure legible); anything else is a bug in the
    phase, and it must reach the artifact by name rather than hide behind an
    outcome.
    """
    found = getattr(exc, "errors", None)
    if isinstance(found, list):
        return [error for error in found if isinstance(error, str)]
    return [f"{type(exc).__name__}: {exc}"]


#: why a repair round was asked for (spec §4 Repair, validator/18)
REPAIR_DISPUTE = "dispute"  # a disagreement the audit's findings landed on
REPAIR_ELIGIBILITY = "eligibility"  # a settled record blocking findings keep out
#: what a dimension says once its audit COMPLETED; `error` is never a pass, and
#: `not_checked` is a phase that never ran
_AUDIT_COMPLETED = ("no_findings", "findings")
#: review verbs that leave a decision standing (`retry` clears instead)
_RULING_VERBS = ("accept", "reject", "flag", "refute")


@dataclass(frozen=True)
class RepairTrigger:
    """One candidate the bounded repair round (spec §4/§6) is asked to fix.

    Identity and reason only: the findings to repair against are the ones in
    that candidate's archived audit, and the repair phase reads them from the
    archive rather than carrying a copy through settlement.
    """

    document_hash: str
    attempt_key: str
    reason: str  # REPAIR_DISPUTE | REPAIR_ELIGIBILITY


#: an audit call that never reached the auditor. `throttled` is the provider
#: asking for silence — the run stops on it, exactly as a throttled extraction
#: stops it; `unreachable` is everything else that produced no response at all
#: (transport, a refused request, a model the provider does not have).
AUDIT_THROTTLED = "throttled"
AUDIT_UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class AuditPhase:
    """What one turn through the audit phase leaves for the run to act on.

    Two independent facts, because they ask for opposite things. `trigger` is a
    verdict the auditor reached and the repair round is asked to fix.
    `unreachable` is the auditor never answering: no verdict, no artifact, and
    NONE of the candidate's passes spent — the archive is write-once, so an
    `audit_error` written for a dead socket would be a verdict the retry could
    never replace, and a rate-limit storm at the head of a batch would spend
    the one retry of every document in it.

    That is the drain's own rule, not a new one: a transport failure there
    archives its attempt but never advances the content ladder (spec §4.2), so
    a provider having a bad minute costs the document nothing it cannot get
    back.
    """

    trigger: RepairTrigger | None = None
    unreachable: str | None = None  # AUDIT_THROTTLED | AUDIT_UNREACHABLE


def _human_ruled(reviews: Sequence[Review]) -> bool:
    """Is a review decision standing on this document?

    The fold's own rule, read from outside it: a `retry` starts a fresh cohort
    and clears everything before it, and any other verb is a ruling that stands
    until one does. `human_review` alone cannot answer this — a `flag` or a
    `refute` parks a record without changing it (spec §6 knows none/accepted/
    rejected only) — and the repair loop must no more touch a record someone
    parked than one they accepted (spec §6: the loop "operates before terminal
    settlement, not as a backdoor around the human-only promotion rule").
    """
    for review in sorted(reviews, key=lambda r: (parse_iso(r.at), r.key), reverse=True):
        if review.verb == "retry":
            return False
        if review.verb in _RULING_VERBS:
            return True
    return False


def _repair_trigger(
    dh: str, state: DerivedState, reviews: Sequence[Review]
) -> RepairTrigger | None:
    """Which candidate the repair phase is asked for (validator/18 policy).

    Two triggers, both read off the fold's own verdict so the policy is stated
    once, in `state.py`, and restated nowhere:

    1. a disagreeing cohort still parked after a COMPLETED audit. Under
       validator/18 a completed audit whose blocking findings all miss the
       dispute adjudicates the cohort, so one that is still `needs_review` is
       one whose findings landed ON what the samples split over — the
       blocking-on-disputed case the 2026-09-13 investigation counted 121 of.
    2. anything that settled `validated` carrying blocking work, which is
       exactly what holds it out of the aggregates (eligibility repair).

    Never from an error (an `audit_error` carries no validated findings to
    repair against — it gets a re-audit, not a repair), never without a
    candidate, never on a document a review decision is standing on, and never
    for an incomplete cohort: `sample_failed` is a missing sample, and no
    rewrite of the candidate produces one.
    """
    if state.chosen_attempt is None or _human_ruled(reviews):
        return None
    if state.semantics not in _AUDIT_COMPLETED or state.completeness not in _AUDIT_COMPLETED:
        return None
    if state.blocking <= 0:
        return None
    if state.status == "needs_review" and state.sampling == "disagreement":
        return RepairTrigger(dh, state.chosen_attempt, REPAIR_DISPUTE)
    if state.status == "validated":
        return RepairTrigger(dh, state.chosen_attempt, REPAIR_ELIGIBILITY)
    return None


def _unspent(store: ArchiveStore, trigger: RepairTrigger | None) -> RepairTrigger | None:
    """The trigger, unless this candidate has already taken its one round.

    The archive is the cap (spec §5: one repair round per candidate, under a
    write-once key), so the question is one `exists` — and it is asked wherever
    a trigger is reported, so a round that is gone is never counted, queued or
    asked for twice. A row that went on carrying `repair` in its `flags` would
    be worse than noise: its `updated_at` froze on the fold that spent the
    round, so it is among the oldest rows the campaign can see and would fill
    every window ahead of the documents something can still be done for.
    """
    if trigger is None or not store.exists(keys.x_repair_key(trigger.attempt_key)):
        return trigger
    return None


def _flags(
    state: DerivedState,
    active: Bundle,
    trigger: RepairTrigger | None,
    retry_owed: bool = False,
) -> dict[str, Any] | None:
    """The derived row's scheduling surface: what a later run has to come back
    for, written by the same fold that decided it.

    `audit` is the phase's status on this candidate (`ok`/`error`/
    `not_checked`), `audit_version` is the audit contract this fold selected
    under, `audit_retry` says that status can still change, and `repair` is the
    trigger's reason when there is one, so both passes that revisit settled
    documents — the re-audit here, the repair campaign next — find their work in
    one indexed query instead of an archive probe per settled row. It is
    derived, like every other column this module writes: a rebuild reproduces
    it, and a row folded before it existed simply has none, which is what keeps
    a silent backfill out of the drain.

    `audit_version` is what makes an `AUDIT_VERSION` bump reach the rows that
    are already settled. Their `audit: ok` was true of the contract that judged
    them and is not true of the one in force, and no probe of the archive can
    tell the queue that without a GET per settled row — so the fold records
    which contract it answered for, and a row whose recorded version is not the
    active one is a row that owes an audit (`_reaudit_queue`). The active
    version is the honest answer because it is the only one any fold can read:
    the live path derives that version's keys (`_published`) and replay skips
    every artifact another version wrote (`_ArchivedPhases`), so the stamp names
    the contract behind the verdict rather than merely the one in force.

    `audit_retry` is the one thing the status alone cannot say. "Error" is true
    of a candidate owed a retry and of one whose two passes are gone, and the
    difference is the whole scheduling question: a row that cannot tell them
    apart goes on matching the re-audit queue forever, and — its `updated_at`
    frozen on the fold that spent the budget — sorts ahead of every newer error
    until the spent pool fills the window and the pass stops re-auditing
    anything. It is written only while a pass remains, so its ABSENCE is what a
    finished budget looks like, and a row folded before the key existed asks
    for nothing. The status cannot say it for a repaired candidate either: the
    audit column describes the BASE candidate's verdict, and `ok` there is true
    both of a finished round and of one whose subsequent audit never reached the
    auditor (`_repair_audit_owed`).

    A bundle with no audit phase has none of them, so a v1 row is what it
    always was.
    """
    if active.audit_version is None:
        return None
    if (state.semantics, state.completeness) == ("not_checked", "not_checked"):
        audit = "not_checked"
    elif "error" in (state.semantics, state.completeness):
        audit = "error"
    else:
        audit = "ok"
    flags: dict[str, Any] = {"audit": audit, "audit_version": active.audit_version}
    if retry_owed:
        flags["audit_retry"] = True
    if trigger is not None:
        flags["repair"] = trigger.reason
    return flags


def _audit_retry_owed(store: ArchiveStore, state: DerivedState, active: Bundle) -> bool:
    """Does this settled candidate still have an audit pass to take?

    Costs nothing in one common case: a fold that saw no artifact at all has an
    unspent pass 1 by definition (the phase never ran — a run that died between
    the attempt and the audit, or a provider the audit could not reach). An
    ERROR asks the archive for the one probe that says whether the second pass
    is still free, and a COMPLETED audit asks it whether a repair round left its
    own audit owed — the base candidate's verdict is written once and never
    re-run, but the round's second write is a pass of its own, and the fold has
    no other way to know it is missing.

    It reports the archive's answer whatever the lifecycle says; which rows may
    act on it is the queue's question, and `_reaudit_queue` answers it with spec
    §6's publication boundary.
    """
    if active.audit_version is None or state.chosen_attempt is None:
        return False
    if (state.semantics, state.completeness) == ("not_checked", "not_checked"):
        return True
    if "error" in (state.semantics, state.completeness):
        return _next_audit_pass(store, state.chosen_attempt, active.audit_version) is not None
    return _repair_audit_owed(store, state.chosen_attempt, active.audit_version)


def _settled(record: dict[str, Any], state: DerivedState) -> dict[str, Any]:
    """The chosen candidate's record, carrying the verdict this fold reached.

    The stored profile has to describe the record AFTER settlement: the audit
    phase owns `semantics`/`completeness`, the cohort owns `sampling`, the
    review stream owns `human_review` and the lifecycle, and eligibility is
    derived from all of them at once. None of that exists when `assemble` seals
    a candidate — every one of them is an output of THIS fold — so it travels
    to the bundle's projections as data, under one reserved key.

    Data, and not a second projection argument, because only the bundle knows
    whether its contract has a quality object at all: v1 has none and its
    projections never look for the key, v2 re-derives its seven dimensions from
    it (`l2/v2/serve.SETTLEMENT`, read by `serve.quality_of`). The copy is
    shallow and the key is consumed by the projections, so neither the archived
    record nor the stored blob ever carries it.
    """
    return {
        **record,
        # the name is `l2/v2/serve.SETTLEMENT`; spelled literally here so the
        # generic loop keeps importing no contract-specific module
        "settlement": {
            "lifecycle": state.status,
            "sampling": state.sampling,
            "semantics": state.semantics,
            "completeness": state.completeness,
            # blocking findings PLUS blocking unresolved questions: spec §6
            # gates on "no blocking findings or blocking unresolved fields"
            "blocking": state.blocking,
            "human_review": state.human_review,
        },
    }


def _fold(
    conn: Conn,
    store: ArchiveStore,
    dh: str,
    globs: tuple[str, ...],
    active: Bundle,
    prompt_version: str,
    schema_version: str,
    validator_version: str,
) -> tuple[list[Attempt], list[Review], DerivedState, _Phases]:
    """This document's events under one engine tuple, folded through the one
    shared gate — and the [A1] boundary that ends the read transaction.

    `settle` and the audit phase both derive through here, which is what makes
    the candidate the runner audits the candidate settlement publishes: one
    agreement implementation, one policy, called twice over the same events.
    """
    attempts = extraction.attempts_for(
        conn, dh, prompt_version=prompt_version, schema_version=schema_version,
        validator_version=validator_version,
    )
    # a replayed corpus keeps attempt rows at their ARCHIVED validator while
    # its derived rows live at the active one; fold the compat versions in
    # (event order restored below) or the live path and the rebuild disagree
    # about the same document — the invariant both exist to share
    if validator_version == active.validator_version:
        for compat in active.compat_validators:
            attempts += extraction.attempts_for(
                conn, dh, prompt_version=prompt_version, schema_version=schema_version,
                validator_version=compat,
            )
        attempts.sort(key=lambda a: (a.started_at, a.attempt_no))
    reviews = extraction.reviews_for(
        conn, dh, prompt_version=prompt_version, schema_version=schema_version,
        validator_version=validator_version,
    )
    # [A1] The transaction ends HERE, before a single archive read. Everything
    # between this line and `settle`'s `upsert_state` is archive traffic — the
    # agreement gate loads each ok sample's record, the audit probe checks one
    # key, then the chosen attempt is fetched whole — and a managed Postgres
    # kills a backend that sits idle in a transaction across a round trip
    # (SQLSTATE 25P03, canary run 33666006472).
    # The commit also lands whatever the caller had pending: an insert-only
    # attempt or review row, already in the archive and idempotent on its key.
    # `upsert_state` then opens a fresh transaction, so `settle`'s own row
    # still commits atomically with the caller's per-document commit — but the
    # caller's row and this fold are no longer one transaction, and a death in
    # between leaves the event committed and its derived row stale. Nothing
    # here can close that gap (one connection cannot both hold the caller's
    # write and be transaction-idle), so `_catch_up` heals it instead, on both
    # sides: the attempt scan folds every tuple in its watermark window, and
    # the review scan folds every decision its derived row does not post-date.
    conn.commit()

    # The ONE gate every fold shares (review P0-1): live settlement loads each
    # ok sample's record from its archived attempt object; rebuild passes the
    # same hook over its in-memory re-judged records.
    def _archived_record(a: Attempt) -> dict[str, Any] | None:
        # the COHORT is the extraction samples and nothing else: a repaired
        # candidate is not a sample (spec §5) and never enters the comparison,
        # however it settles.
        loaded = from_bytes(store.get(a.attempt_key))
        return active.profile_of(loaded.record) if loaded.record is not None else None

    phases = _Phases(store, active)
    state = derive_state(
        attempts, reviews, globs,
        cohort_hook(_archived_record, f1_min=active.agreement_f1_min),
        # a bundle with no audit phase folds with no probe at all, which is
        # validator/15 byte for byte — the v1 corpus never touches the archive
        # for an artifact its contract cannot produce
        phases.audit if phases.on else None,
    )
    return attempts, reviews, state, phases


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
    attempts, reviews, state, phases = _fold(
        conn, store, dh, globs, active, prompt_version, schema_version, validator_version
    )

    def _record_of(attempt_key: str) -> dict[str, Any] | None:
        # the profile is the CHOSEN attempt's archived record — never whatever
        # record the caller happened to hold (a later ok attempt, or nothing) —
        # unless a repair round replaced the candidate, in which case the
        # repaired record archived beside it is the candidate that settles.
        repaired = phases.record(attempt_key)
        return repaired if repaired is not None else from_bytes(store.get(attempt_key)).record

    _upsert_fold(
        conn, store, dh, active, prompt_version=prompt_version,
        schema_version=schema_version, validator_version=validator_version,
        attempts=attempts, reviews=reviews, state=state, updated_at=updated_at,
        record_of=_record_of,
    )
    return state


def _upsert_fold(
    conn: Conn,
    store: ArchiveStore,
    dh: str,
    active: Bundle,
    *,
    prompt_version: str,
    schema_version: str,
    validator_version: str,
    attempts: Sequence[Attempt],
    reviews: Sequence[Review],
    state: DerivedState,
    updated_at: str,
    record_of: Callable[[str], dict[str, Any] | None],
) -> None:
    """Write the derived row one fold decided — the only place a `DerivedState`
    becomes a row in `extractions`.

    Live settlement and `extract rebuild` both end here, which is what makes a
    rebuilt surface row-for-row identical to the incrementally-built one: same
    projections, same settlement overlay, same scheduling flags, one
    implementation. They differ in one argument — where the chosen candidate's
    record comes from: the archived attempt on the live path, the in-memory
    re-judged record on replay, and the accepted repaired candidate in place of
    either.
    """
    chosen = {a.attempt_key: a for a in attempts}.get(state.chosen_attempt or "")
    model_col = (
        (chosen.observed_model if chosen else None)
        or (attempts[-1].requested_model if attempts else None)
    )
    if model_col is None:
        return  # nothing decisive ever happened; no row to write
    profile: dict[str, Any] | None = None
    mentions: list[tuple[str, str, str]] | None = None
    if state.status in ("validated", "needs_review") and state.chosen_attempt:
        # projected UNDER this fold's verdict, so what is stored is the record
        # as settlement leaves it, audit dimensions included
        record = record_of(state.chosen_attempt)
        if record is not None:
            settled = _settled(record, state)
            profile = active.profile_of(settled)
            # the aggregate's rows come from the same record, the same verdict
            # and the same bundle as the blob, so the two can never disagree
            # about what is eligible
            mentions = active.mention_rows(settled)
    trigger = _repair_trigger(dh, state, reviews)
    if active.audit_version is not None:
        # a tuple with no audit phase has no repair artifacts to probe for
        trigger = _unspent(store, trigger)
    extraction.upsert_state(
        conn, document_hash=dh, model=model_col, prompt_version=prompt_version,
        schema_version=schema_version, validator_version=validator_version,
        state=state, profile=profile, mentions=mentions, k=state.k,
        agreement=state.agreement,
        # what the next run has to come back for, decided by THIS fold: every
        # caller settles through here, so a row can never be left claiming work
        # its own verdict does not ask for
        flags=_flags(state, active, trigger, _audit_retry_owed(store, state, active)),
        reviewed_by=reviews[-1].actor if reviews else None,
        updated_at=updated_at,
    )


def _settle_all(
    conn: Conn,
    store: ArchiveStore,
    globs: tuple[str, ...],
    updated_at: str,
    touched: set[tuple[str, str, str, str]],
) -> None:
    """Fold every `(document, prompt, schema, validator)` tuple in `touched`.

    Sorted so a replay's writes land in a deterministic order, and so two runs
    folding the same set take the same path through it.
    """
    for dh, pv, sv, vv in sorted(touched):
        settle(conn, store, dh, globs, updated_at,
               prompt_version=pv, schema_version=sv, validator_version=vv)


def _folded_at(
    conn: Conn, documents: set[str]
) -> dict[tuple[str, str, str, str], datetime]:
    """When each engine tuple of these documents was last folded.

    The review scan's staleness test, and the review scan's alone — it is the
    scan's own bookkeeping over a derived table, not part of the extraction
    write path, so it reads `extractions` here rather than growing a helper on
    the write module every other caller would inherit.
    """
    if not documents:
        return {}
    rows = conn.execute(
        "SELECT document_hash, prompt_version, schema_version, validator_version,"
        " max(updated_at) AS updated_at FROM extractions WHERE document_hash = ANY(%s)"
        " GROUP BY 1, 2, 3, 4",
        (sorted(documents),),
    ).fetchall()
    return {
        (r["document_hash"], r["prompt_version"], r["schema_version"],
         r["validator_version"]): r["updated_at"]
        for r in rows
    }


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
    #
    # Each batch also FOLDS before the next batch's first GET, and it folds
    # every tuple it SAW, not only the rows it inserted. Both halves are the
    # same lesson: a committed attempt row whose derived row never landed is
    # unreachable afterwards — the watermark sits on it, so the next scan skips
    # its key, and `record_attempt` answering "known" for the one key still in
    # the window would drop it from the fold set. "The row was already there"
    # says nothing about whether the fold ran, and `settle` commits its rows at
    # its [A1] boundary before it reads the archive, so the gap is real: the
    # kill that motivated [A1] lands squarely inside it. Re-folding is a pure
    # re-derivation over committed rows; the window is bounded by the watermark
    # (one second of keys in the steady state), so the cost is one redundant
    # fold per run and a refreshed `updated_at` on that document.
    conn.commit()
    replayed = 0
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
        touched: set[tuple[str, str, str, str]] = set()
        for attempt in loaded:
            if extraction.record_attempt(conn, attempt, derived_error_detail(attempt)):
                replayed += 1  # counted for the caller: only a real replay is news
            touched.add(
                (attempt.document_hash, attempt.prompt_version, attempt.schema_version,
                 attempt.validator_version)
            )
        _settle_all(conn, store, globs, updated_at, touched)
        # ends the last fold's write — or, for a batch that folded nothing, the
        # transaction the no-op inserts opened — before the next batch's GETs
        conn.commit()
        loaded.clear()  # the batch's bytes go with its transaction
    # review events are archived BEFORE their DB row (archive-as-truth): a crash
    # between the two must not leave a human decision unapplied until a manual
    # rebuild. The prefix is tiny (human verbs), so a full idempotent scan is
    # fine. Unlike the attempt scan this one has no watermark to bound it, but
    # "the row was already there" says nothing about whether the FOLD ran, and
    # here it says even less than on the attempt side: `settle` lands the
    # caller's pending review insert at its [A1] boundary and only then reads
    # the archive, so a death in that gap — the `extract review` verbs and the
    # refuter both write in exactly that shape — leaves the decision committed
    # with its derived row untouched, unreachable by every later scan.
    #
    # The derived row's own `updated_at` is the bound: a fold that finished
    # post-dates the decision it folded (its callers stamp it after the event),
    # so anything it does not post-date is re-derived and everything else is
    # left alone. Untouched matters — `oldest_review_at`, how long the queue has
    # really been waiting, is min(updated_at) over the rows awaiting a human, so
    # a scan that re-folded every reviewed document would erase it. A tuple with
    # no row at all is not this failure (a review is only ever issued against an
    # existing row); it is a fold that already ran and settled to pending.
    for review_batch in batched(store.list(keys.X_REVIEWS_PREFIX), CATCH_UP_CHUNK):
        events = [json.loads(store.get(key)) for key in review_batch]
        folded = _folded_at(conn, {e["document_hash"] for e in events})
        touched = set()
        for event in events:
            config = (event["document_hash"], event["prompt_version"],
                      event["schema_version"], event["validator_version"])
            if extraction.record_review(conn, **event):
                replayed += 1
                touched.add(config)
            elif config in folded and folded[config] <= parse_iso(event["at"]):
                touched.add(config)  # committed decision, fold never finished
        _settle_all(conn, store, globs, updated_at, touched)
        conn.commit()
    return replayed


def _reaudit_queue(
    conn: Conn,
    *,
    prompt_version: str,
    schema_version: str,
    validator_version: str,
    audit_version: str,
    model_regex: str,
    only_doc: str | None,
    limit: int,
) -> list[tuple[str, str]]:
    """Settled documents whose audit is owed, oldest fold first.

    The derived row's own `flags` is the index — `settle` writes the audit
    phase's status, the audit version it answered for, and whether a pass
    remains there on every fold — so finding this work costs one query instead
    of an archive probe per settled document. A row folded before those flags
    existed carries none and is not retried: the pass fills as the corpus is
    re-folded, which is what keeps a silent backfill out of the drain.

    Three conditions, and each of them is a rule this pass exists to obey.

    `audit_retry` is the budget: a candidate whose two passes are gone must
    leave the window, or the spent pool — oldest rows in the table, because
    their `updated_at` froze on the fold that recorded the second error — fills
    every `LIMIT max_docs` window and the pass stops re-auditing corpus-wide.

    A recorded audit version that is not the active one is the second kind of
    owed audit, and it is the one an `AUDIT_VERSION` bump creates: the row's
    `audit: ok` describes a contract that has been replaced, the artifact behind
    it is not at the key the tuple in force reads, and nothing else in the table
    can say so. Such a row takes exactly the path an `audit_error` takes — this
    same window, this same per-pass budget, one call each — never a blanket
    sweep of the corpus.

    `status = 'validated'` is the publication boundary (spec §6: "Once a
    terminal needs-review/quarantined/rejected state has been published,
    automated subsequent samples or audits cannot promote it"). A `validated`
    row's re-audit can only move ELIGIBILITY — the status is already what a
    clean audit would leave — which is exactly the permanence this pass exists
    to remove. A parked row's cannot: a person may already be triaging that
    disagreement, and an automated audit clearing it would promote a published
    terminal state into the aggregates behind them. Their exit is a human
    `retry`, which starts a fresh cohort with a fresh audit.

    That line runs through a repair round's own audit too (`_repair_audit_owed`,
    the other work `audit_retry` stands for): finishing one settles the repaired
    candidate in the base's place, which on a `validated` row can only move
    eligibility and on a parked one would be exactly the promotion spec §6
    forbids. The row goes on saying honestly that the audit is owed; this is
    where it is refused.
    """
    rows = conn.execute(
        """
        SELECT document_hash, chosen_attempt FROM extractions
        WHERE prompt_version = %(pv)s AND schema_version = %(sv)s
          AND validator_version = %(vv)s AND model ~ %(model_regex)s
          AND chosen_attempt IS NOT NULL AND status = 'validated'
          AND (
            flags->>'audit_retry' = 'true'
            OR (flags->'audit' IS NOT NULL
                AND flags->>'audit_version' IS DISTINCT FROM %(av)s)
          )
          AND (%(only)s::text IS NULL OR document_hash = %(only)s)
        ORDER BY updated_at, document_hash
        LIMIT %(limit)s
        """,
        {
            "pv": prompt_version, "sv": schema_version, "vv": validator_version,
            "av": audit_version, "model_regex": model_regex,
            "only": only_doc, "limit": limit,
        },
    ).fetchall()
    return [(r["document_hash"], r["chosen_attempt"]) for r in rows]


def _drive_pass(
    settings: Settings,
    summary: ExtractSummary,
    queued: list[tuple[str, str]],
    take: Callable[[tuple[str, str], threading.RLock | None], str | None],
    max_usd: float,
    breaker_abort: str,
) -> str | None:
    """Run one pre-drain pass's queue under the drain's own concurrency.

    The policy is the serial loop's, verbatim: the money cap is checked before
    each item (strict >, so a cap of 0 is "free work only"), `BREAKER_LIMIT`
    consecutive unanswered calls abort with `breaker_abort`, a throttle aborts
    with `AUDIT_THROTTLED`, and an answered item resets the streak. What the
    parallel branch adds is the drain's worker shape (2026-09-19): each
    worker holds the shared gate for ALL state and releases it only around
    engine waits (`_ungated`, inside the phase helpers `take` calls), so a
    backlog of owed audits and repair rounds — pure engine-bound work — stops
    serializing a wide drain behind one call at a time. `take` returns "ok"
    (answered), "unanswered", "skip" (nothing consumed), or `AUDIT_THROTTLED`.
    """
    unanswered = 0
    if settings.l2_concurrency <= 1:
        for item in queued:
            if summary.spend_usd > max_usd:
                break
            status = take(item, None)
            if status == AUDIT_THROTTLED:
                summary.throttled = True
                return AUDIT_THROTTLED
            if status == "unanswered":
                unanswered += 1
                if unanswered >= BREAKER_LIMIT:
                    summary.aborted = breaker_abort
                    return breaker_abort
                continue
            if status == "ok":
                unanswered = 0
        return None

    gate = threading.RLock()
    stop = threading.Event()
    items = iter(queued)
    # one mutable cell per shared fact, mutated only under the gate — the same
    # shape as the drain's breaker_box
    state: dict[str, Any] = {"unanswered": 0, "abort": None}

    def _worker() -> None:
        while not stop.is_set():
            with gate:
                if summary.spend_usd > max_usd or stop.is_set():
                    return
                item = next(items, None)
            if item is None:
                return
            status = take(item, gate)
            with gate:
                if status == AUDIT_THROTTLED:
                    summary.throttled = True
                    state["abort"] = AUDIT_THROTTLED
                    stop.set()
                    return
                if status == "unanswered":
                    state["unanswered"] += 1
                    if state["unanswered"] >= BREAKER_LIMIT:
                        summary.aborted = breaker_abort
                        state["abort"] = breaker_abort
                        stop.set()
                        return
                elif status == "ok":
                    state["unanswered"] = 0

    from concurrent.futures import ThreadPoolExecutor

    workers = min(settings.l2_concurrency, max(len(queued), 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f in [pool.submit(_worker) for _ in range(workers)]:
            f.result()  # a worker's bug must surface, not vanish
    abort = state["abort"]
    return abort if isinstance(abort, str) else None


def _reaudit_pass(
    settings: Settings,
    session: _Session,
    journal: _Journal,
    engine: Engine,
    summary: ExtractSummary,
    now: Callable[[], datetime],
    active: Bundle,
    only_doc: str | None,
    max_docs: int,
    max_usd: float,
) -> str | None:
    """The ONE re-audit an unfinished audit gets (spec §5, validator/18).

    Two audits can be unfinished, and this pass takes either: the candidate's
    own, when a pass of its budget is still free, and a repair round's
    subsequent audit, when the call that should have judged the repaired
    candidate never reached the auditor (`_repair_audit_owed`). Both are a phase
    the archive shows as owed; both settle through the same single fold.

    A settled document is never re-queued for extraction — its row satisfies the
    tuple — so a failed quality phase would otherwise be permanent, and every
    record it touched permanently ineligible. This pass is the next run coming
    back for it: no extraction, no new sample, one audit call on the candidate
    already on record, then the same single settlement every other path takes.
    Exactly one, and never in the loop that produced the error: `_next_audit_pass`
    hands out the second pass and refuses a third, and the failure that costs a
    document its audit is a provider having a bad minute, which a retry in the
    same breath would only pay for twice.

    It runs BEFORE the extraction drain because it is the cheapest eligibility
    this run can buy: one call finishes a document the corpus has already paid
    three extraction calls for. It shares the run's money cap and takes the
    document cap as its own bound, so a backlog of errors cannot crowd out the
    queue indefinitely — and on a v1 tuple it does nothing at all.

    The queue is what keeps it honest about WHICH documents (see
    `_reaudit_queue`: a pass still owed, and a published state a re-audit can
    only make eligible, never promote). This loop is what keeps it honest about
    the provider. It is the drain's own throttle-and-breaker discipline, for
    the same reason the drain has it: a document here has exactly one pass
    left, so a rate-limit storm that ran the window would spend the last retry
    of every document in it — permanently, the archive being write-once — and
    report a successful run. The first 429 ends the run instead, and
    `BREAKER_LIMIT` consecutive unanswered audits end it too. The abort reason
    is returned; nothing was spent to learn it.
    """
    if active.audit_version is None:
        return None  # no audit phase, nothing that could have errored
    store = journal.store
    audit_version = active.audit_version
    queued = session.do(
        lambda c: _reaudit_queue(
            c, prompt_version=active.prompt_version, schema_version=active.schema_version,
            validator_version=active.validator_version, audit_version=audit_version,
            model_regex=globs_to_regex(settings.l2_models), only_doc=only_doc, limit=max_docs,
        )
    )
    session.do(lambda c: c.commit())  # [A1]: the probes below are archive traffic

    def _take(item: tuple[str, str], gate: threading.RLock | None) -> str | None:
        dh, candidate = item
        # `d=dh` binds the loop variable at definition (ruff B023); the
        # annotations are what keep `session.do`'s type inference working

        def _markdown(c: Conn, d: str = dh) -> str | None:
            return extraction.markdown_for(c, d, NORMALIZER_VERSION)

        def _settle(c: Conn, d: str = dh) -> DerivedState:
            return settle(c, store, d, settings.l2_models, iso(now()), bundle=active)

        # The base candidate's own pass first, then a repair round's subsequent
        # audit. Under one audit version the two are never both owed — a round
        # exists only where the base's verdict is already complete — but a
        # version bump owes both, and the base's is what the round's verdict is
        # judged against (`_repair_accepted`), so it must not be taken second.
        scheduled = _next_audit_pass(store, candidate, audit_version)
        owed_repair = scheduled is None and _repair_audit_owed(store, candidate, audit_version)
        if scheduled is None and not owed_repair:
            # the row asks for work the archive says is done: a flag that
            # outlived the budget, a settlement that never landed, or a version
            # this tuple has already audited under a fold that did not record
            # it. Re-fold it so it stops claiming a pass it cannot take, and do
            # not count it — the archive is what says so, and this is the only
            # place that can put the row back in agreement with it.
            session.do(_settle)
            session.do(lambda c: c.commit())
            return "skip"
        markdown = session.do(_markdown)
        if markdown is None:
            return "skip"  # the normalizer moved; the new text will be drained
        session.do(lambda c: c.commit())
        if owed_repair:
            # the round's second half, finished by the next pass. No repair
            # trigger can come of it: the candidate has taken its one round
            # (`_unspent`), and what is left is the verdict that says whether
            # the repaired candidate settles in the base's place.
            phase = AuditPhase(unreachable=_audit_repaired(
                settings, session, journal, engine, dh, markdown, summary, now,
                active, gate, candidate,
            ))
        else:
            phase = _audit_candidate(settings, session, journal, engine, dh, markdown,
                                     summary, now, active, gate)
        if phase.unreachable == AUDIT_THROTTLED:
            return AUDIT_THROTTLED  # nothing archived: every queued pass stands
        if phase.unreachable is not None:
            return "unanswered"  # this document's pass is intact for the next run
        stopped: str | None = None
        if phase.trigger is not None:
            # The verdict this trigger reads was reached in THIS pass and has
            # never been published: the row on record says `validated` with an
            # unfinished audit, and the repair round runs before the settlement
            # that will publish the finished one. It cannot promote anything
            # either — `_reaudit_queue` only ever offers `validated` rows, so
            # the most a repaired candidate moves here is ELIGIBILITY, which is
            # the permanence this whole pass exists to remove.
            summary.repair_triggered += 1
            stopped = _repair_candidate(settings, session, journal, engine, dh, markdown,
                                        summary, now, bundle=active, gate=gate,
                                        trigger=phase.trigger)
        session.do(_settle)
        session.do(lambda c: c.commit())
        summary.reaudited += 1
        if stopped == AUDIT_THROTTLED:
            return AUDIT_THROTTLED  # the fresh verdict is settled; the run stops
        return "ok"

    def _gated_take(item: tuple[str, str], gate: threading.RLock | None) -> str | None:
        if gate is None:
            return _take(item, None)
        with gate:  # held for all state; the phase helpers release around engine waits
            return _take(item, gate)

    return _drive_pass(settings, summary, queued, _gated_take, max_usd, "audit_unreachable")


def _repair_queue(
    conn: Conn,
    *,
    prompt_version: str,
    schema_version: str,
    validator_version: str,
    model_regex: str,
    only_doc: str | None,
    limit: int,
) -> list[tuple[str, str]]:
    """Settled documents a repair round would return to the aggregates.

    The derived row's own `flags` is the index — every fold writes the repair
    its verdict asks for — so the campaign that comes back for the backlog costs
    one query instead of an archive probe per settled row. A row folded before
    the flag existed asks for nothing, which keeps a silent backfill out of the
    drain; re-folding the corpus is what fills this queue.

    The queue exists because the drain never revisits a settled document: its
    row satisfies the tuple, so a candidate whose findings keep it out of the
    aggregates stays out for the life of the tuple unless something comes back
    for it. That is the whole permanence this pass removes.

    `eligibility` on a `validated` row is the only work it will take, and that
    is the publication boundary (spec §6). Such a repair can only move
    ELIGIBILITY: the lifecycle is already what a clean audit leaves, so no
    automated phase promotes anything. A PARKED record is the case the rule
    forbids — its needs-review state is published, a person may be triaging it,
    and an automated round that cleared the disagreement would promote a
    terminal state behind them. Those take their round in the pre-settlement
    window instead: the drain's on the way in, the replay's when a re-settle
    folds the corpus under a new policy, or the fresh cohort a human `retry`
    opens.
    """
    rows = conn.execute(
        """
        SELECT document_hash, chosen_attempt FROM extractions
        WHERE prompt_version = %(pv)s AND schema_version = %(sv)s
          AND validator_version = %(vv)s AND model ~ %(model_regex)s
          AND chosen_attempt IS NOT NULL AND status = 'validated'
          AND flags->>'repair' = %(reason)s
          AND (%(only)s::text IS NULL OR document_hash = %(only)s)
        ORDER BY updated_at, document_hash
        LIMIT %(limit)s
        """,
        {
            "pv": prompt_version, "sv": schema_version, "vv": validator_version,
            "model_regex": model_regex, "reason": REPAIR_ELIGIBILITY,
            "only": only_doc, "limit": limit,
        },
    ).fetchall()
    return [(r["document_hash"], r["chosen_attempt"]) for r in rows]


def _repair_pass(
    settings: Settings,
    session: _Session,
    journal: _Journal,
    engine: Engine,
    summary: ExtractSummary,
    now: Callable[[], datetime],
    active: Bundle,
    only_doc: str | None,
    max_docs: int,
    max_usd: float,
) -> str | None:
    """The repair campaign: the settled backlog, one round per candidate.

    The drain repairs the document in front of it; this is the same phase,
    driven off the derived rows of documents nothing is going to drain again
    (`_repair_queue`). It runs after the re-audit pass and before the drain,
    for the reason that pass runs first: a document the corpus has already paid
    three extraction calls for is the cheapest eligibility a run can buy, and
    one repair call is what stands between it and the aggregates.

    Its bounds are the drain's, and each one is the same rule. The money cap is
    shared, the document cap is its own, and the provider's own signals end the
    run — a 429 stops it with every remaining round unspent (a rate-limit storm
    must not spend a corpus's rounds on nothing, the archive being write-once),
    and `BREAKER_LIMIT` unanswered calls in a row say the provider is not
    answering rather than that the queue is long. On a tuple with no repair
    contract it does nothing at all.
    """
    if _repair_contract(active) is None:
        return None
    store = journal.store
    queued = session.do(
        lambda c: _repair_queue(
            c, prompt_version=active.prompt_version, schema_version=active.schema_version,
            validator_version=active.validator_version,
            model_regex=globs_to_regex(settings.l2_models), only_doc=only_doc, limit=max_docs,
        )
    )
    session.do(lambda c: c.commit())  # [A1]: the probes below are archive traffic

    def _take(item: tuple[str, str], gate: threading.RLock | None) -> str | None:
        dh, candidate = item

        def _markdown(c: Conn, d: str = dh) -> str | None:
            return extraction.markdown_for(c, d, NORMALIZER_VERSION)

        def _settle(c: Conn, d: str = dh) -> DerivedState:
            return settle(c, store, d, settings.l2_models, iso(now()), bundle=active)

        if store.exists(keys.x_repair_key(candidate)):
            # the flag outlived the round — a run that died between the artifact
            # and its fold. Re-fold it so the row stops claiming work nothing can
            # do, and do not count it: the archive is what says the round is gone.
            session.do(_settle)
            session.do(lambda c: c.commit())
            return "skip"
        markdown = session.do(_markdown)
        if markdown is None:
            return "skip"  # the normalizer moved; the new text will be drained
        session.do(lambda c: c.commit())
        summary.repair_triggered += 1
        stopped = _repair_candidate(settings, session, journal, engine, dh, markdown,
                                    summary, now, active, gate,
                                    RepairTrigger(dh, candidate, REPAIR_ELIGIBILITY))
        session.do(_settle)
        session.do(lambda c: c.commit())
        if stopped == AUDIT_THROTTLED:
            return AUDIT_THROTTLED
        if stopped is not None:
            return "unanswered"  # this candidate's round is intact for the next run
        return "ok"

    def _gated_take(item: tuple[str, str], gate: threading.RLock | None) -> str | None:
        if gate is None:
            return _take(item, None)
        with gate:  # held for all state; the phase helpers release around engine waits
            return _take(item, gate)

    return _drive_pass(settings, summary, queued, _gated_take, max_usd, "repair_unreachable")


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
        # after the replay, so a document whose audit artifact never reached a
        # derived row is folded before it is judged to need another audit
        if _reaudit_pass(settings, session, journal, engine, summary, now, active,
                         only_doc, max_docs, max_usd) is not None:
            # the provider is throttling or unreachable. The drain's first act
            # is another call to it, so there is nothing to do but stop; the
            # counters and `throttled`/`aborted` say what happened.
            return summary
        # then the repair backlog: documents the drain will never see again,
        # one call each away from the aggregates (`_repair_queue`)
        if _repair_pass(settings, session, journal, engine, summary, now, active,
                        only_doc, max_docs, max_usd) is not None:
            return summary
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
                disposition, breaker = result  # already committed by _extract_doc
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
                        # the document's own commit already happened under the
                        # gate _extract_doc held; this block only counts it
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
    """One document, start to committed.

    With a gate (parallel drain): the gate is held for ALL state — DB, journal,
    summary, breaker — and released strictly around engine calls, so the
    semantics stay those of the serial drain while the waiting overlaps.

    The per-document commit belongs HERE, not to the caller ([A1]). The gate is
    handed to another worker the instant this returns, and that worker's first
    act on the SAME connection is archive I/O — its attempt PUT, or `settle`'s
    GETs. A commit the caller makes after re-acquiring the gate is too late: the
    transaction `upsert_state` opened would be live across another worker's
    round trip, which is the idle-in-transaction state SQLSTATE 25P03 kills. The
    early returns are covered too — a document that vanished leaves the read
    transaction its markdown lookup opened, and a document that raises leaves
    its `engine_fatal` row.
    """
    if gate is None:
        result = _extract_doc_inner(settings, session, journal, engine, dh, summary,
                                    breaker, now, bundle, None)
        session.do(lambda c: c.commit())
        return result
    with gate:
        try:
            result = _extract_doc_inner(settings, session, journal, engine, dh, summary,
                                        breaker, now, bundle, gate)
        except BaseException:
            # Landing this document's rows is what `settle`'s own boundary does
            # with them: insert-only, already archived, idempotent on their key.
            # A commit that cannot happen changes nothing here — the connection
            # is gone, so no other worker will reach the archive on it either —
            # and must never replace the failure that is ending the run.
            with contextlib.suppress(psycopg.Error):
                session.conn.commit()
            raise
        session.do(lambda c: c.commit())
        return result


def _audit_candidate(
    settings: Settings,
    session: _Session,
    journal: _Journal,
    engine: Engine,
    dh: str,
    markdown: str,
    summary: ExtractSummary,
    now: Callable[[], datetime],
    bundle: Bundle,
    gate: threading.RLock | None,
) -> AuditPhase:
    """The `semantic-audit/v2` phase: one full-source audit of the candidate
    settlement is about to publish (spec §4, §5.5), and the repair the verdict
    it reaches asks for.

    It runs after the samples and BEFORE `settle`, because spec §6 forbids an
    automated phase promoting a state that has already been published — the
    lifecycle-boundary defect class the 2026-09-11 analysis named. So the
    document is settled once, already audited, and nothing intermediate is
    published on the way. The returned trigger is read in that same window: the
    repair round is a phase before settlement, never a correction after it.

    The candidate is chosen by the same fold settlement will run, so the audited
    candidate and the published one cannot diverge. The artifact is archived
    before any derived row exists ([A1], archive-as-truth): a crash after the
    PUT replays the audit for free, a crash before it re-audits, and neither
    leaves a verdict nothing backs.

    Nothing here can block settlement or fabricate a pass. An auditor that
    answered with unparseable JSON, from a model outside the globs, or with an
    audit the judge rejected archives an `audit_error` artifact, which the
    settlement policy reads as `error` on both dimensions: never eligible,
    never a demotion of an otherwise valid record. An error is not final
    either: `_next_audit_pass` hands out the one re-audit spec §5 allows, and
    `_reaudit_pass` is what brings an already-settled document back here to
    take it.

    An auditor that never answered at all is a different thing and is reported,
    not archived (`AuditPhase.unreachable`): a throttle, a dead socket, a
    refused request or a missing model produced no verdict, so writing one
    under this candidate's write-once pass key would spend a retry on a
    provider's bad minute and leave nothing able to replace it.
    """
    if (
        bundle.audit_version is None
        or bundle.audit_render is None
        or bundle.audit_emit_schema is None
        or bundle.audit_judge is None
    ):
        return AuditPhase()  # no audit phase (v1): nothing to run, nothing to read
    store = journal.store

    def fold() -> tuple[list[Attempt], list[Review], DerivedState, _Phases]:
        return session.do(
            lambda c: _fold(
                c, store, dh, settings.l2_models, bundle, bundle.prompt_version,
                bundle.schema_version, bundle.validator_version,
            )
        )

    _, reviews, state, _ = fold()
    # Only a settled candidate is auditable: a quarantined or pending document
    # has none, and a human-rejected one is terminal (spec §6) — auditing it
    # could not change anything and would only spend the operator's budget.
    if state.chosen_attempt is None or state.status not in ("validated", "needs_review"):
        return AuditPhase()
    scheduled = _next_audit_pass(store, state.chosen_attempt, bundle.audit_version)
    if scheduled is None:
        # audited already (write-once), or two errors deep: the verdict on
        # record is the verdict, and it is what the trigger reads.
        return AuditPhase(trigger=_unspent(store, _repair_trigger(dh, state, reviews)))
    audit_pass, key = scheduled
    candidate = from_bytes(store.get(state.chosen_attempt))
    record = candidate.record
    if record is None:
        return AuditPhase()  # an attempt with no record cannot be the medoid; belt and braces
    outcome, unreachable = _audit_once(
        settings, engine, dh, markdown, record, state.chosen_attempt,
        candidate.requested_model,  # the rung that actually answered
        key, audit_pass, store, summary, now, bundle, gate,
    )
    if unreachable is not None:
        # Nothing reached the auditor, so there is no verdict to archive. The
        # document settles with its audit `not_checked` — honestly unaudited,
        # never eligible — and `_reaudit_pass` comes back for the pass that was
        # not spent. Writing an `audit_error` here instead would burn it on a
        # provider's bad minute, under a write-once key nothing could rewrite.
        return AuditPhase(unreachable=unreachable)
    if outcome is None:
        # an `audit_error` asks for the re-audit above, never for a repair:
        # there are no validated findings to repair against, and the fold's
        # verdict cannot have moved (`error` is not a completed dimension)
        return AuditPhase()
    # The fold above predates the artifact, so the trigger is read off a FRESH
    # one: the audit is exactly what decides whether this cohort adjudicates or
    # parks, and the repair phase must be asked for on the verdict settlement is
    # about to publish, not the one that stood before the audit ran.
    _, reviews, state, _ = fold()
    return AuditPhase(trigger=_unspent(store, _repair_trigger(dh, state, reviews)))


def _audit_once(
    settings: Settings,
    engine: Engine,
    dh: str,
    markdown: str,
    record: dict[str, Any],
    attempt_key: str,
    model: str,
    key: str,
    audit_pass: int,
    store: ArchiveStore,
    summary: ExtractSummary,
    now: Callable[[], datetime],
    bundle: Bundle,
    gate: threading.RLock | None,
) -> tuple[Any, str | None]:
    """One full-source audit of one candidate, archived where the caller says.

    Returns `(outcome, unreachable)`, and the three answers it can give are the
    three things that can happen to a quality phase: a completed audit (an
    outcome, artifact `ok`), a verdict the judge refused (`None` with no
    `unreachable`, archived as `audit_error` — never a pass, never a demotion of
    the record itself), and a call that never reached an auditor (`unreachable`,
    nothing archived and no pass spent, because a verdict written for a dead
    socket is one no retry could ever replace).

    Both the base candidate and a repaired one come through here, so what an
    audit costs, retries and writes cannot drift between them; only the key and
    the record differ.
    """
    assert bundle.audit_render is not None and bundle.audit_emit_schema is not None
    assert bundle.audit_judge is not None
    candidate_hash = _hash_of(record)
    schema = bundle.audit_emit_schema()
    prompt = bundle.audit_render(markdown, candidate_hash, record)

    t0 = now()
    errors: list[str] = []
    result: EngineResult | None = None
    unreachable: str | None = None
    # one transport retry, like a sample slot: a codex flake would otherwise
    # leave a clean candidate looking unaudited for the life of the tuple
    for attempt_i in range(2):
        try:

            def _call(p: str = prompt, m: str = model) -> EngineResult:
                return engine.complete(p, schema, m)

            result = _ungated(gate, _call)
            break
        except EngineThrottled as exc:
            # the provider asking for silence. Not retried in the same breath
            # (that is what it asked us not to do) and not archived: the run
            # stops with this candidate's pass still unspent.
            errors.append(str(exc))
            unreachable = AUDIT_THROTTLED
            break
        except EngineTransportError as exc:
            errors.append(str(exc))
            unreachable = AUDIT_UNREACHABLE
            if attempt_i == 1:
                break
        except (EngineModelNotFound, EngineFatalError) as exc:
            # no rung ladder here: the audit is a quality gate, not a candidate,
            # and a document whose audit could not run settles ineligible rather
            # than not at all. One refused audit must not throw away a paid-for
            # extraction: in the drain the extraction path raises on the next
            # document, and in the re-audit pass — which has no extraction path
            # to raise — `BREAKER_LIMIT` of these in a row ends the run.
            errors.append(f"{type(exc).__name__}: {exc}")
            unreachable = AUDIT_UNREACHABLE
            break

    if result is None:
        return None, unreachable or AUDIT_UNREACHABLE

    outcome: Any = None
    summary.spend_usd += result.cost_usd or 0.0
    if not model_matches(result.observed_model, settings.l2_models):
        # `observed_model` gates everything (spec §4.1): a substituted model
        # clearing records is exactly what the glob exists to prevent, and
        # an unknown auditor is an error, never a pass.
        errors.append(f"observed model {result.observed_model!r} outside globs")
    else:
        try:
            emit = json.loads(result.raw_text)
            if not isinstance(emit, dict):
                raise ValueError("top level is not an object")
        except ValueError as exc:
            errors.append(f"response is not valid JSON: {exc}")
        else:
            try:
                outcome = bundle.audit_judge(emit, record, markdown, candidate_hash)
            except Exception as exc:
                # Every validity defect the judge found, verbatim: the class
                # name travels too, so a bug in this path is legible in the
                # artifact instead of hiding inside an `audit_error`.
                errors.append(f"{type(exc).__name__}: {exc}")

    artifact: dict[str, Any] = {
        "audit_version": bundle.audit_version,
        "outcome": "ok" if outcome is not None else "audit_error",
        # which of the candidate's passes this is; the key says so too, and an
        # artifact that has to be read on its own should not need the key
        "audit_pass": audit_pass,
        "attempt_key": attempt_key,
        "document_hash": dh,
        "candidate_hash": candidate_hash,
        "prompt_version": bundle.prompt_version,
        "schema_version": bundle.schema_version,
        "validator_version": bundle.validator_version,
        "run_id": summary.run_id,
        "cli_version": __version__,
        "requested_engine": engine.name,
        "requested_model": model,
        "observed_model": result.observed_model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost_usd": result.cost_usd,
        "started_at": iso(t0),
        "finished_at": iso(now()),
        "raw_response": result.raw_text,
        "errors": errors,
        # the three settlement reads, plus everything a human or a repair round
        # needs to act on the verdict
        "semantics": outcome.semantics if outcome is not None else "error",
        "completeness": outcome.completeness if outcome is not None else "error",
        "blocking": outcome.blocking if outcome is not None else 0,
        "warnings": outcome.warnings if outcome is not None else 0,
        "findings": outcome.findings if outcome is not None else [],
        "unresolved": outcome.unresolved if outcome is not None else [],
    }
    store.put(key, _audit_bytes(artifact))
    return outcome, None


def _repair_candidate(
    settings: Settings,
    session: _Session,
    journal: _Journal,
    engine: Engine,
    dh: str,
    markdown: str,
    summary: ExtractSummary,
    now: Callable[[], datetime],
    bundle: Bundle,
    gate: threading.RLock | None,
    trigger: RepairTrigger,
) -> str | None:
    """The ONE `semantic-repair/v1` round a candidate gets (spec §4, §5).

    It runs in the window the audit opened and settlement closes: the verdict
    the document is about to be published with says the candidate is defective
    where it matters, so the defect is fixed BEFORE anything is published rather
    than corrected afterwards — the auto-promotion hole spec §6 forbids. Source,
    candidate and the audit's own validated findings go to the repairer; typed
    operations come back; `apply` either returns a whole new assembled record or
    refuses the patch entirely.

    Five things bound it, and each is a rule from the spec rather than a
    preference. The round is archived under a write-once key, so a candidate is
    offered exactly one (§5's "one semantic repair round"). The repaired
    candidate is re-verified by the bundle's own verifier and then audited once
    (§5's "one subsequent audit"), and it settles only if that audit completed
    and is no worse than the base's (`_repair_accepted`). A judge error, a
    failed verify, an unparseable answer or a model outside the globs archives
    the defect list and no record, which leaves the base candidate to settle
    exactly as it would have (§4: "A failed repair does not erase the base
    candidate"). Nothing here is an extraction sample: the repaired candidate
    lives in its own namespace and the agreement cohort never sees it. And a
    call that never reached the repairer archives nothing at all, so the round
    is still there to take — the audit phase's discipline, for the same reason.

    Returns `AUDIT_THROTTLED` when the provider asked for silence (the caller
    stops the run, with nothing spent), else `None`.
    """
    contract = _repair_contract(bundle)
    if contract is None:
        return None  # this tuple has no repair contract; findings just gate
    store = journal.store
    key = keys.x_repair_key(trigger.attempt_key)
    if store.exists(key):
        return None  # the round is taken; write-once is the cap
    audit = _latest_audit(store, trigger.attempt_key, bundle.audit_version)
    if audit is None or _errored(audit):
        return None  # no completed audit: nothing validated to repair against
    findings = [f for f in audit.findings if f.get("severity") != "warning"]
    if not findings:
        # blocking unresolved questions only. They gate eligibility (spec §4),
        # but no typed operation addresses a question, and a repair prompt with
        # an empty finding list is an invitation to rewrite the record freely.
        return None
    candidate = from_bytes(store.get(trigger.attempt_key))
    record = candidate.record
    if record is None:
        return None
    candidate_hash = _hash_of(record)
    model = candidate.requested_model
    prompt = contract.render(markdown, candidate_hash, record, list(findings))
    schema = contract.emit_schema()
    # [A1]: a repair call is a minute of model time, and a managed Postgres
    # kills a session that holds a transaction across one (SQLSTATE 25P03).
    session.do(lambda c: c.commit())

    t0 = now()
    errors: list[str] = []
    result: EngineResult | None = None
    unreachable: str | None = None
    for attempt_i in range(2):  # one transport retry, like the audit phase
        try:

            def _call(p: str = prompt, m: str = model) -> EngineResult:
                return engine.complete(p, schema, m)

            result = _ungated(gate, _call)
            break
        except EngineThrottled as exc:
            errors.append(str(exc))
            unreachable = AUDIT_THROTTLED
            break
        except EngineTransportError as exc:
            errors.append(str(exc))
            unreachable = AUDIT_UNREACHABLE
            if attempt_i == 1:
                break
        except (EngineModelNotFound, EngineFatalError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            unreachable = AUDIT_UNREACHABLE
            break
    if result is None:
        return unreachable or AUDIT_UNREACHABLE

    summary.spend_usd += result.cost_usd or 0.0
    repaired: dict[str, Any] | None = None
    if not model_matches(result.observed_model, settings.l2_models):
        # `observed_model` gates everything (spec §4.1), and a record written by
        # a model the operator never accepted is exactly what it gates.
        errors.append(f"observed model {result.observed_model!r} outside globs")
    else:
        try:
            emit = json.loads(result.raw_text)
            if not isinstance(emit, dict):
                raise ValueError("top level is not an object")
        except ValueError as exc:
            errors.append(f"response is not valid JSON: {exc}")
        else:
            try:
                repaired = contract.apply(emit, record, markdown, candidate_hash)
            except Exception as exc:
                errors.extend(_judge_errors(exc))
    if repaired is not None:
        # spec §4: "Re-run all deterministic checks". A repaired candidate is a
        # whole assembled record and is held to exactly what a fresh extraction
        # is — the repair module deliberately owns no record-level grammar.
        report = bundle.verify(repaired, markdown)
        if report.status == "fail":
            render = bundle.render_finding
            errors += [
                render(f) if render is not None else f"{f.check}:{f.code} at {f.path}"
                for f in report.findings if f.severity == "error"
            ]
            repaired = None

    artifact: dict[str, Any] = {
        "repair_version": contract.version,
        "outcome": "repaired" if repaired is not None else "repair_error",
        "reason": trigger.reason,
        "attempt_key": trigger.attempt_key,
        "document_hash": dh,
        "candidate_hash": candidate_hash,
        "repaired_candidate_hash": None if repaired is None else _hash_of(repaired),
        "prompt_version": bundle.prompt_version,
        "schema_version": bundle.schema_version,
        "validator_version": bundle.validator_version,
        "run_id": summary.run_id,
        "cli_version": __version__,
        "requested_engine": engine.name,
        "requested_model": model,
        "observed_model": result.observed_model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost_usd": result.cost_usd,
        "started_at": iso(t0),
        "finished_at": iso(now()),
        # what the round was asked to fix, what it asked for, and what came of
        # it: the artifact has to stand on its own for a human reading it later
        "findings": list(findings),
        "raw_response": result.raw_text,
        "errors": errors,
        "record": repaired,
    }
    store.put(key, _audit_bytes(artifact))
    if repaired is None:
        return None  # the base candidate settles exactly as it would have
    unreachable = _audit_repaired(settings, session, journal, engine, dh, markdown,
                                  summary, now, bundle, gate, trigger.attempt_key)
    return AUDIT_THROTTLED if unreachable == AUDIT_THROTTLED else None


def _audit_repaired(
    settings: Settings,
    session: _Session,
    journal: _Journal,
    engine: Engine,
    dh: str,
    markdown: str,
    summary: ExtractSummary,
    now: Callable[[], datetime],
    bundle: Bundle,
    gate: threading.RLock | None,
    attempt_key: str,
) -> str | None:
    """The ONE subsequent audit a repair round's candidate gets (spec §5).

    It is the round's second write, and it is separately reachable because it
    is separately owed: the repaired record is archived BEFORE the call ([A1]),
    and a call that never reaches the auditor archives nothing, so the round is
    left unfinished rather than spent on a provider's bad minute. The round that
    produced the candidate takes it first; when that call did not reach anyone,
    `_reaudit_pass` comes back here with the same key, the same record and the
    same accounting, so where the audit was taken cannot change what it means.

    Archived beside the base candidate's passes, never over them: this is an
    audit of a DIFFERENT candidate, and a verdict that overwrote another would
    be a verdict nothing can account for afterwards.

    Returns what the audit call reported when it reached nobody (the caller
    stops the run on a throttle), or `None` once a verdict — a pass or an
    `audit_error` — is on record.
    """
    store = journal.store
    key = keys.x_repair_key(attempt_key)
    repaired = _repaired_record(store, key) if store.exists(key) else None
    if repaired is None:
        return None  # no round, or one that produced no candidate to audit
    audit_key = _repair_audit_key(attempt_key, bundle.audit_version)
    if store.exists(audit_key):
        return None  # write-once: the one subsequent audit is the one it took
    model = from_bytes(store.get(attempt_key)).requested_model
    # [A1]: an audit call is a minute of model time, and a managed Postgres
    # kills a session that holds a transaction across one (SQLSTATE 25P03)
    session.do(lambda c: c.commit())
    _, unreachable = _audit_once(
        settings, engine, dh, markdown, repaired, attempt_key, model,
        audit_key, 1, store, summary, now, bundle, gate,
    )
    # counted on the policy's own answer, not on the phase's hopes: this is the
    # same read settlement is about to make
    if _published(store, attempt_key, bundle.audit_version).record is not None:
        summary.repaired += 1
    return unreachable


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
    schema = (bundle.engine_emit_schema() if bundle.engine_emit_schema is not None
              else emit_schema(bundle.schema_version))
    # [A1] end the transaction those two lookups opened. Everything this
    # function does next is archive traffic — the over-budget branch PUTs its
    # attempt object without ever reaching the pre-call commit below — and a PUT
    # is the same round trip a GET is, so it may no more hold a transaction open
    # (SQLSTATE 25P03, canary run 33666006472).
    session.do(lambda c: c.commit())

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
        # The audit phase belongs HERE, between the last extraction attempt and
        # the single settlement this document gets (spec §5.6: archive every
        # phase artifact, then "settle once ... Do not publish an intermediate
        # candidate during audit"). Every path that settles goes through this
        # function, so every path that publishes a candidate audits it first —
        # including the ones that never sampled. A document with no candidate
        # (over budget, quarantined) costs nothing: the phase folds, sees no
        # chosen attempt and returns before any engine call.
        phase = _audit_candidate(settings, session, journal, engine, dh, markdown,
                                 summary, now, bundle, gate)
        throttled = phase.unreachable == AUDIT_THROTTLED
        if phase.trigger is not None:
            # The repair round belongs in THIS window — after the audit, before
            # the one settlement (spec §5.5/§6): the document has not been
            # published yet, so fixing the candidate here is a repair and doing
            # it afterwards would be a promotion.
            summary.repair_triggered += 1
            stopped = _repair_candidate(settings, session, journal, engine, dh, markdown,
                                        summary, now, bundle, gate, phase.trigger)
            throttled = throttled or stopped == AUDIT_THROTTLED
        state = session.do(
            lambda c: settle(c, store, dh, settings.l2_models, iso(now()), bundle=bundle)
        )
        if throttled:
            # The extraction this document already paid for is settled — an
            # unaudited record is ineligible, not lost, and `_reaudit_pass`
            # comes back for the pass the throttle did not spend. The run stops
            # here all the same: the next document's first act is another call
            # to the provider that just said no.
            return "throttled", breaker
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
        # The last candidate this rung produced: the response verbatim (fed
        # back so the retry edits it instead of regenerating) and its parsed
        # form (the delta check's baseline). The two move together and only on
        # an attempt that parsed. A rung change resets them with `prior_errors`
        # — a fresh model is a fresh generation, not an edit of another model's
        # answer.
        prior_raw: str | None = None
        prior_emit: dict[str, Any] | None = None
        transports = 0
        content_no = 0
        while content_no < CONTENT_ATTEMPTS:
            t0 = now()
            prompt = bundle.render(markdown, prior_errors, prior_raw)
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
                # An unparseable answer replaces neither half: there is nothing
                # to show a retry and nothing it could have deleted. The next
                # attempt is shown — and judged against — the last candidate
                # that actually existed, so what the prompt carries and what the
                # delta check compares can never be two different emits. The
                # errors it is asked to fix are BOTH halves: the parse failure
                # and whatever the shown candidate actually failed on — feeding
                # the parse error alone would pair a valid-looking candidate
                # with "fix ONLY these issues" and invite returning it verbatim
                # (2026-09-16 review).
                prior_errors = errors + [
                    e for e in prior_errors
                    if not e.startswith("response is not valid JSON")
                ]
                continue
            # this attempt parsed, so it is answerable for what the last one
            # held: anything valid and unnamed that is missing is a content
            # error, carried alongside whatever else this attempt got wrong.
            # A deletion error in the fed list is an order to RESTORE, never
            # permission to drop again — it does not "name" the object into
            # the allowed-change set.
            asked = [e for e in prior_errors
                     if not e.startswith("retry:unexplained_deletion")]
            dropped = (
                unexplained_deletions(prior_emit, emit, asked)
                if prior_emit is not None else []
            )
            if not dropped:
                # a flagged emit never becomes the baseline: advancing to it
                # would judge the next attempt against the collapsed candidate,
                # find nothing missing, and publish the loss one attempt later
                # (2026-09-16 review probe) — the check must survive a repeat.
                prior_raw, prior_emit = result.raw_text, emit
            if schema_errors := validate_emit(emit, bundle.schema_version):
                errors = schema_errors + dropped
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="schema_invalid", raw_response=result.raw_text,
                                fed=prior_errors, produced=errors,
                                ladder_exhausted=exhausted, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                prior_errors = errors
                continue
            try:
                record = bundle.assemble(emit, markdown, document_hash=dh,
                                         normalizer_version=NORMALIZER_VERSION,
                                         observed_model=observed, at=iso(t0))
            except AssembleError as exc:
                errors = exc.errors + dropped
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="attribution_failed", raw_response=result.raw_text,
                                fed=prior_errors, produced=errors,
                                ladder_exhausted=exhausted, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                prior_errors = errors
                continue
            report = bundle.verify(record, markdown)
            findings: list[dict[str, Any]] = [
                {"check": f.check, "path": f.path, "code": f.code,
                 "severity": f.severity, "detail": f.detail}
                for f in report.findings
            ]
            if report.status == "fail" or dropped:
                _rf = bundle.render_finding
                errors = [
                    _rf(f) if _rf is not None else f"{f.check}:{f.code} at {f.path}"
                    for f in report.findings if f.severity == "error"
                ] + dropped
                # A retry that deleted unnamed work is a content failure even
                # when the verifier passes it: the record it would publish is
                # the lossy one, and the ladder is where that gets another go.
                # `attribution_failed` is the vocabulary's content-failure
                # outcome (state.py settles it like the rest, quarantining an
                # exhausted ladder) — a new outcome would change settlement.
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
    # one transport retry per slot: a codex flake leaves the slot without a
    # record, the agreement hook reads that as sample_failed, and the whole
    # cohort demotes to needs_review — the largest human-queue class on the
    # 2026-09-11 quarantine set. A retried flake usually completes; a slot
    # that fails transport twice stays empty and demotes honestly.
    # bounded content repair for samples (2026-09-11 analysis): all 26 failed
    # extra samples were attribution failures, and slots 2/3 got a single
    # unrepaired shot while slot 1 had three fed-back attempts — hard documents
    # were compared against fresh outputs and demoted as "incomplete". Each
    # slot now gets SAMPLE_CONTENT_ATTEMPTS with the same error feedback.
    slots: list[tuple[int, bool, int, list[str]]] = [(2, False, 1, []), (3, False, 1, [])]
    while slots:
        slot, retried, content_no, prior_errors = slots.pop(0)
        t0 = now()
        # No prior candidate here, deliberately: the retry-carries-the-candidate
        # evidence (2026-09-14) is about the slot-1 ladder, and a sample slot's
        # value to the agreement gate is that it was generated independently.
        # Extending the edit contract to sample retries is a measured change,
        # not a free one.
        prompt = bundle.render(markdown, prior_errors, None)
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
            if not retried:
                slots.insert(0, (slot, True, content_no, prior_errors))
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
            "raw_response": result.raw_text, "fed": list(prior_errors),
            "ladder_exhausted": False,
            "started_at": t0, "sample_slot": slot,
            "tokens": (result.input_tokens, result.output_tokens),
            "cost": result.cost_usd,
        }

        def _content_retry(errors: list[str], *, _slot: int = slot,
                           _retried: bool = retried, _n: int = content_no) -> None:
            if _n < SAMPLE_CONTENT_ATTEMPTS:
                slots.insert(0, (_slot, _retried, _n + 1, errors))
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
            errors = [f"response is not valid JSON: {exc}"]
            archive_attempt(outcome="schema_invalid", produced=errors, **common)
            _content_retry(errors)
            continue
        if schema_errors := validate_emit(emit, bundle.schema_version):
            archive_attempt(outcome="schema_invalid", produced=schema_errors, **common)
            _content_retry(schema_errors)
            continue
        try:
            record = bundle.assemble(emit, markdown, document_hash=dh,
                                     normalizer_version=NORMALIZER_VERSION,
                                     observed_model=observed, at=iso(t0))
        except AssembleError as exc:
            archive_attempt(outcome="attribution_failed", produced=exc.errors, **common)
            _content_retry(exc.errors)
            continue
        report = bundle.verify(record, markdown)
        findings = [
            {"check": f.check, "path": f.path, "code": f.code,
             "severity": f.severity, "detail": f.detail}
            for f in report.findings
        ]
        if report.status == "fail":
            _rf = bundle.render_finding
            errors = [_rf(f) if _rf is not None else f"{f.check}:{f.code} at {f.path}"
                      for f in report.findings if f.severity == "error"]
            archive_attempt(outcome="attribution_failed", produced=errors,
                            findings=findings, **common)
            _content_retry(errors)
            continue
        archive_attempt(outcome="ok", produced=[], findings=findings,
                        record=record, **common)
    return None
