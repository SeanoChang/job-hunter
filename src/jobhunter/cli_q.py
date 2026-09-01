"""Read-only `q` namespace: the agent-facing query surface (spec §4).

Every list verb is bounded (`--limit`, hard cap 500), always reports
`meta.truncated`, and hands back an opaque `--after` cursor built from the last
row it actually emitted — never from a row the reader never saw. Nothing here
writes, so these verbs run on a read-only Postgres role.

`jobhunter.cli` is imported inside the command bodies on purpose: it imports
this module to mount the sub-app, and its `_schema` is a test seam that must be
read at call time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import typer

from jobhunter.cli_output import Exit, emit, fail, output_option
from jobhunter.pulse import closed_between, profile_summary
from jobhunter.timeutil import iso, parse_iso

if TYPE_CHECKING:
    from jobhunter.config import Settings
    from jobhunter.store.db import Conn

q_app = typer.Typer(help="Read the corpus: postings, events, boards, documents, profiles")

MAX_LIMIT = 500
EVENT_KINDS = ("opened", "changed", "closed", "reopened")
IMPORTANCES = ("required", "preferred", "contextual")  # record.schema.json v1


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _open(output: str | None) -> tuple[Settings, Conn]:
    from jobhunter import cli

    settings = cli._settings(output)
    return settings, cli._conn(settings, schema=cli._schema, output=output)


def _query[T](conn: Conn, output: str | None, fn: Callable[[], T]) -> T:
    """Run the read and close the connection, whatever happens."""
    try:
        return fn()
    except typer.Exit:
        raise  # a fail() inside the block already chose its kind and code
    except Exception as e:
        fail("backend", f"database error: {e}", code=Exit.BACKEND, output=output)
    finally:
        conn.close()


def _select_fields(
    rows: list[dict[str, Any]], fields: str | None, output: str | None
) -> list[dict[str, Any]]:
    """`--fields` is the token-cost lever; an unknown name enumerates the real ones."""
    if not fields:
        return rows
    want = [f.strip() for f in fields.split(",") if f.strip()]
    if rows:
        unknown = [f for f in want if f not in rows[0]]
        if unknown:
            fail("usage", f"unknown field(s): {', '.join(unknown)}",
                 valid=sorted(rows[0]), code=Exit.USAGE, output=output)
    return [{k: r[k] for k in want if k in r} for r in rows]


def _check_after(after: str | None, output: str | None) -> None:
    """A hand-made cursor is a usage error, not a database one: `postings_page`
    would otherwise blow up deep inside the query with a bare ValueError."""
    if after is None:
        return
    ts, sep, uid = after.partition("|")
    ok = bool(sep and uid)
    if ok:
        try:
            parse_iso(ts)
        except ValueError:
            ok = False
    if not ok:
        fail("usage", f"--after is not a cursor: {after!r}", code=Exit.USAGE, output=output,
             hint="pass meta.next_cursor from the previous page verbatim")


def _since(value: str | None) -> Any:
    """Relative window (`Nm`/`Nh`/`Nd`) resolved against the CLI's clock."""
    from jobhunter.cli import _now, _parse_since

    return _now() - _parse_since(value) if value else None


@q_app.command("postings")
def q_postings(
    board: str | None = typer.Option(None, "--board", help="source:board"),
    status: str | None = typer.Option(None, "--status", help="open|closed"),
    since: str | None = typer.Option(None, "--since", help="Nm, Nh or Nd (first seen)"),
    search: str | None = typer.Option(None, "--search", help="ILIKE over title+company"),
    fields: str | None = typer.Option(None, "--fields", help="Comma list of keys to keep"),
    limit: int = typer.Option(50, "--limit", help=f"1-{MAX_LIMIT}"),
    after: str | None = typer.Option(None, "--after", help="Opaque cursor from meta.next_cursor"),
    output: str | None = output_option(),
) -> None:
    """List postings, newest first. Bounded; meta.truncated + meta.next_cursor page on."""
    from jobhunter.cli import _split_board
    from jobhunter.store import queries

    if status not in (None, "open", "closed"):
        fail("usage", f"--status must be open or closed: {status!r}",
             valid=["open", "closed"], code=Exit.USAGE, output=output)
    src, brd = _split_board(board, output)
    _check_after(after, output)
    limit = _clamp(limit)
    window = _since(since)
    _, conn = _open(output)
    rows = _query(conn, output, lambda: queries.postings_page(
        conn, source=src, board=brd, status=status, since=window, search=search,
        limit=limit, after=after))
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
    human = "\n".join(
        f"{r['status']:6} {r['uid']:32} {(r['company'] or '-'):18} {r['title'] or '-'}"
        for r in data
    ) or "(no postings)"
    cursor = (f"{rows[-1]['first_seen_at'].isoformat()}|{rows[-1]['uid']}"
              if truncated and rows else None)
    emit(_select_fields(data, fields, output), human=human, output=output, count=len(data),
         truncated=truncated, next_cursor=cursor,
         hint=f"q posting {rows[0]['uid']} for lifecycle detail" if rows else None)


@q_app.command("posting")
def q_posting(
    uid: str = typer.Argument(..., help="Posting uid, e.g. ab:ramp:x"),
    output: str | None = output_option(),
) -> None:
    """One posting: lifecycle, close interval, version history, events, document."""
    from jobhunter.store import queries

    _, conn = _open(output)
    row = _query(conn, output, lambda: queries.posting_detail(conn, uid))
    if row is None:
        fail("not_found", f"no posting {uid!r}", code=Exit.NOT_FOUND, output=output,
             hint="find uids with: q postings --search <text>")
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
    lines = [
        f"{data['title'] or '?'} — {data['company'] or '?'}  [{data['board']}]",
        f"{uid}  {data['status']}  {data['version_count']} version(s), "
        f"{data['reopen_count']} reopen(s)",
        f"seen {data['first_seen_at']} .. {data['last_seen_at']}"
        + (f"  closed between {data['closed_between'][0]} and {data['closed_between'][1]}"
           if data["closed_between"] else ""),
        f"document {data['document_hash'] or '-'}",
        f"url {data['url'] or '-'}",
    ]
    lines += [f"  {e['kind']:8} {e['at']}" for e in data["events"]]
    doc = data["document_hash"]
    emit(data, human="\n".join(lines), output=output,
         hint=f"q document {doc[:12]} for the canonical text" if doc else None)


@q_app.command("events")
def q_events(
    since: str | None = typer.Option(None, "--since", help="Nm, Nh or Nd"),
    kind: str | None = typer.Option(None, "--kind", help=f"Comma list: {', '.join(EVENT_KINDS)}"),
    board: str | None = typer.Option(None, "--board", help="source:board"),
    uid: str | None = typer.Option(None, "--uid", help="One posting"),
    fields: str | None = typer.Option(None, "--fields", help="Comma list of keys to keep"),
    limit: int = typer.Option(50, "--limit", help=f"1-{MAX_LIMIT}"),
    after: str | None = typer.Option(None, "--after", help="Opaque cursor from meta.next_cursor"),
    output: str | None = output_option(),
) -> None:
    """Lifecycle events oldest first — what `pulse` reports, without the cursor."""
    from jobhunter.cli import _split_board
    from jobhunter.store import queries

    kinds = tuple(k.strip() for k in kind.split(",") if k.strip()) if kind else None
    for k in kinds or ():
        if k not in EVENT_KINDS:
            fail("usage", f"unknown event kind: {k!r}", valid=list(EVENT_KINDS),
                 code=Exit.USAGE, output=output)
    src, brd = _split_board(board, output)
    after_id: int | None = None
    if after is not None:
        try:
            after_id = int(after)
        except ValueError:
            fail("usage", f"--after is not a cursor: {after!r}", code=Exit.USAGE, output=output,
                 hint="pass meta.next_cursor from the previous page verbatim")
    limit = _clamp(limit)
    window = _since(since)
    _, conn = _open(output)
    rows = _query(conn, output, lambda: queries.events_page(
        conn, since=window, kinds=kinds, source=src, board=brd, uid=uid, limit=limit,
        after_event_id=after_id))
    truncated = len(rows) > limit
    rows = rows[:limit]
    data = [
        {"event_id": e["event_id"], "kind": e["kind"], "uid": e["uid"], "at": iso(e["at"]),
         "board": f"{e['source']}:{e['board']}", "title": e["title"], "company": e["company"],
         "url": e["url"], "closed_between": closed_between(e)}
        for e in rows
    ]
    human = "\n".join(
        f"{e['at']}  {e['kind']:8} {(e['company'] or '-'):18} {e['title'] or '-'}"
        for e in data
    ) or "(no events)"
    emit(_select_fields(data, fields, output), human=human, output=output, count=len(data),
         truncated=truncated, next_cursor=str(rows[-1]["event_id"]) if truncated and rows else None,
         hint=f"q posting {rows[0]['uid']} for lifecycle detail" if rows else None)


@q_app.command("boards")
def q_boards(
    unhealthy: bool = typer.Option(False, "--unhealthy", help="Only boards whose health is not ok"),
    fields: str | None = typer.Option(None, "--fields", help="Comma list of keys to keep"),
    limit: int = typer.Option(50, "--limit", help=f"1-{MAX_LIMIT}"),
    output: str | None = output_option(),
) -> None:
    """Per-board fetch health and open counts, one row per board the store knows."""
    from jobhunter.store import queries

    limit = _clamp(limit)
    _, conn = _open(output)
    rows = _query(conn, output, lambda: queries.boards_overview(conn))
    if unhealthy:
        rows = [r for r in rows if r["health"] != "ok"]
    truncated = len(rows) > limit
    rows = rows[:limit]
    data = [
        {"board": r["board"], "health": r["health"], "open": r["open"], "error": r["error"],
         "started_at": iso(r["started_at"]) if r["started_at"] else None}
        for r in rows
    ]
    human = "\n".join(
        f"{r['board']:32} {(r['health'] or '-'):8} {r['open']:>5} open  {r['error'] or ''}".rstrip()
        for r in data
    ) or "(no boards)"
    emit(_select_fields(data, fields, output), human=human, output=output, count=len(data),
         truncated=truncated,
         hint="raise --limit to see the rest" if truncated else None)


def _parse_slice(value: str | None, output: str | None) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    start_s, sep, end_s = value.partition(":")
    start: int | None = None
    end: int | None = None
    try:
        if not sep:
            raise ValueError(value)
        start = int(start_s) if start_s else None
        end = int(end_s) if end_s else None
    except ValueError:
        fail("usage", f"--slice must be S:E codepoint offsets, got {value!r}",
             code=Exit.USAGE, output=output, hint="e.g. 0:500, 500: or :500")
    return start, end


@q_app.command("document")
def q_document(
    prefix: str = typer.Argument(..., help="document_hash or unambiguous hex prefix"),
    slice_: str | None = typer.Option(None, "--slice", help="S:E codepoint offsets"),
    output: str | None = output_option(),
) -> None:
    """The canonical markdown of one document — the text every quote span indexes."""
    from jobhunter.cli import _resolve_doc
    from jobhunter.markdown import NORMALIZER_VERSION
    from jobhunter.store import extraction as xstore

    start, end = _parse_slice(slice_, output)  # a bad slice never reaches the database
    _, conn = _open(output)

    def load() -> tuple[str, str | None]:
        doc = _resolve_doc(conn, prefix, output)
        return doc, xstore.markdown_for(conn, doc, NORMALIZER_VERSION)

    doc, markdown = _query(conn, output, load)
    if markdown is None:
        fail("not_found", f"no document {doc[:12]} under {NORMALIZER_VERSION}",
             code=Exit.NOT_FOUND, output=output, hint="run: job-hunter rebuild")
    text = markdown[start:end] if slice_ else markdown
    emit({"document_hash": doc, "markdown": text}, human=text, output=output,
         hint=f"q profile --doc {doc[:12]} for what it demands")


def _profile_row(conn: Conn, doc: str) -> dict[str, Any] | None:
    """The row `q profile` reports: validated first, else the newest state, so a
    quarantined document can explain itself instead of looking absent."""
    return conn.execute(
        "SELECT e.status, e.model, e.prompt_version, e.profile, e.updated_at,"
        " v.title, v.company, v.url FROM extractions e"
        " LEFT JOIN documents d ON d.document_hash = e.document_hash"
        " LEFT JOIN posting_versions v ON v.version_hash = d.version_hash"
        " WHERE e.document_hash = %s"
        " ORDER BY (e.status = 'validated') DESC, e.updated_at DESC LIMIT 1",
        (doc,),
    ).fetchone()


@q_app.command("profile")
def q_profile(
    doc: str = typer.Option(..., "--doc", help="document_hash or unambiguous hex prefix"),
    full: bool = typer.Option(False, "--full", help="Emit the stored profile verbatim"),
    output: str | None = output_option(),
) -> None:
    """The demand profile of one document: areas, mentions, facts. `--full` adds evidence."""
    from jobhunter.cli import _resolve_doc

    _, conn = _open(output)

    def load() -> tuple[str, dict[str, Any] | None]:
        resolved = _resolve_doc(conn, doc, output)
        return resolved, _profile_row(conn, resolved)

    resolved, row = _query(conn, output, load)
    if row is None:
        fail("not_found", f"no extraction for {resolved[:12]}", code=Exit.NOT_FOUND,
             output=output, hint=f"run: job-hunter extract run --doc {resolved}")
    if row["status"] != "validated" or row["profile"] is None:
        fail("not_found", f"no validated profile for {resolved[:12]} (status: {row['status']})",
             code=Exit.NOT_FOUND, output=output,
             hint=f"job-hunter extract review show {resolved[:12]}")
    profile = row["profile"]
    summary = profile_summary(profile)
    data = {
        "document_hash": resolved, "status": row["status"], "model": row["model"],
        "prompt_version": row["prompt_version"], "updated_at": iso(row["updated_at"]),
        "title": row["title"], "company": row["company"], "url": row["url"],
        "profile": profile if full else summary,
    }
    facts = summary["facts"]
    comp = ", ".join(
        f"{c['min']}-{c['max']} {c['currency'] or '?'}" for c in facts["compensation"]
    ) or "not stated"
    lines = [
        f"{row['title'] or '?'} — {row['company'] or '?'}",
        f"{resolved[:12]}  {row['status']}  {row['model']}  {row['prompt_version']}",
        f"compensation  {comp}",
        f"experience    {facts['experience_months'] or 'not stated'}",
        f"deadline      {facts['deadline'] or 'not stated'}",
        f"mentions      {', '.join(summary['mentions']) or '-'}",
    ]
    lines += [
        f"  [{a['kind']}] {a['name']} — {a['importance']}{'/' + a['level'] if a['level'] else ''}"
        for a in summary["areas"]
    ]
    emit(data, human="\n".join(lines), output=output,
         hint=f"q document {resolved[:12]} for the text" if full
         else f"q profile --doc {resolved[:12]} --full for claims, quotes and spans")


@q_app.command("claims")
def q_claims(
    mention: str = typer.Option(..., "--mention", help="One mention, matched case-insensitively"),
    importance: str | None = typer.Option(
        None, "--importance", help="|".join(IMPORTANCES)),
    board: str | None = typer.Option(None, "--board", help="source:board"),
    fields: str | None = typer.Option(None, "--fields", help="Comma list of keys to keep"),
    limit: int = typer.Option(50, "--limit", help=f"1-{MAX_LIMIT}"),
    output: str | None = output_option(),
) -> None:
    """Who demands one mention, across the corpus — the postings live on it today."""
    from jobhunter.cli import _split_board
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.runner import SCHEMA_VERSION
    from jobhunter.l2.state import globs_to_regex
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.store import queries

    if importance is not None and importance not in IMPORTANCES:
        fail("usage", f"--importance must be one of: {', '.join(IMPORTANCES)}",
             valid=list(IMPORTANCES), code=Exit.USAGE, output=output)
    src, brd = _split_board(board, output)
    limit = _clamp(limit)
    settings, conn = _open(output)
    # The engine tuple in force, exactly as `pulse` scopes its profiles: retired
    # prompt/validator versions still sit in `profile_mentions` after a rebuild.
    rows = _query(conn, output, lambda: queries.claims_by_mention(
        conn, mention=mention, importance=importance, source=src, board=brd, limit=limit,
        model_regex=globs_to_regex(settings.l2_models), prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION, validator_version=VALIDATOR_VERSION))
    truncated = len(rows) > limit
    rows = rows[:limit]
    data = [
        {"document_hash": r["document_hash"], "mention": r["mention"],
         "area_kind": r["area_kind"], "importance": r["importance"], "uid": r["uid"],
         "board": f"{r['source']}:{r['board']}", "title": r["title"], "company": r["company"],
         "url": r["url"]}
        for r in rows
    ]
    human = "\n".join(
        f"{r['importance']:10} {r['area_kind']:10} {(r['company'] or '-'):18} "
        f"{r['title'] or '-'}  {r['uid']}"
        for r in data
    ) or f"(nothing demands {mention!r})"
    emit(_select_fields(data, fields, output), human=human, output=output, count=len(data),
         truncated=truncated,
         hint=f"q profile --doc {data[0]['document_hash'][:12]} for the whole demand"
         if data else None)
