"""Versioned board membership (spec §5.5), derived from archived registry snapshots."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.keys import registry_key
from jobhunter.models import Board
from jobhunter.store.db import Conn


@dataclass(slots=True)
class PanelDelta:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def boards_from_snapshot(data: bytes) -> tuple[Board, ...]:
    rows: list[dict[str, Any]] = json.loads(data.decode("utf-8"))
    return tuple(
        Board(
            company=r["company"],
            source=r["source"],
            board=r["board"],
            country=r.get("country"),
            tags=tuple(r.get("tags") or ()),
        )
        for r in rows
    )


def load_snapshot(store: ArchiveStore, revision: str) -> tuple[Board, ...]:
    return boards_from_snapshot(store.get(registry_key(revision)))


def apply_snapshot(conn: Conn, boards: Iterable[Board], at: datetime, revision: str) -> PanelDelta:
    wanted = {b.key: b for b in boards}
    open_rows = conn.execute("SELECT source, board FROM panel WHERE removed_at IS NULL").fetchall()
    open_keys = {f"{r['source']}:{r['board']}" for r in open_rows}
    delta = PanelDelta()
    for key in sorted(open_keys - wanted.keys()):
        source, board = key.split(":", 1)
        conn.execute(
            "UPDATE panel SET removed_at = %s "
            "WHERE source = %s AND board = %s AND removed_at IS NULL",
            (at, source, board),
        )
        delta.removed.append(key)
    for key in sorted(wanted.keys() - open_keys):
        b = wanted[key]
        conn.execute(
            "INSERT INTO panel (source, board, company, added_at, removed_at, registry_revision) "
            "VALUES (%s, %s, %s, %s, NULL, %s)",
            (b.source, b.board, b.company, at, revision),
        )
        delta.added.append(key)
    return delta
