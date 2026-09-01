"""Named client-side cursors: `pulse`'s memory of what it already reported.

Personal state stays on the client (2026-08-18 ruling) — a read verb never
writes the shared store. Every named cursor lives in one JSON file under
`Settings.state_dir`:

    {"hourly": {"at": "2026-08-20T06:00:00+00:00", "event_ids_at": [41, 42]}}

The watermark is the timestamp of the newest reported event plus the event ids
seen at exactly that instant. The timestamp is the authoritative half:
`rebuild` regenerates `event_id`s but reproduces `at`, so the worst case after
a rebuild is re-reporting one instant, never skipping past one. `at` keeps full
precision on purpose — a second-truncated watermark would never match its own
instant again and would re-report the same events forever.

Reading is total: unreadable or malformed state reads as "no cursor", which
costs one 24-hour re-report and can never skip an event.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FILENAME = "cursors.json"


@dataclass(frozen=True)
class Watermark:
    at: str  # ISO-8601 UTC of the newest reported event
    event_ids_at: tuple[int, ...] = ()  # ids at exactly `at`, the tie-break


def _path(state_dir: Path) -> Path:
    return state_dir / FILENAME


def _read_all(state_dir: Path) -> dict[str, Any]:
    try:
        raw = _path(state_dir).read_text(encoding="utf-8")
    except OSError:  # absent, unreadable, or a directory: no cursors yet
        return {}
    try:
        cursors = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return cursors if isinstance(cursors, dict) else {}


def read_cursor(state_dir: Path, name: str) -> Watermark | None:
    """The stored watermark, or None when this name has never been advanced."""
    entry = _read_all(state_dir).get(name)
    if not isinstance(entry, dict) or not isinstance(entry.get("at"), str):
        return None
    raw_ids = entry.get("event_ids_at")
    ids = raw_ids if isinstance(raw_ids, list) else []
    return Watermark(at=entry["at"], event_ids_at=tuple(i for i in ids if isinstance(i, int)))


def write_cursor(state_dir: Path, name: str, wm: Watermark) -> None:
    """Advance one cursor, atomically, leaving the other names untouched.

    Written to a temp file in the same directory and renamed over the target:
    a crash mid-write leaves the previous cursor intact, so the next run
    re-reports rather than skips."""
    state_dir.mkdir(parents=True, exist_ok=True)
    cursors = _read_all(state_dir)
    cursors[name] = {"at": wm.at, "event_ids_at": list(wm.event_ids_at)}
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".cursors-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cursors, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _path(state_dir))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
