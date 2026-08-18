from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobhunter.archive.keys import attempt_key
from jobhunter.archive.local import LocalFS
from jobhunter.archive.manifests import (
    all_sorted_by_time,
    iter_manifests,
    latest_per_board,
    write_manifest,
)
from jobhunter.models import AttemptManifest


def _m(source: str, board: str, t: datetime) -> AttemptManifest:
    return AttemptManifest(
        attempt_id=attempt_key(source, board, t), run_id="r", source=source, board=board,
        started_at=t, finished_at=t, url="u", http_status=200, transport="ok",
        blob_sha256=None, payload_bytes=0, record_count=0, adapter_version="x/1",
        registry_revision="rev", cli_version="0.1.0", error=None,
    )


def test_write_and_iter_roundtrip(tmp_path: Path) -> None:
    s = LocalFS(tmp_path)
    t = datetime(2026, 8, 18, 6, tzinfo=UTC)
    m = _m("lever", "palantir", t)
    assert write_manifest(s, m) is True
    assert write_manifest(s, m) is False
    assert list(iter_manifests(s)) == [m]
    assert list(iter_manifests(s, "lever", "palantir")) == [m]
    assert list(iter_manifests(s, "ashby")) == []


def test_latest_per_board_and_time_order(tmp_path: Path) -> None:
    s = LocalFS(tmp_path)
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    early_ashby = _m("ashby", "ramp", t0 - timedelta(days=1))
    late_ashby = _m("ashby", "ramp", t0)
    lever = _m("lever", "palantir", t0 - timedelta(hours=1))
    for m in (late_ashby, early_ashby, lever):
        write_manifest(s, m)
    latest = latest_per_board(s)
    assert latest == {"ashby:ramp": late_ashby, "lever:palantir": lever}
    assert all_sorted_by_time(s) == [early_ashby, lever, late_ashby]


def test_iter_manifests_board_filter_without_source(tmp_path: Path) -> None:
    s = LocalFS(tmp_path)
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    write_manifest(s, _m("ashby", "ramp", t0))
    write_manifest(s, _m("lever", "palantir", t0))
    assert [m.board for m in iter_manifests(s, None, "palantir")] == ["palantir"]
