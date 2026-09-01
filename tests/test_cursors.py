"""Named client-side cursors: `pulse`'s memory of what it already reported.

The rules the file format has to keep: one parseable JSON object, names that
do not disturb each other, and a state that can only ever cause a re-report —
never a skip.
"""

import json
from pathlib import Path

from jobhunter.cursors import Watermark, read_cursor, write_cursor


def test_round_trip_creates_the_state_dir(tmp_path: Path) -> None:
    state = tmp_path / "state" / "job-hunter"
    wm = Watermark(at="2026-09-01T06:00:00+00:00", event_ids_at=(41, 42))
    write_cursor(state, "hourly", wm)
    assert read_cursor(state, "hourly") == wm


def test_missing_file_and_unknown_name_read_as_no_cursor(tmp_path: Path) -> None:
    assert read_cursor(tmp_path / "nowhere", "hourly") is None
    write_cursor(tmp_path, "hourly", Watermark("2026-09-01T06:00:00+00:00", ()))
    assert read_cursor(tmp_path, "other") is None


def test_names_coexist_in_one_parseable_file(tmp_path: Path) -> None:
    write_cursor(tmp_path, "hourly", Watermark("2026-09-01T06:00:00+00:00", (1,)))
    write_cursor(tmp_path, "smoketest", Watermark("2026-09-02T06:00:00+00:00", (2, 3)))
    body = json.loads((tmp_path / "cursors.json").read_text(encoding="utf-8"))
    assert set(body) == {"hourly", "smoketest"}
    assert body["smoketest"] == {"at": "2026-09-02T06:00:00+00:00", "event_ids_at": [2, 3]}
    first = read_cursor(tmp_path, "hourly")
    assert first is not None and first.event_ids_at == (1,)
    # the write renames a temp file in the same directory; nothing is left behind
    assert [p.name for p in tmp_path.iterdir()] == ["cursors.json"]


def test_advancing_one_name_leaves_the_others_alone(tmp_path: Path) -> None:
    write_cursor(tmp_path, "hourly", Watermark("2026-09-01T06:00:00+00:00", (1,)))
    write_cursor(tmp_path, "smoketest", Watermark("2026-09-02T06:00:00+00:00", (2,)))
    write_cursor(tmp_path, "hourly", Watermark("2026-09-03T06:00:00+00:00", (9,)))
    other = read_cursor(tmp_path, "smoketest")
    moved = read_cursor(tmp_path, "hourly")
    assert other == Watermark("2026-09-02T06:00:00+00:00", (2,))
    assert moved == Watermark("2026-09-03T06:00:00+00:00", (9,))


def test_unreadable_state_re_reports_instead_of_crashing(tmp_path: Path) -> None:
    (tmp_path / "cursors.json").write_text("{not json", encoding="utf-8")
    assert read_cursor(tmp_path, "hourly") is None  # no cursor == last 24h, never a skip
    write_cursor(tmp_path, "hourly", Watermark("2026-09-01T06:00:00+00:00", ()))
    assert read_cursor(tmp_path, "hourly") == Watermark("2026-09-01T06:00:00+00:00", ())


def test_garbage_entries_are_ignored_not_trusted(tmp_path: Path) -> None:
    (tmp_path / "cursors.json").write_text(
        json.dumps({"a": {"at": 7}, "b": "nope", "c": {"at": "2026-09-01T06:00:00+00:00",
                                                      "event_ids_at": ["x", 4]}}),
        encoding="utf-8",
    )
    assert read_cursor(tmp_path, "a") is None
    assert read_cursor(tmp_path, "b") is None
    assert read_cursor(tmp_path, "c") == Watermark("2026-09-01T06:00:00+00:00", (4,))
