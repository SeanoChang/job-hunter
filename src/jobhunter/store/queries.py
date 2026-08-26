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


def database_size(conn: Conn) -> int:
    """Bytes used by this database (spec §8: status reports size against the plan limit)."""
    row = conn.execute("SELECT pg_database_size(current_database()) AS n").fetchone()
    assert row is not None
    return int(row["n"])


def extraction_status(
    conn: Conn,
    *,
    prompt_version: str,
    schema_version: str,
    validator_version: str,
    model_regex: str,
    normalizer_version: str,
) -> dict[str, Any]:
    """The status command's extraction block (harness spec §9). Pending work has
    no row by design, so queue depth is computed, not counted from statuses —
    otherwise a fully-queued corpus reports as an empty, healthy surface."""
    depth_row = conn.execute(
        """
        WITH satisfied AS (
          SELECT document_hash FROM extractions
          WHERE prompt_version = %(pv)s AND schema_version = %(sv)s
            AND validator_version = %(vv)s AND model ~ %(model_regex)s
        )
        SELECT count(DISTINCT d.document_hash) AS n
        FROM documents d
        JOIN posting_versions v ON v.version_hash = d.version_hash
        JOIN postings p ON p.uid = v.uid
        WHERE d.normalizer_version = %(nv)s
          AND d.document_hash NOT IN (SELECT document_hash FROM satisfied)
        """,
        {
            "pv": prompt_version, "sv": schema_version, "vv": validator_version,
            "model_regex": model_regex, "nv": normalizer_version,
        },
    ).fetchone()
    by_status = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, count(*) AS n FROM extractions GROUP BY status"
        ).fetchall()
    }
    outcomes_7d = {
        r["outcome"]: r["n"]
        for r in conn.execute(
            "SELECT outcome, count(*) AS n FROM extraction_attempts "
            "WHERE started_at > now() - interval '7 days' GROUP BY outcome"
        ).fetchall()
    }
    models_7d = [
        r["observed_model"]
        for r in conn.execute(
            "SELECT DISTINCT observed_model FROM extraction_attempts "
            "WHERE started_at > now() - interval '7 days' AND observed_model IS NOT NULL "
            "ORDER BY observed_model"
        ).fetchall()
    ]
    spend = conn.execute(
        "SELECT COALESCE(sum(cost_usd) FILTER (WHERE started_at::date = now()::date), 0) AS today,"
        " COALESCE(sum(cost_usd) FILTER (WHERE date_trunc('month', started_at) ="
        " date_trunc('month', now())), 0) AS month FROM extraction_attempts"
    ).fetchone()
    oldest = conn.execute(
        "SELECT min(updated_at) AS at FROM extractions"
        " WHERE status IN ('needs_review', 'quarantined')"
    ).fetchone()
    return {
        "queue_depth": int(depth_row["n"]) if depth_row else 0,
        "by_status": by_status,
        "outcomes_7d": outcomes_7d,
        "observed_models_7d": models_7d,
        "spend_today_usd": float(spend["today"]) if spend else 0.0,
        "spend_month_usd": float(spend["month"]) if spend else 0.0,
        "oldest_review_at": oldest["at"].isoformat() if oldest and oldest["at"] else None,
    }
