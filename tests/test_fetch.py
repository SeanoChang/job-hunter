import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from jobhunter.archive.keys import blob_key, registry_key
from jobhunter.archive.local import LocalFS
from jobhunter.archive.manifests import iter_manifests
from jobhunter.config import Settings
from jobhunter.fetch import gzip_bytes, run
from jobhunter.http import Fetcher
from tests.conftest import TEST_DSN, fixture_bytes

REG = """
[[boards]]
company="Anthropic"
source="greenhouse"
board="anthropic"
[[boards]]
company="Ramp"
source="ashby"
board="ramp"
[[boards]]
company="Palantir"
source="lever"
board="palantir"
"""


def _settings(tmp_path: Path) -> Settings:
    (tmp_path / "companies.toml").write_text(REG)
    return Settings(archive_url=f"file://{tmp_path / 'archive'}",
                    registry_path=tmp_path / "companies.toml", home=tmp_path, database_url=None)


def _fetcher(handler) -> Fetcher:  # type: ignore[no-untyped-def]
    return Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


def _fake_ats(req: httpx.Request) -> httpx.Response:
    host = req.url.host
    if "greenhouse" in host:
        return httpx.Response(200, content=fixture_bytes("greenhouse_board.json"))
    if "ashby" in host:
        return httpx.Response(200, content=fixture_bytes("ashby_board.json"))
    return httpx.Response(503, content=b"down")


def test_gzip_bytes_is_deterministic() -> None:
    assert gzip_bytes(b"abc") == gzip_bytes(b"abc")
    assert gzip.decompress(gzip_bytes(b"abc")) == b"abc"


def test_run_writes_manifests_blobs_and_registry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    summary = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, ingest=False)
    store = LocalFS(tmp_path / "archive")

    assert store.exists(registry_key(summary.registry_revision))
    ms = {m.board: m for m in iter_manifests(store)}
    assert set(ms) == {"anthropic", "ramp", "palantir"}

    gh = ms["anthropic"]
    assert gh.transport == "ok" and gh.http_status == 200 and gh.record_count == 1
    assert gh.blob_sha256 and store.exists(blob_key(gh.blob_sha256))
    assert gzip.decompress(store.get(blob_key(gh.blob_sha256))) == fixture_bytes(
        "greenhouse_board.json"
    )
    assert gh.attempt_id == "attempts/greenhouse/anthropic/2026/08/18T060000Z.json"
    assert gh.registry_revision == summary.registry_revision
    assert gh.adapter_version == "greenhouse/1"

    lv = ms["palantir"]
    assert lv.transport == "http_error" and lv.http_status == 503 and lv.blob_sha256 is None
    assert lv.record_count is None and lv.error == "HTTP 503"

    counts = summary.counts()
    assert counts == {
        "boards": 3, "ok": 2, "envelope_error": 0, "http_error": 1, "transport_error": 0,
        "new_blobs": 2,
    }


def test_second_run_with_same_bodies_writes_no_new_blobs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    t1 = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 8, 19, 6, 0, 0, tzinfo=UTC)
    run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t1, ingest=False)
    s2 = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t2, ingest=False)
    assert s2.counts()["new_blobs"] == 0
    store = LocalFS(tmp_path / "archive")
    assert len(list(iter_manifests(store, "greenhouse", "anthropic"))) == 2


def test_only_and_dry_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    s = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, only="ashby:ramp", dry_run=True)
    assert [o.board.key for o in s.outcomes] == ["ashby:ramp"]
    assert s.outcomes[0].manifest.transport == "ok"
    assert list(iter_manifests(LocalFS(tmp_path / "archive"))) == []


def test_envelope_failure_still_archives_blob_with_null_record_count(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>maintenance</html>")

    t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    s = run(settings, fetcher=_fetcher(h), now=lambda: t, only="lever:palantir", ingest=False)
    m = s.outcomes[0].manifest
    assert m.transport == "ok" and m.record_count is None and m.blob_sha256


def test_summary_to_dict_is_json_serialisable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    s = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, ingest=False)
    json.dumps(s.to_dict())


def test_envelope_failure_is_recorded_as_error_and_not_counted_ok(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>maintenance</html>")

    t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    s = run(settings, fetcher=_fetcher(h), now=lambda: t, only="lever:palantir", ingest=False)
    m = s.outcomes[0].manifest
    assert m.transport == "ok" and m.record_count is None
    assert m.error is not None and m.error.startswith("envelope:")
    counts = s.counts()
    assert counts["ok"] == 0 and counts["envelope_error"] == 1


def test_unknown_board_raises(tmp_path: Path) -> None:
    from jobhunter.fetch import UnknownBoardError

    settings = _settings(tmp_path)
    t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    with pytest.raises(UnknownBoardError, match="greenhouse:nope"):
        run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, only="greenhouse:nope",
            ingest=False)


def test_manifest_key_collision_is_loud(tmp_path: Path) -> None:
    from jobhunter.archive import ArchiveError

    settings = _settings(tmp_path)
    t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, only="ashby:ramp", ingest=False)
    with pytest.raises(ArchiveError, match="already exists"):
        run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, only="ashby:ramp",
            ingest=False)


def test_run_ingests_into_db(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    settings = replace(_settings(tmp_path), database_url=TEST_DSN)
    t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert row is not None
    schema = str(row["s"])
    summary = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, schema=schema)
    assert summary.db_error is None and summary.ingested == 3 and not summary.lock_held
    n = pg.execute("SELECT count(*) AS n FROM fetch_attempts").fetchone()
    assert n is not None and n["n"] == 3
    healths = {
        r["board"]: r["health"] for r in pg.execute("SELECT board, health FROM fetch_attempts")
    }
    assert healths == {"anthropic": "ok", "ramp": "ok", "palantir": "error"}
    postings = pg.execute("SELECT count(*) AS n FROM postings").fetchone()
    assert postings is not None and postings["n"] == 2


def test_run_archives_even_when_db_is_down(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), database_url="postgresql://nobody:x@127.0.0.1:1/none")
    t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    summary = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t)
    assert summary.db_error and summary.ingested == 0
    assert len(list(iter_manifests(LocalFS(tmp_path / "archive")))) == 3


def test_run_returns_lock_held_when_another_run_holds_it(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    from jobhunter.store import db as _db

    settings = replace(_settings(tmp_path), database_url=TEST_DSN)
    row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert row is not None
    schema = str(row["s"])
    assert _db.try_lock(pg)
    try:
        t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
        summary = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, schema=schema)
        assert summary.lock_held and summary.outcomes == []
    finally:
        _db.unlock(pg)
