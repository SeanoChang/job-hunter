"""Read/write attempt manifests in the archive."""

from __future__ import annotations

from collections.abc import Iterator

from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.keys import attempts_prefix
from jobhunter.models import AttemptManifest


def write_manifest(store: ArchiveStore, m: AttemptManifest) -> bool:
    return store.put(m.attempt_id, m.to_json())


def iter_manifests(
    store: ArchiveStore,
    source: str | None = None,
    board: str | None = None,
    start_after: str | None = None,
) -> Iterator[AttemptManifest]:
    for key in store.list(attempts_prefix(source, board), start_after=start_after):
        if key.endswith(".json"):
            yield AttemptManifest.from_json(store.get(key))


def latest_per_board(store: ArchiveStore) -> dict[str, AttemptManifest]:
    latest: dict[str, AttemptManifest] = {}
    for m in iter_manifests(store):
        k = f"{m.source}:{m.board}"
        if k not in latest or m.started_at > latest[k].started_at:
            latest[k] = m
    return latest


def all_sorted_by_time(store: ArchiveStore) -> list[AttemptManifest]:
    return sorted(iter_manifests(store), key=lambda m: (m.started_at, m.attempt_id))
