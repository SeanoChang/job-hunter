"""Rebuild the store from the archive into a fresh schema, then swap it live."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import sql

from jobhunter.archive.base import ArchiveStore
from jobhunter.ingest import replay_pending
from jobhunter.store import db, mcp_state


class LockHeld(RuntimeError):
    """Another writer holds the advisory lock; nothing was rebuilt."""


@dataclass(slots=True)
class RebuildSummary:
    ingested: int
    skipped: int
    work_schema: str
    swapped: bool
    cursors_carried: int = 0
    grants_reapplied: int = 0


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
            # A fresh schema has an empty ACL, so the swap would strip the reader
            # and MCP roles of everything they were granted. Read that off the
            # live schema first: an unreplayable privilege should stop the rebuild
            # before it spends the archive, not after.
            grants = db.capture_grants(conn, schema)
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
            # The watermarks are not in the archive, so they are copied rather than
            # replayed — as late as possible, since the hosted server goes on
            # advancing them while the replay runs. Carry, grant and swap share one
            # transaction: the new schema goes live with its privileges already on.
            carried = mcp_state.carry_cursors(conn, src=schema, dst=work)
            reapplied = db.apply_grants(conn, work, grants)
            db.swap_schema(conn, new=work, target=schema, previous=f"{schema}_previous")
            conn.commit()
            return RebuildSummary(
                ingested=s.ingested, skipped=s.skipped, work_schema=work, swapped=True,
                cursors_carried=carried, grants_reapplied=reapplied,
            )
        finally:
            db.unlock(conn)
    finally:
        conn.close()
