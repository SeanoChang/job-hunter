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


def active_tuple(settings: Settings) -> tuple[str, str, str]:
    """`(prompt_version, schema_version, validator_version)` of the bundle the
    settings name — the one definition of "the engine tuple in force", shared by
    every read path that scopes to it.

    The WRITE path has taken its tuple from the selected bundle since the v2
    contract landed (`l2/bundles.py`, `JOB_HUNTER_L2_BUNDLE`). The read paths
    used to compute theirs from the three v1 module constants instead, which was
    invisible only while `v1` was the selected bundle: the moment the settings
    name any other one, every extraction is written under that bundle's tuple
    while `q profile`, `q claims` and `pulse` keep asking for v1's, so a corpus
    extracting normally reads back empty. One helper, three call sites, so the
    read surface follows whatever bundle the settings name — including back
    again, since rollback is selecting the previous bundle and never deleting
    rows.
    """
    from jobhunter.l2.bundles import get_bundle

    bundle = get_bundle(settings.l2_bundle)
    return bundle.prompt_version, bundle.schema_version, bundle.validator_version


def profile_row(conn: Conn, settings: Settings, document_hash: str) -> dict[str, Any] | None:
    """The row a profile is reported from: the engine tuple in force first —
    a retired prompt's "validated" never outranks the current engine's verdict
    — then validated over the newest state, so a quarantined document can
    explain itself instead of looking absent. A row only a retired tuple left
    behind still surfaces, with `current_tuple` false so the payload can label
    it historical."""
    tup = active_tuple(settings)
    return conn.execute(
        "SELECT e.status, e.model, e.prompt_version, e.validator_version, e.profile,"
        " e.updated_at, v.title, v.company, v.url,"
        " (e.prompt_version, e.schema_version, e.validator_version) = (%s, %s, %s)"
        "   AS current_tuple"
        " FROM extractions e"
        " LEFT JOIN documents d ON d.document_hash = e.document_hash"
        " LEFT JOIN posting_versions v ON v.version_hash = d.version_hash"
        " WHERE e.document_hash = %s"
        " ORDER BY ((e.prompt_version, e.schema_version, e.validator_version) = (%s, %s, %s))"
        " DESC, (e.status = 'validated') DESC, e.updated_at DESC LIMIT 1",
        (*tup, document_hash, *tup),
    ).fetchone()


def profile_payload(
    document_hash: str, row: dict[str, Any], *, full: bool = False
) -> dict[str, Any]:
    """One serving row as a payload: the digest, or the stored profile verbatim
    under `full` — quotes and spans are what `full` buys.

    `status` and `quality` ride on the envelope, not inside the digest, because
    a `needs_review` row serves the same slice a validated one does (parsing
    contract v3 §4) and the two facts that separate them are which status it
    holds and what its samples split on (`quality.sample_notes`). An agent that
    had to pass `--full` to learn it was reading a review row would read most of
    them without knowing.
    """
    profile = row["profile"]
    quality = profile.get("quality") if isinstance(profile, dict) else None
    return {
        "document_hash": document_hash, "status": row["status"], "model": row["model"],
        "prompt_version": row["prompt_version"],
        "validator_version": row["validator_version"],
        "historical": not row["current_tuple"],
        "updated_at": iso(row["updated_at"]),
        "title": row["title"], "company": row["company"], "url": row["url"],
        "quality": quality,
        "profile": profile if full else profile_summary(profile),
    }


def profile_view(
    conn: Conn, settings: Settings, document_hash: str, *, full: bool = False
) -> Page | None:
    """The demand profile of one document. None when nothing serves it —
    `profile_row` says which of the two reasons that is, and callers that owe
    the reader a teaching message read it themselves.

    Serving is the store's own predicate (`extraction.SERVING_STATUSES`): the
    aggregate and the blob are refilled for the same rows this answers from, so
    `q claims` can never name a document `q profile` then refuses to explain.
    """
    from jobhunter.store.extraction import SERVING_STATUSES

    row = profile_row(conn, settings, document_hash)
    if row is None or row["status"] not in SERVING_STATUSES or row["profile"] is None:
        return None
    return Page(profile_payload(document_hash, row, full=full))


def mention_context(profile: Any, surface: str, area_kind: str) -> dict[str, Any]:
    """What a schema-3 record says AROUND one (mention, kind) row: the section
    heading that row's statement sits under, and the modal phrase it quotes.

    The three columns of `profile_mentions` predate both fields and schema.sql
    is not this increment's to change, so the context is read back off the
    served blob — the only place it is durable.

    `area_kind` is half the key, not decoration. `serve.mention_rows` emits one
    row per (mention, STATEMENT) pair and puts the statement's kind in
    `area_kind`, so a mention supporting a qualification and a responsibility is
    two rows differing only there. Answering both from the first statement the
    mention links puts one statement's heading and modal phrase in the other's
    mouth — exactly the misattribution schema 3 exists to prevent — so the match
    is on the pair: among the statements this surface links, the first IN RECORD
    ORDER whose `kind` is `area_kind`. Record order (the blob's `statements`
    list, not the mention's link list) is the tie-break when a mention links
    several statements of one kind, because three columns can carry only one
    answer and the choice has to be the same on every read.

    Returns the two keys only when such a statement exists; `{}` otherwise —
    absence IS the signal that this row speaks the older vocabulary and its
    verdict columns are the ones to read (a v1 blob has no `statements` at all,
    a schema-2 one has statements whose verdicts live in `importance`/
    `proficiency`). A schema-3 statement that sits under no heading and quotes
    no modal phrase still returns both keys, set to None: present-and-null is a
    schema-3 row with nothing to say, missing is a row that says it differently.

    Defensive throughout: the argument is a stored blob, guaranteed only to
    match the schema of the day it was written.
    """
    if not isinstance(profile, dict):
        return {}
    wanted = surface.casefold()
    linked: set[str] = set()
    for entry in profile.get("mentions") or []:
        if not isinstance(entry, dict) or str(entry.get("surface", "")).casefold() != wanted:
            continue
        linked.update(
            sid for sid in entry.get("statement_ids") or [] if isinstance(sid, str)
        )
    for statement in profile.get("statements") or []:
        if not isinstance(statement, dict) or statement.get("id") not in linked:
            continue
        if statement.get("kind") != area_kind or "section_heading" not in statement:
            continue  # another kind's row, or schema 2: verdicts, not headings
        refs = statement.get("modality_evidence") or []
        quote = refs[0].get("text") if isinstance(refs, list) and refs else None
        return {
            "section_heading": statement["section_heading"],
            "modality": quote if isinstance(quote, str) else None,
        }
    return {}


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
    after a rebuild.

    Each row says which tier it came from (`extraction_status`: a review row
    serves alongside a validated one, parsing contract v3 §4) and carries the
    two fields a schema-3 claim replaced its verdict with. Those come from the
    served blobs of the documents on THIS page — one blob per DISTINCT document,
    at most `limit` of them, in one query, loaded the way `pulse` already loads a
    blob per profiled event — never one fetch per row and never a second pass
    over the corpus.

    `section_heading` and `modality` are present (possibly null) on a schema-3
    row and ABSENT on an older one, which is how a renderer tells the two apart
    without guessing: `NO_IMPORTANCE` is the string `contextual`, so a schema-3
    row's `importance` column reads like a verdict it never issued, and only the
    presence of these keys says not to print it. `schema_version` rides along as
    the partition the row was written under.
    """
    from jobhunter.l2.state import globs_to_regex

    prompt_version, schema_version, validator_version = active_tuple(settings)
    engine = {
        "model_regex": globs_to_regex(settings.l2_models), "prompt_version": prompt_version,
        "schema_version": schema_version, "validator_version": validator_version,
    }
    rows = queries.claims_by_mention(
        conn, mention=mention, importance=importance, source=source, board=board, limit=limit,
        **engine)
    truncated = len(rows) > limit
    rows = rows[:limit]
    served = queries.served_profiles(
        conn, sorted({r["document_hash"] for r in rows}), **engine)
    data: list[dict[str, Any]] = []
    for r in rows:
        blob = served.get(r["document_hash"]) or {}
        data.append(
            {"document_hash": r["document_hash"], "mention": r["mention"],
             "area_kind": r["area_kind"], "importance": r["importance"],
             "schema_version": r["schema_version"],
             "extraction_status": blob.get("status"),
             **mention_context(blob.get("profile"), r["mention"], r["area_kind"]),
             "uid": r["uid"], "board": f"{r['source']}:{r['board']}", "title": r["title"],
             "company": r["company"], "url": r["url"]}
        )
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
