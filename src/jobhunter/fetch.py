"""One run: registry -> fetch every board -> archive manifest + blob -> ingest into the store."""

from __future__ import annotations

import contextlib
import gzip
import secrets
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
import psycopg

from jobhunter import __version__
from jobhunter.archive import ArchiveError, ArchiveStore, open_store
from jobhunter.archive.keys import attempt_key, blob_key, registry_key
from jobhunter.archive.manifests import iter_manifests, write_manifest
from jobhunter.config import Settings
from jobhunter.hashing import sha256_hex
from jobhunter.http import Fetcher
from jobhunter.ingest import replay_pending
from jobhunter.models import AttemptManifest, Board, DetailAttempt, FetchResult
from jobhunter.registry import load as load_registry
from jobhunter.sources import get_source, get_two_phase
from jobhunter.sources.base import EnvelopeError, ListRow, Source, TwoPhaseSource
from jobhunter.store import db as _db
from jobhunter.store.lifecycle import Ingestor, OutOfOrder
from jobhunter.timeutil import iso, utcnow

# Two-phase driver limits (spec 2026-09-04 §3.4; the page cap is §4.1's Workday
# value, the largest verified board needing 100 pages).
PAGE_CAP = 250
DETAIL_BUDGET = 40
REDETAIL_DAYS = 7


class UnknownBoardError(ValueError):
    """--board named a source:board that is not in the registry."""


def post_ping(url: str) -> None:
    """Dead-man's switch signal (docs/2026-08-25-durability-and-serving.md §3.1)."""
    httpx.post(url, timeout=10.0)


def gzip_bytes(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


def is_healthy(m: AttemptManifest) -> bool:
    """A board counts as healthy only if the transport succeeded AND the body parsed."""
    return m.transport == "ok" and m.error is None


@dataclass(frozen=True, slots=True)
class BoardOutcome:
    board: Board
    manifest: AttemptManifest
    blob_new: bool


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    started_at: datetime
    finished_at: datetime
    registry_revision: str
    outcomes: list[BoardOutcome]
    ingested: int = 0
    replayed: int = 0
    gaps: list[str] = field(default_factory=list)
    db_error: str | None = None
    lock_held: bool = False

    def counts(self) -> dict[str, int]:
        ok = sum(is_healthy(o.manifest) for o in self.outcomes)
        envelope_error = sum(
            o.manifest.transport == "ok" and o.manifest.error is not None for o in self.outcomes
        )
        http_error = sum(o.manifest.transport == "http_error" for o in self.outcomes)
        return {
            "boards": len(self.outcomes),
            "ok": ok,
            "envelope_error": envelope_error,
            "http_error": http_error,
            "transport_error": len(self.outcomes) - ok - envelope_error - http_error,
            "new_blobs": sum(o.blob_new for o in self.outcomes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "registry_revision": self.registry_revision,
            "counts": self.counts(),
            "ingested": self.ingested,
            "replayed": self.replayed,
            "gaps": self.gaps,
            "db_error": self.db_error,
            "lock_held": self.lock_held,
            "boards": [
                {
                    "board": o.board.key,
                    "transport": o.manifest.transport,
                    "http_status": o.manifest.http_status,
                    "record_count": o.manifest.record_count,
                    "payload_bytes": o.manifest.payload_bytes,
                    "blob_new": o.blob_new,
                    "attempt_id": o.manifest.attempt_id,
                    "error": o.manifest.error,
                }
                for o in self.outcomes
            ],
        }


def fetch_board(
    board: Board,
    source: Source,
    fetcher: Fetcher,
    store: ArchiveStore,
    *,
    run_id: str,
    registry_revision: str,
    now: Callable[[], datetime],
    dry_run: bool,
) -> BoardOutcome:
    started = now()
    url = source.url(board)
    res = fetcher.fetch(url)
    finished = now()
    blob_sha: str | None = None
    record_count: int | None = None
    blob_new = False
    error = res.error
    if res.transport == "ok":
        blob_sha = sha256_hex(res.body)
        try:
            record_count = sum(1 for _ in source.parse(res.body))
        except EnvelopeError as e:
            # Transport was fine, the body is not what the source promises. The raw
            # bytes are archived regardless; the manifest says loudly why it did not parse.
            record_count = None
            error = f"envelope: {e}"
        if not dry_run:
            blob_new = store.put(blob_key(blob_sha), gzip_bytes(res.body))
    manifest = AttemptManifest(
        attempt_id=attempt_key(source.name, board.board, started),
        run_id=run_id,
        source=source.name,
        board=board.board,
        started_at=started,
        finished_at=finished,
        url=url,
        http_status=res.status,
        transport=res.transport,
        blob_sha256=blob_sha,
        payload_bytes=len(res.body),
        record_count=record_count,
        adapter_version=source.adapter_version,
        registry_revision=registry_revision,
        cli_version=__version__,
        error=error,
    )
    if not dry_run and not write_manifest(store, manifest):
        # Manifests are write-once; a pre-existing key means two attempts for one board in
        # the same second (or a replayed clock). Silence would hide an observation.
        raise ArchiveError(f"manifest {manifest.attempt_id} already exists")
    return BoardOutcome(board=board, manifest=manifest, blob_new=blob_new)


# ---- two-phase boards (list + detail), spec 2026-09-04 §3.2/§3.4


@dataclass(frozen=True, slots=True)
class TwoPhaseBudget:
    """What one board may spend in one run."""

    page_cap: int = PAGE_CAP
    detail_budget: int = DETAIL_BUDGET
    redetail_days: int = REDETAIL_DAYS


DEFAULT_BUDGET = TwoPhaseBudget()

# Bot-challenge pages (Cloudflare, Imperva, PerimeterX). Only ever consulted for
# an HTML body — a JSON list page is never a challenge, whatever words it holds.
_CHALLENGE_MARKERS = (
    b"captcha",
    b"just a moment",
    b"cf-chl",
    b"/cdn-cgi/challenge",
    b"incapsula",
    b"perimeterx",
    b"enable javascript and cookies",
)


def looks_blocked(res: FetchResult) -> bool:
    """A 403 or a bot-challenge page. Policy §2: the board is skipped, never retried around."""
    if res.status == 403:
        return True
    head = res.body[:4096].lstrip().lower()
    if not head.startswith(b"<"):
        return False
    return any(marker in head for marker in _CHALLENGE_MARKERS)


@dataclass(slots=True)
class _ListPhase:
    rows: list[ListRow] = field(default_factory=list)
    page_blobs: list[str] = field(default_factory=list)
    bytes_read: int = 0
    http_status: int | None = None
    transport: str = "ok"
    error: str | None = None
    new_blob: bool = False

    @property
    def blocked(self) -> bool:
        return self.transport == "blocked"


def detail_history(store: ArchiveStore, source: str, board: str) -> dict[str, datetime]:
    """uid -> when its detail body was last archived, read back from this board's manifests.

    The archive is the only state the driver consults (the store is derived and may be
    a rebuild behind). A detail that failed leaves no entry, so the uid stays "new" and
    the next run retries it ahead of the staleness sweep.
    """
    last: dict[str, datetime] = {}
    for m in iter_manifests(store, source, board):
        for d in m.details or ():
            if d.blob_sha256 is None:
                continue
            seen = last.get(d.uid)
            if seen is None or m.started_at > seen:
                last[d.uid] = m.started_at
    return last


def pick_details(
    rows: Iterable[ListRow],
    history: dict[str, datetime],
    *,
    now: datetime,
    budget: TwoPhaseBudget,
) -> list[ListRow]:
    """New uids first (list order — sources list newest first), then the staleness sweep.

    Never more than `detail_budget` rows: a full detail sweep per run is forbidden
    (rejected option O-20260904-X6EC).
    """
    if budget.detail_budget <= 0:
        return []
    cutoff = now - timedelta(days=budget.redetail_days)
    new = [r for r in rows if r.uid not in history]
    stale = sorted(
        (r for r in rows if r.uid in history and history[r.uid] < cutoff),
        key=lambda r: history[r.uid],
    )
    return (new + stale)[: budget.detail_budget]


def _fetch_list(
    board: Board,
    source: TwoPhaseSource,
    fetcher: Fetcher,
    store: ArchiveStore,
    *,
    budget: TwoPhaseBudget,
    dry_run: bool,
) -> _ListPhase:
    """Page the list to `total`, archiving each page BEFORE it is parsed or used."""
    phase = _ListPhase()
    offset = 0
    seen: set[str] = set()
    for page_no in range(budget.page_cap):
        spec = source.list_url(board, offset)
        res = fetcher.fetch(spec.url, method=spec.method, json_body=spec.json_body)
        phase.bytes_read += len(res.body)
        phase.http_status = res.status
        if looks_blocked(res):
            phase.transport = "blocked"
            phase.error = (
                f"blocked: list page {page_no} returned HTTP {res.status} "
                "(403 or bot challenge); board skipped, not retried"
            )
            return phase
        if res.transport != "ok":
            phase.transport = res.transport
            phase.error = res.error if page_no == 0 else f"list page {page_no}: {res.error}"
            return phase
        sha = sha256_hex(res.body)
        if not dry_run:
            phase.new_blob |= store.put(blob_key(sha), gzip_bytes(res.body))
        phase.page_blobs.append(sha)
        try:
            page = source.parse_list(res.body)
        except EnvelopeError as e:
            phase.error = f"envelope: {e}"
            return phase
        for row in page.rows:
            if row.uid not in seen:  # a uid repeated across pages is one posting
                seen.add(row.uid)
                phase.rows.append(row)
        offset += len(page.rows)
        if not page.rows or offset >= page.total:
            return phase
    # Truncated coverage must never read as a complete snapshot.
    phase.error = (
        f"page cap: stopped after {budget.page_cap} pages with {len(phase.rows)} rows listed"
    )
    return phase


def _fetch_details(
    board: Board,
    source: TwoPhaseSource,
    fetcher: Fetcher,
    store: ArchiveStore,
    rows: list[ListRow],
    *,
    dry_run: bool,
) -> tuple[list[DetailAttempt], int, bool]:
    details: list[DetailAttempt] = []
    read = 0
    new_blob = False
    for row in rows:
        try:
            spec = source.detail_url(board, row)
            res = fetcher.fetch(spec.url, method=spec.method, json_body=spec.json_body)
        except Exception as e:  # an adapter bug on one row must not lose the whole board
            details.append(DetailAttempt(row.uid, None, None, f"{type(e).__name__}: {e}"))
            continue
        read += len(res.body)
        if res.transport != "ok":
            details.append(DetailAttempt(row.uid, None, res.status, res.error))
            if looks_blocked(res):
                break  # the board started refusing mid-run; stop asking (policy §2)
            continue
        sha = sha256_hex(res.body)
        if not dry_run:
            new_blob |= store.put(blob_key(sha), gzip_bytes(res.body))
        details.append(DetailAttempt(row.uid, sha, res.status, None))
    return details, read, new_blob


def fetch_board_two_phase(
    board: Board,
    source: TwoPhaseSource,
    fetcher: Fetcher,
    store: ArchiveStore,
    *,
    run_id: str,
    registry_revision: str,
    now: Callable[[], datetime],
    dry_run: bool,
    budget: TwoPhaseBudget = DEFAULT_BUDGET,
) -> BoardOutcome:
    """One attempt for a list+detail board: page the list, then spend the detail budget.

    One manifest per board per run as for single-phase boards, with `blob_sha256` null
    (there is no single body), `page_blobs` the ordered list pages, and `details` the
    detail fetches of this attempt. Every blob is archived before the manifest that
    names it, so nothing downstream can see a manifest whose bytes are missing.
    """
    started = now()
    url = source.list_url(board, 0).url
    listing = _fetch_list(board, source, fetcher, store, budget=budget, dry_run=dry_run)

    details: list[DetailAttempt] = []
    error = listing.error
    payload_bytes = listing.bytes_read
    blob_new = listing.new_blob
    if not listing.blocked and listing.rows:
        history = detail_history(store, source.name, board.board)
        picks = pick_details(listing.rows, history, now=started, budget=budget)
        details, read, detail_new = _fetch_details(
            board, source, fetcher, store, picks, dry_run=dry_run
        )
        payload_bytes += read
        blob_new = blob_new or detail_new

    manifest = AttemptManifest(
        attempt_id=attempt_key(source.name, board.board, started),
        run_id=run_id,
        source=source.name,
        board=board.board,
        started_at=started,
        finished_at=now(),
        url=url,
        http_status=listing.http_status,
        transport=listing.transport,
        blob_sha256=None,
        payload_bytes=payload_bytes,
        record_count=len(listing.rows) if listing.transport == "ok" else None,
        adapter_version=source.adapter_version,
        registry_revision=registry_revision,
        cli_version=__version__,
        error=error,
        page_blobs=tuple(listing.page_blobs),
        details=tuple(details),
    )
    if not dry_run and not write_manifest(store, manifest):
        raise ArchiveError(f"manifest {manifest.attempt_id} already exists")
    return BoardOutcome(board=board, manifest=manifest, blob_new=blob_new)


def run(
    settings: Settings,
    *,
    store: ArchiveStore | None = None,
    fetcher: Fetcher | None = None,
    only: str | None = None,
    dry_run: bool = False,
    now: Callable[[], datetime] = utcnow,
    concurrency: int = 4,
    ingest: bool = True,
    schema: str = _db.SCHEMA,
    ping: Callable[[str], None] = post_ping,
    budget: TwoPhaseBudget = DEFAULT_BUDGET,
) -> RunSummary:
    store = store or open_store(settings.archive_url)
    started = now()
    run_id = f"{iso(started).replace('-', '').replace(':', '')}-{secrets.token_hex(3)}"
    registry = load_registry(settings.registry_path)
    boards = [b for b in registry.boards if only is None or b.key == only]
    if only is not None and not boards:
        raise UnknownBoardError(f"board {only!r} is not in the registry")

    conn: _db.Conn | None = None
    db_error: str | None = None
    if ingest and not dry_run:
        dsn = settings.require_database_url()  # ConfigError propagates: the spec makes it required
        try:
            conn = _db.connect(dsn, schema=schema)
            if not _db.try_lock(conn):
                conn.close()
                return RunSummary(run_id, started, now(), registry.revision, [], lock_held=True)
            _db.init(conn, schema)
            conn.commit()
        except (psycopg.Error, OSError, _db.SchemaMismatch) as e:
            db_error = f"{type(e).__name__}: {e}"
            if conn is not None:
                conn.close()
            conn = None

    replayed = 0
    gaps: list[str] = []
    if conn is not None:
        # Drain manifests archived while the DB was unreachable BEFORE fetching, so the
        # watermark never advances past an unreplayed attempt (spec §8 recovery path).
        try:
            pending = replay_pending(conn, store, drop_ratio=settings.drop_ratio)
            replayed = pending.ingested
            gaps = pending.gaps
            conn.commit()
        except (psycopg.Error, OSError, OutOfOrder, ArchiveError) as e:
            with contextlib.suppress(Exception):
                conn.rollback()
            db_error = f"{type(e).__name__}: {e}"
            with contextlib.suppress(Exception):
                _db.unlock(conn)
            with contextlib.suppress(Exception):
                conn.close()
            conn = None

    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher()
    if not dry_run:
        store.put(registry_key(registry.revision), registry.snapshot_json())

    def one(board: Board) -> BoardOutcome:
        two_phase = get_two_phase(board.source)
        if two_phase is not None:
            return fetch_board_two_phase(
                board, two_phase, fetcher, store,
                run_id=run_id, registry_revision=registry.revision, now=now, dry_run=dry_run,
                budget=budget,
            )
        return fetch_board(
            board, get_source(board.source), fetcher, store,
            run_id=run_id, registry_revision=registry.revision, now=now, dry_run=dry_run,
        )

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            outcomes = list(pool.map(one, boards))
    finally:
        if own_fetcher:
            fetcher.close()

    # Collection liveness signal: sent once the fetch phase completed, whatever the
    # store did (the switch watches for no-run-at-all, not DB health). A ping
    # failure must never fail a run.
    if not dry_run and settings.ping_url:
        with contextlib.suppress(Exception):
            ping(settings.ping_url)

    ingested = 0
    if conn is not None:
        try:
            ing = Ingestor(conn, store, drop_ratio=settings.drop_ratio)
            for o in sorted(outcomes, key=lambda o: (o.manifest.started_at, o.manifest.attempt_id)):
                if ing.ingest(o.manifest) is not None:
                    ingested += 1
            conn.commit()
        except (psycopg.Error, OSError, OutOfOrder, ArchiveError) as e:
            # Each attempt is its own committed transaction (Ingestor.ingest); the rollback
            # only discards the failed one, so `ingested` keeps counting what landed. The
            # archive is already written either way; the caller reports db_error and exits 2.
            with contextlib.suppress(Exception):
                conn.rollback()
            db_error = f"{type(e).__name__}: {e}"
        finally:
            # Unlocking a dead connection must not mask the error that killed it; a session
            # lock dies with its session anyway.
            with contextlib.suppress(Exception):
                _db.unlock(conn)
            with contextlib.suppress(Exception):
                conn.close()
    return RunSummary(
        run_id=run_id, started_at=started, finished_at=now(), registry_revision=registry.revision,
        outcomes=outcomes, ingested=ingested, replayed=replayed, gaps=gaps, db_error=db_error,
    )
