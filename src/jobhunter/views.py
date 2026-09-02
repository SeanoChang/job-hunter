"""Payload assembly for the read surface: what every face of the corpus emits.

Each `q` verb used to shape its own rows inside its command body, which made
the CLI the only way to obtain a payload. That shaping lives here instead, as
functions that take a connection rather than a process: run the query, convert
the timestamps, name the board the way `--board` accepts it back, and hand the
caller the `data` an envelope carries plus the two facts a bounded read owes —
whether it truncated, and the cursor that continues it.

Nothing here imports typer or `cli_output`, and nothing here writes. Flag
validation stays with the flags: a view raises `ValueError` for a shape it
cannot use and returns `None` for an identifier the store does not know, and
the caller decides which exit code (or protocol error) that is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from jobhunter.cursors import Watermark
from jobhunter.pulse import build_pulse, closed_between, profile_summary
from jobhunter.store import queries
from jobhunter.timeutil import iso

if TYPE_CHECKING:
    from jobhunter.config import Settings
    from jobhunter.store.db import Conn


@dataclass(frozen=True)
class Page:
    """One view's answer: the payload, and how the read was bounded.

    `data` is a row list for the list views and a single object for the detail
    ones; `rows()` and `record()` narrow it for callers that know which they
    asked for.
    """

    data: list[dict[str, Any]] | dict[str, Any]
    truncated: bool = False
    next_cursor: str | None = None

    def rows(self) -> list[dict[str, Any]]:
        assert isinstance(self.data, list)  # a list view; a detail one has no rows
        return self.data

    def record(self) -> dict[str, Any]:
        assert isinstance(self.data, dict)  # a detail view; a list one has no record
        return self.data


def postings_view(
    conn: Conn,
    *,
    source: str | None = None,
    board: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    search: str | None = None,
    limit: int = 50,
    after: str | None = None,
) -> Page:
    """Postings newest first. The cursor is built from the last row actually
    emitted — never from a row the reader never saw."""
    rows = queries.postings_page(
        conn, source=source, board=board, status=status, since=since, search=search,
        limit=limit, after=after)
    truncated = len(rows) > limit
    rows = rows[:limit]
    data = [
        {"uid": r["uid"], "board": f"{r['source']}:{r['board']}", "status": r["status"],
         "title": r["title"], "company": r["company"], "url": r["url"],
         "first_seen_at": iso(r["first_seen_at"]), "last_seen_at": iso(r["last_seen_at"]),
         "version_count": r["version_count"], "reopen_count": r["reopen_count"],
         "closed_between": closed_between(r)}
        for r in rows
    ]
    cursor = (f"{rows[-1]['first_seen_at'].isoformat()}|{rows[-1]['uid']}"
              if truncated and rows else None)
    return Page(data, truncated=truncated, next_cursor=cursor)


def posting_view(conn: Conn, uid: str) -> Page | None:
    """One posting: lifecycle, close interval, version history, events, document.
    None when the store has no such uid."""
    row = queries.posting_detail(conn, uid)
    if row is None:
        return None
    data = {
        **{k: v for k, v in row.items()
           if k not in ("source", "closed_lower_at", "closed_upper_at", "versions", "events")},
        "board": f"{row['source']}:{row['board']}",
        "first_seen_at": iso(row["first_seen_at"]),
        "last_seen_at": iso(row["last_seen_at"]),
        "source_updated_at": iso(row["source_updated_at"]) if row["source_updated_at"] else None,
        "closed_between": closed_between(row),
        "versions": [
            {"version_hash": v["version_hash"], "title": v["title"], "at": iso(v["at"])}
            for v in row["versions"]
        ],
        "events": [
            {"event_id": e["event_id"], "kind": e["kind"], "at": iso(e["at"]),
             "from_version": e["from_version"], "to_version": e["to_version"],
             "closed_between": closed_between(e)}
            for e in row["events"]
        ],
    }
    return Page(data)


def events_view(
    conn: Conn,
    *,
    since: datetime | None = None,
    kinds: tuple[str, ...] | None = None,
    source: str | None = None,
    board: str | None = None,
    uid: str | None = None,
    limit: int = 50,
    after_event_id: int | None = None,
) -> Page:
    """Lifecycle events oldest first — what `pulse` reports, without the cursor."""
    rows = queries.events_page(
        conn, since=since, kinds=kinds, source=source, board=board, uid=uid, limit=limit,
        after_event_id=after_event_id)
    truncated = len(rows) > limit
    rows = rows[:limit]
    data = [
        {"event_id": e["event_id"], "kind": e["kind"], "uid": e["uid"], "at": iso(e["at"]),
         "board": f"{e['source']}:{e['board']}", "title": e["title"], "company": e["company"],
         "url": e["url"], "closed_between": closed_between(e)}
        for e in rows
    ]
    return Page(data, truncated=truncated,
                next_cursor=str(rows[-1]["event_id"]) if truncated and rows else None)


def boards_view(conn: Conn, *, unhealthy_only: bool = False, limit: int = 50) -> Page:
    """Per-board fetch health and open counts, one row per board the store knows."""
    rows = queries.boards_overview(conn)
    if unhealthy_only:
        rows = [r for r in rows if r["health"] != "ok"]
    truncated = len(rows) > limit
    rows = rows[:limit]
    data = [
        {"board": r["board"], "health": r["health"], "open": r["open"], "error": r["error"],
         "started_at": iso(r["started_at"]) if r["started_at"] else None}
        for r in rows
    ]
    return Page(data, truncated=truncated)


def parse_slice(value: str) -> tuple[int | None, int | None]:
    """`S:E` codepoint offsets, either side optional. ValueError on anything else."""
    start_s, sep, end_s = value.partition(":")
    if not sep:
        raise ValueError(value)
    return int(start_s) if start_s else None, int(end_s) if end_s else None


def document_view(conn: Conn, document_hash: str, *, slice_: str | None = None) -> Page | None:
    """The canonical markdown of one document — the text every quote span
    indexes. None when no document under the current normalizer has that hash."""
    from jobhunter.markdown import NORMALIZER_VERSION
    from jobhunter.store import extraction as xstore

    start, end = parse_slice(slice_) if slice_ is not None else (None, None)
    markdown = xstore.markdown_for(conn, document_hash, NORMALIZER_VERSION)
    if markdown is None:
        return None
    return Page({"document_hash": document_hash, "markdown": markdown[start:end]})


def profile_row(conn: Conn, document_hash: str) -> dict[str, Any] | None:
    """The row a profile is reported from: validated first, else the newest
    state, so a quarantined document can explain itself instead of looking
    absent."""
    return conn.execute(
        "SELECT e.status, e.model, e.prompt_version, e.profile, e.updated_at,"
        " v.title, v.company, v.url FROM extractions e"
        " LEFT JOIN documents d ON d.document_hash = e.document_hash"
        " LEFT JOIN posting_versions v ON v.version_hash = d.version_hash"
        " WHERE e.document_hash = %s"
        " ORDER BY (e.status = 'validated') DESC, e.updated_at DESC LIMIT 1",
        (document_hash,),
    ).fetchone()


def profile_payload(
    document_hash: str, row: dict[str, Any], *, full: bool = False
) -> dict[str, Any]:
    """One validated row as a payload: the digest, or the stored profile
    verbatim under `full` — quotes and spans are what `full` buys."""
    profile = row["profile"]
    return {
        "document_hash": document_hash, "status": row["status"], "model": row["model"],
        "prompt_version": row["prompt_version"], "updated_at": iso(row["updated_at"]),
        "title": row["title"], "company": row["company"], "url": row["url"],
        "profile": profile if full else profile_summary(profile),
    }


def profile_view(conn: Conn, document_hash: str, *, full: bool = False) -> Page | None:
    """The demand profile of one document. None when nothing validated it —
    `profile_row` says which of the two reasons that is, and callers that owe
    the reader a teaching message read it themselves."""
    row = profile_row(conn, document_hash)
    if row is None or row["status"] != "validated" or row["profile"] is None:
        return None
    return Page(profile_payload(document_hash, row, full=full))


def claims_view(
    conn: Conn,
    settings: Settings,
    *,
    mention: str,
    importance: str | None = None,
    source: str | None = None,
    board: str | None = None,
    limit: int = 50,
) -> Page:
    """Who demands one mention, across the corpus — the postings living on it
    today, scoped to the engine tuple in force exactly as `pulse` scopes its
    profiles: retired prompt/validator versions still sit in `profile_mentions`
    after a rebuild."""
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.runner import SCHEMA_VERSION
    from jobhunter.l2.state import globs_to_regex
    from jobhunter.l2.transforms import VALIDATOR_VERSION

    rows = queries.claims_by_mention(
        conn, mention=mention, importance=importance, source=source, board=board, limit=limit,
        model_regex=globs_to_regex(settings.l2_models), prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION, validator_version=VALIDATOR_VERSION)
    truncated = len(rows) > limit
    rows = rows[:limit]
    data = [
        {"document_hash": r["document_hash"], "mention": r["mention"],
         "area_kind": r["area_kind"], "importance": r["importance"], "uid": r["uid"],
         "board": f"{r['source']}:{r['board']}", "title": r["title"], "company": r["company"],
         "url": r["url"]}
        for r in rows
    ]
    return Page(data, truncated=truncated)


def pulse_view(
    conn: Conn,
    settings: Settings,
    *,
    wm: Watermark | None,
    since_iso: str | None,
    limit: int,
    boards: tuple[str, ...] | None = None,
    now: datetime,
) -> tuple[Page, Watermark | None]:
    """The delta since the watermark, plus the watermark the caller should store
    once the payload is out.

    `since_iso` reports a window instead: it replaces the watermark for this
    call and is not a first run, so a reader can ask for a fixed span without
    disturbing anyone's cursor.
    """
    start = Watermark(since_iso, ()) if since_iso is not None else wm
    payload, new_wm = build_pulse(
        conn, settings, wm=start, limit=limit, boards=boards, now=now
    )
    truncated = bool(payload.pop("_truncated"))
    return Page(payload, truncated=truncated), new_wm
