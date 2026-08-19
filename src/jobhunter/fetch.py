"""One run: registry -> fetch every board -> archive manifest + blob -> ingest into the store."""

from __future__ import annotations

import gzip
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from jobhunter import __version__
from jobhunter.archive import ArchiveError, ArchiveStore, open_store
from jobhunter.archive.keys import attempt_key, blob_key, registry_key
from jobhunter.archive.manifests import write_manifest
from jobhunter.config import Settings
from jobhunter.hashing import sha256_hex
from jobhunter.http import Fetcher
from jobhunter.models import AttemptManifest, Board
from jobhunter.registry import load as load_registry
from jobhunter.sources import get_source
from jobhunter.sources.base import EnvelopeError, Source
from jobhunter.store import db as _db
from jobhunter.store.lifecycle import Ingestor
from jobhunter.timeutil import iso, utcnow


class UnknownBoardError(ValueError):
    """--board named a source:board that is not in the registry."""


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
        except (psycopg.Error, OSError) as e:
            db_error = f"{type(e).__name__}: {e}"
            if conn is not None:
                conn.close()
            conn = None

    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher()
    if not dry_run:
        store.put(registry_key(registry.revision), registry.snapshot_json())

    def one(board: Board) -> BoardOutcome:
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

    ingested = 0
    if conn is not None:
        try:
            ing = Ingestor(conn, store, drop_ratio=settings.drop_ratio)
            for o in sorted(outcomes, key=lambda o: (o.manifest.started_at, o.manifest.attempt_id)):
                if ing.ingest(o.manifest) is not None:
                    ingested += 1
            conn.commit()
        except (psycopg.Error, OSError) as e:
            # Each attempt is its own committed transaction (Ingestor.ingest); the rollback
            # only discards the failed one, so `ingested` keeps counting what landed.
            conn.rollback()
            db_error = f"{type(e).__name__}: {e}"
        finally:
            try:
                _db.unlock(conn)
            finally:
                conn.close()
    return RunSummary(
        run_id=run_id, started_at=started, finished_at=now(), registry_revision=registry.revision,
        outcomes=outcomes, ingested=ingested, db_error=db_error,
    )
