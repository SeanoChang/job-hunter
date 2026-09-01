"""Read helpers for the CLI. Plain SQL over the derived tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jobhunter.store.db import Conn
from jobhunter.timeutil import parse_iso

# Every event row an agent sees: the event, the board it belongs to, and the
# version it points at (the new one for changes/opens, the last known one for
# closes, which carry no to_version).
_EVENT_SELECT = (
    "SELECT e.event_id, e.kind, e.uid, e.at, e.from_version, e.to_version, "
    "e.closed_lower_at, e.closed_upper_at, p.source, p.board, v.title, v.company, v.url "
    "FROM posting_events e "
    "JOIN postings p ON p.uid = e.uid "
    "LEFT JOIN posting_versions v ON v.uid = e.uid "
    "AND v.version_hash = COALESCE(e.to_version, p.current_version_hash) "
)


def _split_cursor(after: str) -> tuple[datetime, str]:
    """`postings_page`'s opaque cursor: "<iso first_seen_at>|<uid>"."""
    ts, sep, uid = after.partition("|")
    if not sep or not uid:
        raise ValueError(f"malformed cursor: {after!r}")
    return parse_iso(ts), uid


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


# ---- agent-facing reads (2026-09-01 CLI rework §3/§4): every list is bounded,
# every page returns limit+1 rows so its caller can report truncation honestly.


def postings_page(
    conn: Conn,
    *,
    source: str | None = None,
    board: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    search: str | None = None,
    limit: int = 50,
    after: str | None = None,
) -> list[dict[str, Any]]:
    """One page of postings, newest first, with the current version's fields.

    Returns up to limit+1 rows on purpose: the caller emits `limit` of them,
    marks `truncated`, and builds the next cursor from the last row it emitted
    — never from a row the reader never saw."""
    params: dict[str, Any] = {"limit": limit + 1}
    where: list[str] = []
    if source is not None:
        where.append("p.source = %(source)s")
        params["source"] = source
    if board is not None:
        where.append("p.board = %(board)s")
        params["board"] = board
    if status is not None:
        where.append("p.status = %(status)s")
        params["status"] = status
    if since is not None:
        where.append("p.first_seen_at >= %(since)s")
        params["since"] = since
    if search:
        where.append("(v.title ILIKE %(q)s OR v.company ILIKE %(q)s)")
        params["q"] = f"%{search}%"
    if after is not None:
        params["cur_at"], params["cur_uid"] = _split_cursor(after)
        where.append("(p.first_seen_at, p.uid) < (%(cur_at)s::timestamptz, %(cur_uid)s::text)")
    return conn.execute(
        "SELECT p.uid, p.source, p.board, p.status, p.version_count, p.reopen_count, "
        "p.first_seen_at, p.last_seen_at, p.closed_lower_at, p.closed_upper_at, "
        "v.title, v.company, v.url "
        "FROM postings p "
        "LEFT JOIN posting_versions v ON v.uid = p.uid "
        "AND v.version_hash = p.current_version_hash "
        f"WHERE {' AND '.join(where) if where else 'TRUE'} "
        "ORDER BY p.first_seen_at DESC, p.uid DESC LIMIT %(limit)s",
        params,
    ).fetchall()


def posting_detail(conn: Conn, uid: str) -> dict[str, Any] | None:
    """One posting: lifecycle fields, version history, events, current document.
    None when the uid is unknown — the CLI turns that into exit 4."""
    row = conn.execute(
        "SELECT p.uid, p.source, p.board, p.source_id, p.status, p.current_version_hash, "
        "p.version_count, p.reopen_count, p.first_seen_at, p.last_seen_at, "
        "p.closed_lower_at, p.closed_upper_at, p.source_updated_at, "
        "v.title, v.company, v.url, v.apply_url, v.locations, v.workplace_type, "
        "v.is_remote, v.department, v.team, v.employment_type, v.compensation "
        "FROM postings p "
        "LEFT JOIN posting_versions v ON v.uid = p.uid "
        "AND v.version_hash = p.current_version_hash "
        "WHERE p.uid = %s",
        (uid,),
    ).fetchone()
    if row is None:
        return None
    row["versions"] = conn.execute(
        "SELECT v.version_hash, v.title, a.started_at AS at FROM posting_versions v "
        "JOIN fetch_attempts a ON a.attempt_id = v.first_seen_attempt "
        "WHERE v.uid = %s ORDER BY a.started_at, v.version_hash",
        (uid,),
    ).fetchall()
    row["events"] = conn.execute(
        "SELECT event_id, kind, at, from_version, to_version, closed_lower_at, closed_upper_at "
        "FROM posting_events WHERE uid = %s ORDER BY event_id",
        (uid,),
    ).fetchall()
    doc = conn.execute(  # newest normalizer by name; md/1 is the only one so far
        "SELECT document_hash FROM documents WHERE version_hash = %s "
        "ORDER BY normalizer_version DESC LIMIT 1",
        (row["current_version_hash"],),
    ).fetchone()
    row["document_hash"] = doc["document_hash"] if doc else None
    return row


def events_page(
    conn: Conn,
    *,
    since: datetime | None = None,
    kinds: tuple[str, ...] | None = None,
    source: str | None = None,
    board: str | None = None,
    uid: str | None = None,
    limit: int = 50,
    after_event_id: int | None = None,
) -> list[dict[str, Any]]:
    """Lifecycle events oldest first, keyed on event_id. Returns limit+1 rows."""
    params: dict[str, Any] = {"limit": limit + 1}
    where: list[str] = []
    if since is not None:
        where.append("e.at >= %(since)s")
        params["since"] = since
    if kinds:
        where.append("e.kind = ANY(%(kinds)s::text[])")
        params["kinds"] = list(kinds)
    if source is not None:
        where.append("p.source = %(source)s")
        params["source"] = source
    if board is not None:
        where.append("p.board = %(board)s")
        params["board"] = board
    if uid is not None:
        where.append("e.uid = %(uid)s")
        params["uid"] = uid
    if after_event_id is not None:
        where.append("e.event_id > %(after)s")
        params["after"] = after_event_id
    return conn.execute(
        _EVENT_SELECT
        + f"WHERE {' AND '.join(where) if where else 'TRUE'} "
        "ORDER BY e.event_id LIMIT %(limit)s",
        params,
    ).fetchall()


def events_after_watermark(
    conn: Conn, *, at: datetime, exclude_ids: tuple[int, ...], limit: int
) -> list[dict[str, Any]]:
    """`pulse`'s delta feed: everything after the watermark instant, plus the
    events at that exact instant not yet reported. Ordered by (at, event_id),
    never by event_id alone — `rebuild` regenerates ids but reproduces `at`, so
    the timestamp is the only stable half of the watermark."""
    return conn.execute(
        _EVENT_SELECT
        + "WHERE (e.at > %(at)s OR (e.at = %(at)s "
        "AND NOT e.event_id = ANY(%(ids)s::bigint[]))) "
        "ORDER BY e.at, e.event_id LIMIT %(limit)s",
        {"at": at, "ids": list(exclude_ids), "limit": limit + 1},
    ).fetchall()


def boards_overview(conn: Conn) -> list[dict[str, Any]]:
    """One row per board the store knows: latest fetch health joined to the open
    count. A board present in only one of the two still gets a row — omitting it
    would read as a healthy board with nothing to report."""
    health = board_health(conn)
    opens = open_counts(conn)
    rows: list[dict[str, Any]] = []
    for key in sorted(set(health) | set(opens)):
        h = health.get(key)
        rows.append({
            "board": key,
            "health": h["health"] if h else None,
            "open": opens.get(key, 0),
            "error": h["error"] if h else None,
            "started_at": h["started_at"] if h else None,
        })
    return rows


def docs_for_events(conn: Conn, uids: list[str], normalizer_version: str) -> dict[str, str]:
    """uid -> document_hash of that posting's CURRENT version. Postings whose
    current text has no document under this normalizer are simply absent."""
    if not uids:
        return {}
    rows = conn.execute(
        "SELECT p.uid, d.document_hash FROM postings p "
        "JOIN documents d ON d.version_hash = p.current_version_hash "
        "AND d.normalizer_version = %(nv)s "
        "WHERE p.uid = ANY(%(uids)s::text[])",
        {"nv": normalizer_version, "uids": list(uids)},
    ).fetchall()
    return {r["uid"]: r["document_hash"] for r in rows}


def claims_by_mention(
    conn: Conn,
    *,
    mention: str,
    importance: str | None = None,
    source: str | None = None,
    board: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Who demands X, across the corpus, from the derived `profile_mentions`.

    Matching is case-insensitive on the whole mention — models spell "python"
    and "Python" both, and an agent that has to guess the casing would read the
    corpus as empty. That costs the (mention, importance) index a scan the size
    of the mention table, which is the cheaper of the two mistakes.

    A row is reported only while some posting's CURRENT version is the document
    the claim was extracted from: a claim attached to superseded text is history,
    not demand. DISTINCT because a document extracted under two engine tuples
    asserts the mention once, not twice. Returns limit+1 rows so the caller can
    mark truncation."""
    params: dict[str, Any] = {"mention": mention, "limit": limit + 1}
    where = ["lower(m.mention) = lower(%(mention)s)"]
    if importance is not None:
        where.append("m.importance = %(importance)s")
        params["importance"] = importance
    if source is not None:
        where.append("p.source = %(source)s")
        params["source"] = source
    if board is not None:
        where.append("p.board = %(board)s")
        params["board"] = board
    return conn.execute(
        "SELECT DISTINCT m.document_hash, m.mention, m.area_kind, m.importance, "
        "p.uid, p.source, p.board, p.last_seen_at, v.title, v.company, v.url "
        "FROM profile_mentions m "
        "JOIN documents d ON d.document_hash = m.document_hash "
        "JOIN posting_versions v ON v.version_hash = d.version_hash "
        "JOIN postings p ON p.uid = v.uid AND p.current_version_hash = v.version_hash "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY p.last_seen_at DESC, p.uid, m.mention LIMIT %(limit)s",
        params,
    ).fetchall()


def validated_profiles(
    conn: Conn,
    doc_hashes: list[str],
    *,
    model_regex: str,
    prompt_version: str,
    schema_version: str,
    validator_version: str,
) -> dict[str, dict[str, Any]]:
    """document_hash -> profile, for validated rows under the engine tuple in
    force. Anything else (needs_review, quarantined, another model) is not a
    fact this corpus asserts, so it is not returned at all."""
    if not doc_hashes:
        return {}
    rows = conn.execute(
        "SELECT document_hash, profile FROM extractions "
        "WHERE document_hash = ANY(%(hashes)s::text[]) AND status = 'validated' "
        "AND prompt_version = %(pv)s AND schema_version = %(sv)s "
        "AND validator_version = %(vv)s AND model ~ %(model_regex)s "
        "AND profile IS NOT NULL",
        {
            "hashes": list(doc_hashes), "pv": prompt_version, "sv": schema_version,
            "vv": validator_version, "model_regex": model_regex,
        },
    ).fetchall()
    return {r["document_hash"]: r["profile"] for r in rows}
