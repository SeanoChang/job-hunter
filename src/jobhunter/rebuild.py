"""Rebuild the store from the archive into a fresh schema, then swap it live."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import sql

from jobhunter.archive.base import ArchiveStore
from jobhunter.ingest import replay_pending
from jobhunter.store import db


class LockHeld(RuntimeError):
    """Another writer holds the advisory lock; nothing was rebuilt."""


@dataclass(slots=True)
class RebuildSummary:
    ingested: int
    skipped: int
    work_schema: str
    swapped: bool


def rebuild(
    store: ArchiveStore,
    dsn: str,
    *,
    drop_ratio: float = 0.5,
    schema: str = db.SCHEMA,
    work_schema: str | None = None,
    l2_globs: tuple[str, ...] = ("*",),
) -> RebuildSummary:
    """Replay the whole archive into `work_schema`, then make it the live `schema`."""
    work = work_schema or f"{schema}_new"
    conn = db.connect(dsn, schema=work)
    try:
        if not db.try_lock(conn):
            raise LockHeld("already running (advisory lock held)")
        try:
            with conn.transaction():
                conn.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(work))
                )
            db.init(conn, work)
            conn.commit()
            s = replay_pending(conn, store, drop_ratio=drop_ratio)
            conn.commit()
            # the L2 surface is part of the store: swapping without replaying it
            # would put empty extraction tables live and re-spend the corpus
            from jobhunter.l2.rebuild import rebuild_extractions

            rebuild_extractions(conn, store, l2_globs)
            conn.commit()
            db.swap_schema(conn, new=work, target=schema, previous=f"{schema}_previous")
            conn.commit()
            return RebuildSummary(
                ingested=s.ingested, skipped=s.skipped, work_schema=work, swapped=True
            )
        finally:
            db.unlock(conn)
    finally:
        conn.close()
