"""The hosted MCP server's pulse watermarks — the only writer of `mcp_cursors`.

Personal state stays on the client (2026-08-18 ruling) and `cursors.py` keeps
it there for the CLI, but the hosted server has no client: a Cloud Run instance
is ephemeral, so its watermark lives in the store instead (spec 2026-09-02 §3,
a deliberate bend of that ruling — the server is owner infrastructure and the
state is one timestamp). Semantics are unchanged: the timestamp is
authoritative, `event_ids_at` tie-breaks the events sharing that instant, and
the cursor advances only once the response payload is built — hence the
caller-commits contract every store helper here follows.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jobhunter.cursors import Watermark
from jobhunter.store.db import Conn
from jobhunter.timeutil import parse_iso


def read_cursor(conn: Conn, name: str) -> Watermark | None:
    """The stored watermark, or None when this name has never been advanced."""
    row = conn.execute(
        "SELECT at, event_ids_at FROM mcp_cursors WHERE name = %s", (name,)
    ).fetchone()
    if row is None:
        return None
    at: datetime = row["at"]
    ids: list[int] = row["event_ids_at"]
    # `at` comes back in the session's timezone; normalise so the ISO string is
    # byte-identical to the one that went in. Full precision on purpose — a
    # second-truncated watermark would re-report its own instant forever.
    return Watermark(at=at.astimezone(UTC).isoformat(), event_ids_at=tuple(ids))


def write_cursor(conn: Conn, name: str, wm: Watermark) -> None:
    """Advance one cursor, leaving the other names untouched. Joins the caller's
    transaction; the caller commits, after the payload is out."""
    conn.execute(
        "INSERT INTO mcp_cursors (name, at, event_ids_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET at = EXCLUDED.at, "
        "event_ids_at = EXCLUDED.event_ids_at",
        (name, parse_iso(wm.at), list(wm.event_ids_at)),
    )
