"""One run: registry -> fetch every board -> archive manifest + blob. No database here."""

from __future__ import annotations

import gzip
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jobhunter import __version__
from jobhunter.archive import ArchiveStore, open_store
from jobhunter.archive.keys import attempt_key, blob_key, registry_key
from jobhunter.archive.manifests import write_manifest
from jobhunter.config import Settings
from jobhunter.hashing import sha256_hex
from jobhunter.http import Fetcher
from jobhunter.models import AttemptManifest, Board
from jobhunter.registry import load as load_registry
from jobhunter.sources import get_source
from jobhunter.sources.base import EnvelopeError, Source
from jobhunter.timeutil import iso, utcnow


def gzip_bytes(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


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

    def counts(self) -> dict[str, int]:
        ok = sum(o.manifest.transport == "ok" for o in self.outcomes)
        http_error = sum(o.manifest.transport == "http_error" for o in self.outcomes)
        return {
            "boards": len(self.outcomes),
            "ok": ok,
            "http_error": http_error,
            "transport_error": len(self.outcomes) - ok - http_error,
            "new_blobs": sum(o.blob_new for o in self.outcomes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "registry_revision": self.registry_revision,
            "counts": self.counts(),
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
    if res.transport == "ok":
        blob_sha = sha256_hex(res.body)
        try:
            record_count = sum(1 for _ in source.parse(res.body))
        except EnvelopeError:
            record_count = None
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
        error=res.error,
    )
    if not dry_run:
        write_manifest(store, manifest)
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
) -> RunSummary:
    store = store or open_store(settings.archive_url)
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher()
    started = now()
    run_id = f"{iso(started).replace('-', '').replace(':', '')}-{secrets.token_hex(3)}"
    registry = load_registry(settings.registry_path)
    if not dry_run:
        store.put(registry_key(registry.revision), registry.snapshot_json())
    boards = [b for b in registry.boards if only is None or b.key == only]

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
    return RunSummary(
        run_id=run_id, started_at=started, finished_at=now(),
        registry_revision=registry.revision, outcomes=outcomes,
    )
