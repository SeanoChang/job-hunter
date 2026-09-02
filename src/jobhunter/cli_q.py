"""Read-only `q` namespace: the agent-facing query surface (spec §4).

Every list verb is bounded (`--limit`, hard cap 500), always reports
`meta.truncated`, and hands back an opaque `--after` cursor built from the last
row it actually emitted — never from a row the reader never saw. Nothing here
writes, so these verbs run on a read-only Postgres role.

A command is flags, then a view, then output: the payloads come from
`views.py`, which the MCP wrapper reads too, and what stays here is the part
that only makes sense at a command line — validating a flag into a teaching
error, selecting fields, rendering the human table.

`jobhunter.cli` is imported inside the command bodies on purpose: it imports
this module to mount the sub-app, and its `_schema` is a test seam that must be
read at call time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import typer

from jobhunter import views
from jobhunter.cli_output import Exit, emit, fail, output_option
from jobhunter.pulse import profile_summary
from jobhunter.timeutil import parse_iso

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


def _since(value: str | None, output: str | None) -> Any:
    """Relative window (`Nm`/`Nh`/`Nd`) resolved against the CLI's clock."""
    from jobhunter.cli import _now, _parse_since

    return _now() - _parse_since(value, output) if value else None


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

    if status not in (None, "open", "closed"):
        fail("usage", f"--status must be open or closed: {status!r}",
             valid=["open", "closed"], code=Exit.USAGE, output=output)
    src, brd = _split_board(board, output)
    _check_after(after, output)
    limit = _clamp(limit)
    window = _since(since, output)
    _, conn = _open(output)
    page = _query(conn, output, lambda: views.postings_view(
        conn, source=src, board=brd, status=status, since=window, search=search,
        limit=limit, after=after))
    data = page.rows()
    human = "\n".join(
        f"{r['status']:6} {r['uid']:32} {(r['company'] or '-'):18} {r['title'] or '-'}"
        for r in data
    ) or "(no postings)"
    emit(_select_fields(data, fields, output), human=human, output=output, count=len(data),
         truncated=page.truncated, next_cursor=page.next_cursor,
         hint=f"q posting {data[0]['uid']} for lifecycle detail" if data else None)


@q_app.command("posting")
def q_posting(
    uid: str = typer.Argument(..., help="Posting uid, e.g. ab:ramp:x"),
    output: str | None = output_option(),
) -> None:
    """One posting: lifecycle, close interval, version history, events, document."""
    _, conn = _open(output)
    page = _query(conn, output, lambda: views.posting_view(conn, uid))
    if page is None:
        fail("not_found", f"no posting {uid!r}", code=Exit.NOT_FOUND, output=output,
             hint="find uids with: q postings --search <text>")
    data = page.record()
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
    window = _since(since, output)
    _, conn = _open(output)
    page = _query(conn, output, lambda: views.events_view(
        conn, since=window, kinds=kinds, source=src, board=brd, uid=uid, limit=limit,
        after_event_id=after_id))
    data = page.rows()
    human = "\n".join(
        f"{e['at']}  {e['kind']:8} {(e['company'] or '-'):18} {e['title'] or '-'}"
        for e in data
    ) or "(no events)"
    emit(_select_fields(data, fields, output), human=human, output=output, count=len(data),
         truncated=page.truncated, next_cursor=page.next_cursor,
         hint=f"q posting {data[0]['uid']} for lifecycle detail" if data else None)


@q_app.command("boards")
def q_boards(
    unhealthy: bool = typer.Option(False, "--unhealthy", help="Only boards whose health is not ok"),
    fields: str | None = typer.Option(None, "--fields", help="Comma list of keys to keep"),
    limit: int = typer.Option(50, "--limit", help=f"1-{MAX_LIMIT}"),
    output: str | None = output_option(),
) -> None:
    """Per-board fetch health and open counts, one row per board the store knows."""
    limit = _clamp(limit)
    _, conn = _open(output)
    page = _query(conn, output, lambda: views.boards_view(
        conn, unhealthy_only=unhealthy, limit=limit))
    data = page.rows()
    human = "\n".join(
        f"{r['board']:32} {(r['health'] or '-'):8} {r['open']:>5} open  {r['error'] or ''}".rstrip()
        for r in data
    ) or "(no boards)"
    emit(_select_fields(data, fields, output), human=human, output=output, count=len(data),
         truncated=page.truncated,
         hint="raise --limit to see the rest" if page.truncated else None)


def _check_slice(value: str | None, output: str | None) -> None:
    """Like `_check_after`: a malformed slice is a usage error, not a database
    one — `document_view` would otherwise raise a bare ValueError mid-read."""
    if value is None:
        return
    try:
        views.parse_slice(value)
    except ValueError:
        fail("usage", f"--slice must be S:E codepoint offsets, got {value!r}",
             code=Exit.USAGE, output=output, hint="e.g. 0:500, 500: or :500")


@q_app.command("document")
def q_document(
    prefix: str = typer.Argument(..., help="document_hash or unambiguous hex prefix"),
    slice_: str | None = typer.Option(None, "--slice", help="S:E codepoint offsets"),
    output: str | None = output_option(),
) -> None:
    """The canonical markdown of one document — the text every quote span indexes."""
    from jobhunter.cli import _resolve_doc
    from jobhunter.markdown import NORMALIZER_VERSION

    _check_slice(slice_, output)  # a bad slice never reaches the database
    _, conn = _open(output)

    def load() -> tuple[str, views.Page | None]:
        doc = _resolve_doc(conn, prefix, output)
        return doc, views.document_view(conn, doc, slice_=slice_)

    doc, page = _query(conn, output, load)
    if page is None:
        fail("not_found", f"no document {doc[:12]} under {NORMALIZER_VERSION}",
             code=Exit.NOT_FOUND, output=output, hint="run: job-hunter rebuild")
    data = page.record()
    emit(data, human=data["markdown"], output=output,
         hint=f"q profile --doc {doc[:12]} for what it demands")


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
        # the row, not `profile_view`: the two ways a profile can be absent are
        # two different messages, and only the row says which one this is
        return resolved, views.profile_row(conn, resolved)

    resolved, row = _query(conn, output, load)
    if row is None:
        fail("not_found", f"no extraction for {resolved[:12]}", code=Exit.NOT_FOUND,
             output=output, hint=f"run: job-hunter extract run --doc {resolved}")
    if row["status"] != "validated" or row["profile"] is None:
        fail("not_found", f"no validated profile for {resolved[:12]} (status: {row['status']})",
             code=Exit.NOT_FOUND, output=output,
             hint=f"job-hunter extract review show {resolved[:12]}")
    data = views.profile_payload(resolved, row, full=full)
    summary = profile_summary(row["profile"])  # the human lines read the digest either way
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

    if importance is not None and importance not in IMPORTANCES:
        fail("usage", f"--importance must be one of: {', '.join(IMPORTANCES)}",
             valid=list(IMPORTANCES), code=Exit.USAGE, output=output)
    src, brd = _split_board(board, output)
    limit = _clamp(limit)
    settings, conn = _open(output)
    page = _query(conn, output, lambda: views.claims_view(
        conn, settings, mention=mention, importance=importance, source=src, board=brd,
        limit=limit))
    data = page.rows()
    human = "\n".join(
        f"{r['importance']:10} {r['area_kind']:10} {(r['company'] or '-'):18} "
        f"{r['title'] or '-'}  {r['uid']}"
        for r in data
    ) or f"(nothing demands {mention!r})"
    emit(_select_fields(data, fields, output), human=human, output=output, count=len(data),
         truncated=page.truncated,
         hint=f"q profile --doc {data[0]['document_hash'][:12]} for the whole demand"
         if data else None)
