"""The `pulse` payload: one delta an agent can act on in a single call.

`pulse` answers "what changed in the corpus since my last run?" — lifecycle
events in the window, the demand each opened or changed posting states, and
what needs attention — bounded by `--limit` and by the client-side watermark in
`cursors.py`.

The shaping helpers live here rather than in a command module because `q` and
`pulse` report the same rows: a close interval or a profile digest must not
read one way through one verb and another way through the other. Quotes and
spans stay behind `q profile --full`; a delta covering dozens of postings must
not carry the corpus text along with it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from jobhunter.cursors import Watermark
from jobhunter.timeutil import iso, parse_iso

if TYPE_CHECKING:
    from jobhunter.config import Settings
    from jobhunter.store.db import Conn

MAX_MENTIONS = 8
FIRST_RUN_WINDOW = timedelta(hours=24)
# Kinds whose event points at a live text worth profiling; a close describes a
# disappearance, so there is no current demand to inline.
PROFILED_KINDS = ("opened", "changed")


def closed_between(row: dict[str, Any]) -> list[str | None] | None:
    """The honest interval: a close is known to have happened between the last
    sighting and the first miss, never at a point."""
    if not row.get("closed_lower_at"):
        return None
    upper = row.get("closed_upper_at")
    return [iso(row["closed_lower_at"]), iso(upper) if upper else None]


def profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    """Areas, the top mentions across them, and the three headline facts.

    Reads defensively: `profile` is model output that passed the validator of
    its day, so a field the current schema guarantees may still be absent in a
    row written under an older one."""
    areas = (profile.get("demand_profile") or {}).get("areas") or []
    mentions: dict[str, None] = {}  # insertion-ordered set: first mention wins
    for area in areas:
        for mention in area.get("mentions") or []:
            mentions.setdefault(mention, None)
    facts = profile.get("facts") or {}
    experience = facts.get("experience_months")
    deadline = facts.get("deadline")
    return {
        "areas": [
            {"name": a.get("name"), "kind": a.get("kind"),
             "importance": a.get("importance"), "level": a.get("level")}
            for a in areas
        ],
        "mentions": list(mentions)[:MAX_MENTIONS],
        "facts": {
            "compensation": [
                {k: c.get(k) for k in ("min", "max", "currency", "period")}
                for c in facts.get("compensation") or []
            ],
            "experience_months": (
                {"min": experience.get("min"), "max": experience.get("max")}
                if experience else None
            ),
            "deadline": deadline.get("date") if deadline else None,
        },
    }


def _advance(previous: Watermark | None, page: list[dict[str, Any]]) -> Watermark | None:
    """The watermark after reading `page`: its last instant, plus every id seen
    at that instant. Ids from the previous watermark survive when the instant
    has not moved — they were already reported, and reporting them again would
    misstate what is new. None when the page is empty: nothing to advance past.

    `at` keeps full precision (`isoformat`, not `iso`) — a truncated watermark
    would stop matching its own instant and re-report it on every call."""
    if not page:
        return None
    last_at: datetime = page[-1]["at"]
    ids = {int(r["event_id"]) for r in page if r["at"] == last_at}
    if previous is not None and parse_iso(previous.at) == last_at:
        ids |= set(previous.event_ids_at)
    return Watermark(at=last_at.isoformat(), event_ids_at=tuple(sorted(ids)))


def build_pulse(
    conn: Conn,
    settings: Settings,
    *,
    wm: Watermark | None,
    limit: int,
    boards: tuple[str, ...] | None,
    now: datetime,
) -> tuple[dict[str, Any], Watermark | None]:
    """The payload plus the watermark the caller should store once it is flushed.

    No watermark means a first run: the last 24 hours, flagged as such. The
    returned watermark covers every event *read*, not every event reported —
    a `--boards` filter drops rows the reader deliberately did not want, and a
    cursor that refused to pass them would wedge on a busy unwatched board.
    """
    from jobhunter.cli import _extraction_block
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.runner import SCHEMA_VERSION
    from jobhunter.l2.state import globs_to_regex
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.markdown import NORMALIZER_VERSION
    from jobhunter.store import queries

    if wm is None:
        window_from = now - FIRST_RUN_WINDOW
        # ordered by event_id, which is the ingest order and therefore the
        # chronological one; `events_after_watermark` orders by (at, event_id)
        rows = queries.events_page(conn, since=window_from, limit=limit)
    else:
        window_from = parse_iso(wm.at)
        rows = queries.events_after_watermark(
            conn, at=window_from, exclude_ids=wm.event_ids_at, limit=limit
        )
    truncated = len(rows) > limit
    rows = rows[:limit]
    new_wm = _advance(wm, rows)
    wanted = set(boards) if boards is not None else None
    if wanted is not None:
        rows = [r for r in rows if f"{r['source']}:{r['board']}" in wanted]

    profiled = list(dict.fromkeys(r["uid"] for r in rows if r["kind"] in PROFILED_KINDS))
    docs = queries.docs_for_events(conn, profiled, NORMALIZER_VERSION)
    profiles = queries.validated_profiles(
        conn, sorted(set(docs.values())),
        model_regex=globs_to_regex(settings.l2_models), prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION, validator_version=VALIDATOR_VERSION,
    )
    events: list[dict[str, Any]] = []
    for r in rows:
        event: dict[str, Any] = {
            "event_id": r["event_id"], "kind": r["kind"], "uid": r["uid"], "at": iso(r["at"]),
            "board": f"{r['source']}:{r['board']}", "title": r["title"],
            "company": r["company"], "url": r["url"], "closed_between": closed_between(r),
        }
        if r["kind"] in PROFILED_KINDS:
            doc = docs.get(r["uid"])
            profile = profiles.get(doc) if doc else None
            event["document_hash"] = doc
            event["profile"] = profile_summary(profile) if profile else None
        events.append(event)

    overview = queries.boards_overview(conn)
    if wanted is not None:
        overview = [b for b in overview if b["board"] in wanted]
    payload = {
        "window": {"from": iso(window_from), "to": iso(now)},
        "first_run": wm is None,
        "events": events,
        "attention": {
            "unhealthy_boards": [
                {"board": b["board"], "health": b["health"], "open": b["open"],
                 "error": b["error"],
                 "started_at": iso(b["started_at"]) if b["started_at"] else None}
                for b in overview if b["health"] != "ok"
            ],
            "extraction": _extraction_block(settings),
        },
        "_truncated": truncated,
    }
    return payload, new_wm
