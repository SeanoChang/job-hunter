from dataclasses import replace
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
from jobhunter.models import AttemptManifest, DetailAttempt


def _m(source: str, board: str, t: datetime) -> AttemptManifest:
    return AttemptManifest(
        attempt_id=attempt_key(source, board, t), run_id="r", source=source, board=board,
        started_at=t, finished_at=t, url="u", http_status=200, transport="ok",
        blob_sha256=None, payload_bytes=0, record_count=0, adapter_version="x/1",
        registry_revision="rev", cli_version="0.1.0", error=None,
    )


def test_manifest_without_page_blobs_or_details_omits_them_from_json() -> None:
    m = _m("lever", "palantir", datetime(2026, 8, 18, 6, tzinfo=UTC))
    data = m.to_json()
    assert b"page_blobs" not in data
    assert b"details" not in data
    assert AttemptManifest.from_json(data) == m
    assert m.page_blobs is None
    assert m.details is None


def test_manifest_with_page_blobs_and_details_roundtrips() -> None:
    t = datetime(2026, 8, 18, 6, tzinfo=UTC)
    base = _m("workday", "nvidia", t)
    m = replace(
        base,
        page_blobs=("aa" * 32, "bb" * 32),
        details=(
            DetailAttempt(uid="wd:nvidia:1", blob_sha256="cc" * 32, http_status=200, error=None),
            DetailAttempt(uid="wd:nvidia:2", blob_sha256=None, http_status=500, error="http_error"),
        ),
    )
    data = m.to_json()
    assert b'"page_blobs":["' in data
    assert b'"details":[{' in data
    parsed = AttemptManifest.from_json(data)
    assert parsed == m
    assert parsed.page_blobs == ("aa" * 32, "bb" * 32)
    assert parsed.details is not None
    assert parsed.details[0].uid == "wd:nvidia:1"
    assert parsed.details[1].blob_sha256 is None
    assert parsed.details[1].error == "http_error"


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
