"""Read helpers for the CLI. Plain SQL over the derived tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jobhunter.store.db import Conn


def events_since(conn: Conn, since: datetime) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT e.event_id, e.kind, e.uid, e.at, e.from_version, e.to_version, "
        "e.closed_lower_at, e.closed_upper_at, v.title, v.company, v.url "
        "FROM posting_events e "
        "JOIN postings p ON p.uid = e.uid "
        "LEFT JOIN posting_versions v ON v.uid = e.uid "
        "AND v.version_hash = COALESCE(e.to_version, p.current_version_hash) "
        "WHERE e.at >= %s ORDER BY e.event_id",
        (since,),
    ).fetchall()


def panel_rows(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT source, board, company, added_at, removed_at, registry_revision "
        "FROM panel ORDER BY source, board, added_at"
    ).fetchall()


def board_health(conn: Conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT DISTINCT ON (source, board) source, board, health, observed_count, started_at, "
        "error FROM fetch_attempts ORDER BY source, board, started_at DESC"
    ).fetchall()
    return {f"{r['source']}:{r['board']}": r for r in rows}


def open_counts(conn: Conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT source, board, count(*) AS n FROM postings WHERE status = 'open' "
        "GROUP BY source, board"
    ).fetchall()
    return {f"{r['source']}:{r['board']}": int(r["n"]) for r in rows}
