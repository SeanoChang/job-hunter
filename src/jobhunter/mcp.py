"""The hosted read surface: the `q` verbs as MCP tools (spec 2026-09-02 §1).

An agent that cannot reach Postgres — a cloud routine in an ephemeral VM with
proxy-restricted egress — reads the corpus over HTTPS instead. The payloads are
not reimplemented for it: every tool below opens a connection, calls the same
`views.py` function the CLI calls, and returns `{data, truncated, next_cursor}`.
What stays behind is everything that only makes sense at a terminal — the
envelope, the exit codes, `--fields`, the human table.

The transport is streamable HTTP, stateless and JSON: a Cloud Run instance can
be reaped between two calls, so no session survives a request and no connection
is pooled. Auth is one static bearer checked in ASGI middleware ahead of the
MCP app; `/healthz` is the only route that answers without it. Nothing here
writes — `mcp_cursors` is the sole exception, and only the `pulse` tool touches
it.

Flag validation that the CLI renders as a teaching error becomes a `ToolError`
carrying the same sentence: the model reads it and fixes the call itself.
"""

from __future__ import annotations

import hmac
import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.types import ASGIApp, Receive, Scope, Send

from jobhunter import __version__, views
from jobhunter.cli_output import Exit
from jobhunter.cli_q import EVENT_KINDS, IMPORTANCES, _clamp
from jobhunter.config import ConfigError, Settings, env_snapshot
from jobhunter.store import db, mcp_state
from jobhunter.timeutil import parse_iso, utcnow

MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
_HEALTH_BODY = json.dumps({"ok": True, "version": __version__}).encode("utf-8")

# Indirections so tests can point the serving path at a throwaway schema and a
# fixed clock, the way `cli._schema` and `cli._now` do for the command line.
_schema: str = db.SCHEMA


def _now() -> datetime:
    return utcnow()

_WINDOW = re.compile(r"^(\d+)([mhd])$")

server: MCPServer[Any] = MCPServer(
    "job-hunter",
    version=__version__,
    instructions="Read Sean's job-posting corpus: postings and their lifecycle, "
    "the events since a watermark, canonical posting text, and demand profiles. "
    "Read-only; every list verb is bounded and marks truncation.",
)


# -- shared plumbing -------------------------------------------------------


@contextmanager
def _read() -> Iterator[tuple[Settings, db.Conn]]:
    """One connection per call, closed when the call ends.

    Serverless-friendly on purpose (spec §5): no pool outlives a request. A
    backend failure becomes an anticipated tool error rather than a crash, so
    the model is told what broke instead of `Error executing tool`.
    """
    try:
        settings = Settings.load()
        conn = db.connect(settings.require_database_url(), schema=_schema)
    except ConfigError as e:
        raise ToolError(f"config error: {e}") from e
    except Exception as e:  # psycopg.OperationalError and friends
        raise ToolError(f"database error: {e}") from e
    try:
        yield settings, conn
    except ToolError:
        raise  # already chose its sentence
    except Exception as e:
        raise ToolError(f"database error: {e}") from e
    finally:
        conn.close()


def _page(page: views.Page) -> dict[str, Any]:
    return {"data": page.data, "truncated": page.truncated, "next_cursor": page.next_cursor}


def _split_board(value: str | None) -> tuple[str | None, str | None]:
    """`source:board` — the one form every payload prints and this takes back."""
    if not value:
        return None, None
    if ":" not in value:
        raise ToolError(f"board must look like source:board, got {value!r} (e.g. ashby:ramp)")
    src, brd = value.split(":", 1)
    return src, brd


def _since(value: str | None) -> datetime | None:
    """A relative window (`30m`, `24h`, `7d`) like the CLI's `--since`, or an
    absolute ISO instant for a routine reporting a fixed span."""
    if value is None:
        return None
    m = _WINDOW.match(value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return _now() - {"m": timedelta(minutes=n), "h": timedelta(hours=n),
                         "d": timedelta(days=n)}[unit]
    try:
        return parse_iso(value)
    except ValueError:
        raise ToolError(
            f"since must be a window like 30m, 24h or 7d, or an ISO instant: {value!r}"
        ) from None


def _cursor(after: str | None) -> str | None:
    """A hand-made cursor is a usage error, not a database one: `postings_page`
    would otherwise raise a bare ValueError deep inside the query."""
    if after is None:
        return None
    ts, sep, uid = after.partition("|")
    try:
        if not (sep and uid):
            raise ValueError(after)
        parse_iso(ts)
    except ValueError:
        raise ToolError(
            f"after is not a cursor: {after!r} — pass next_cursor from the previous page verbatim"
        ) from None
    return after


def _event_cursor(after: str | None) -> int | None:
    if after is None:
        return None
    try:
        return int(after)
    except ValueError:
        raise ToolError(
            f"after is not a cursor: {after!r} — pass next_cursor from the previous page verbatim"
        ) from None


# -- the seven read tools --------------------------------------------------


@server.tool()
def postings(
    board: str | None = None,
    status: str | None = None,
    since: str | None = None,
    search: str | None = None,
    limit: int = 50,
    after: str | None = None,
) -> dict[str, Any]:
    """List postings, newest first: uid, board, status, title, company, lifecycle dates.

    board is source:board (ashby:ramp), status is open or closed, since is a
    window (7d) or an ISO instant over first_seen_at, search is a case-insensitive
    match on title and company. Bounded at 500; when truncated is true, pass
    next_cursor back as after for the next page.
    """
    if status not in (None, "open", "closed"):
        raise ToolError(f"status must be open or closed: {status!r}")
    src, brd = _split_board(board)
    window = _since(since)
    with _read() as (_, conn):
        return _page(views.postings_view(
            conn, source=src, board=brd, status=status, since=window, search=search,
            limit=_clamp(limit), after=_cursor(after)))


@server.tool()
def posting(uid: str) -> dict[str, Any]:
    """One posting in full: lifecycle, close interval, version history, events, document_hash."""
    with _read() as (_, conn):
        page = views.posting_view(conn, uid)
        if page is None:
            raise ToolError(f"no posting {uid!r} — find uids with the postings tool (search=…)")
        return _page(page)


@server.tool()
def events(
    since: str | None = None,
    kind: str | None = None,
    board: str | None = None,
    uid: str | None = None,
    limit: int = 50,
    after: str | None = None,
) -> dict[str, Any]:
    """Lifecycle events oldest first — what pulse reports, without the watermark.

    kind is a comma list of opened, changed, closed, reopened; since is a window
    (24h) or an ISO instant; board is source:board. Bounded at 500; next_cursor
    pages on through after.
    """
    kinds = tuple(k.strip() for k in kind.split(",") if k.strip()) if kind else None
    for k in kinds or ():
        if k not in EVENT_KINDS:
            raise ToolError(f"unknown event kind: {k!r} — one of {', '.join(EVENT_KINDS)}")
    src, brd = _split_board(board)
    window = _since(since)
    with _read() as (_, conn):
        return _page(views.events_view(
            conn, since=window, kinds=kinds, source=src, board=brd, uid=uid,
            limit=_clamp(limit), after_event_id=_event_cursor(after)))


@server.tool()
def boards(unhealthy_only: bool = False, limit: int = 50) -> dict[str, Any]:
    """Per-board fetch health and open counts, one row per board the store knows."""
    with _read() as (_, conn):
        return _page(views.boards_view(conn, unhealthy_only=unhealthy_only, limit=_clamp(limit)))


@server.tool()
def document(document_hash: str, slice: str | None = None) -> dict[str, Any]:
    """The canonical markdown of one document — the text every quote span indexes.

    document_hash comes from a posting, profile or claims payload. slice is S:E
    codepoint offsets (0:500, 500:, :500) for reading a long posting in parts.
    """
    if slice is not None:
        try:
            views.parse_slice(slice)
        except ValueError:
            raise ToolError(
                f"slice must be S:E codepoint offsets, got {slice!r} (e.g. 0:500, 500: or :500)"
            ) from None
    with _read() as (_, conn):
        from jobhunter.markdown import NORMALIZER_VERSION

        page = views.document_view(conn, document_hash, slice_=slice)
        if page is None:
            raise ToolError(f"no document {document_hash[:12]} under {NORMALIZER_VERSION}")
        return _page(page)


@server.tool()
def profile(document_hash: str, full: bool = False) -> dict[str, Any]:
    """The demand profile of one document: areas, mentions, facts. full adds evidence.

    Summary by default — areas, mention names, compensation, experience, deadline.
    full returns the stored profile verbatim, quotes and character spans included.
    """
    with _read() as (_, conn):
        # the row, not `profile_view`: the two ways a profile can be absent are
        # two different messages, and only the row says which one this is
        row = views.profile_row(conn, document_hash)
        if row is None:
            raise ToolError(
                f"no extraction for {document_hash[:12]} — the owner runs: extract run --doc"
            )
        if row["status"] != "validated" or row["profile"] is None:
            raise ToolError(
                f"no validated profile for {document_hash[:12]} (status: {row['status']}) — "
                "the owner runs: extract run --doc"
            )
        return _page(views.Page(views.profile_payload(document_hash, row, full=full)))


@server.tool()
def claims(
    mention: str,
    importance: str | None = None,
    board: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Who demands one mention across the corpus — the postings living on it today.

    mention is matched case-insensitively (python, kubernetes); importance is
    required, preferred or contextual; board is source:board.
    """
    if importance is not None and importance not in IMPORTANCES:
        raise ToolError(f"importance must be one of: {', '.join(IMPORTANCES)}")
    src, brd = _split_board(board)
    with _read() as (settings, conn):
        return _page(views.claims_view(
            conn, settings, mention=mention, importance=importance, source=src, board=brd,
            limit=_clamp(limit)))


# -- pulse: the delta, and the only row this server writes -----------------


@server.tool()
def pulse(
    cursor: str = "default",
    peek: bool = False,
    since: str | None = None,
    limit: int = 200,
    boards: str | None = None,
) -> dict[str, Any]:
    """Everything new since this cursor's last pulse: events, demand, attention.

    The watermark is named and kept server-side, so a routine that persists no
    files still resumes where it stopped: call pulse(cursor="hourly") each hour
    and every call reports only what the one before it did not. peek reports
    without advancing. since — a window (24h) or an ISO instant — reports a
    fixed span and leaves the cursor untouched. boards is a comma list of
    source:board. When truncated is true the cursor stands at the end of this
    page, so calling again continues rather than repeats.
    """
    only = tuple(b.strip() for b in boards.split(",") if b.strip()) if boards else None
    for b in only or ():
        _split_board(b)  # a typo in one entry must not silently match nothing
    start = _since(since)
    with _read() as (settings, conn):
        wm = None if since is not None else mcp_state.read_cursor(conn, cursor)
        page, new_wm = views.pulse_view(
            conn, settings, wm=wm, since_iso=start.isoformat() if start is not None else None,
            limit=_clamp(limit), boards=only, now=_now())
        body = page.record()
        payload = {"data": body, "truncated": page.truncated,
                   "cursor": None if since is not None else cursor,
                   "first_run": body["first_run"]}
        if not peek and since is None and new_wm is not None:
            # After the payload is assembled, never before: a failure between
            # the two re-reports one window, which is the harmless direction.
            mcp_state.write_cursor(conn, cursor, new_wm)
            conn.commit()
        return payload


# -- serving ---------------------------------------------------------------


async def _respond(send: Send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]})
    await send({"type": "http.response.body", "body": body})


class BearerAuth:
    """The static-bearer gate ahead of the MCP app, and the one open route (spec §2).

    Owner-internal serving, so one token and no OAuth: every request but
    `/healthz` must carry it, compared in constant time so a wrong token leaks
    nothing through timing. `/healthz` answers here — liveness and build version
    are the platform's business, not the protocol's, and the probe must not
    depend on an MCP session. Non-HTTP scopes (the lifespan the session manager
    runs on) pass straight through.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if scope.get("path") == HEALTH_PATH:
            await _respond(send, 200, _HEALTH_BODY)
            return
        offered = next(
            (v.decode("latin-1") for k, v in scope["headers"] if k == b"authorization"), ""
        )
        if not hmac.compare_digest(offered, self._expected):
            await _respond(send, 401, b'{"error": "unauthorized"}')
            return
        await self._app(scope, receive, send)


def build_app(token: str) -> BearerAuth:
    """The served ASGI app: guarded MCP at `/mcp`, open `/healthz`.

    `host` is the bind address the SDK reads to decide whether to enable its
    DNS-rebinding defence; naming 0.0.0.0 keeps it off, because on Cloud Run the
    Host header is the service's own domain and the bearer is the real gate.
    """
    return BearerAuth(
        server.streamable_http_app(
            streamable_http_path=MCP_PATH, json_response=True, stateless_http=True,
            host="0.0.0.0",  # the bind address, not a trusted-host claim
        ),
        token,
    )


def main() -> None:
    """`job-hunter-mcp`: serve on $PORT, Cloud Run's contract (default 8080)."""
    import uvicorn

    env = env_snapshot()  # config.py stays the only reader of os.environ
    try:
        token = Settings.load().require_mcp_token()
        port = int(env.get("PORT", "8080"))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(int(Exit.CONFIG)) from None
    except ValueError:
        print(f"config error: PORT must be a number, got {env.get('PORT')!r}", file=sys.stderr)
        raise SystemExit(int(Exit.CONFIG)) from None
    uvicorn.run(build_app(token), host="0.0.0.0", port=port)
