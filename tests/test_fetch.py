import gzip
import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

import jobhunter.sources as sources_mod
from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.keys import blob_key, registry_key
from jobhunter.archive.local import LocalFS
from jobhunter.archive.manifests import iter_manifests
from jobhunter.config import Settings
from jobhunter.fetch import TwoPhaseBudget, gzip_bytes, is_healthy, run
from jobhunter.http import Fetcher
from jobhunter.models import AttemptManifest, Board, PostingVersion
from jobhunter.sources.base import ListPage, ListRow, RequestSpec
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


def _manifest(**overrides: Any) -> AttemptManifest:
    now = datetime(2026, 9, 4, tzinfo=UTC)
    base: dict[str, Any] = dict(
        attempt_id="a1", run_id="r1", source="workday", board="nvidia",
        started_at=now, finished_at=now, url="https://x.example/jobs",
        http_status=200, transport="ok", blob_sha256="b" * 64, payload_bytes=1,
        record_count=1, adapter_version="workday/1", registry_revision="rev",
        cli_version="0", error=None,
    )
    base.update(overrides)
    return AttemptManifest(**base)


def test_two_phase_manifest_without_blob_is_healthy() -> None:
    m = _manifest(blob_sha256=None, page_blobs=("p" * 64,))
    assert is_healthy(m)


def test_two_phase_manifest_with_error_is_not_healthy() -> None:
    m = _manifest(blob_sha256=None, page_blobs=("p" * 64,), error="boom")
    assert not is_healthy(m)


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


def test_ingest_failures_are_reported_not_raised(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    from jobhunter.store import db as _db

    settings = replace(_settings(tmp_path), database_url=TEST_DSN)
    schema = pg.execute("SELECT current_schema() AS s").fetchone()["s"]
    t_late = datetime(2026, 8, 19, 6, 0, 0, tzinfo=UTC)
    t_early = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    first = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t_late, schema=schema)
    assert first.db_error is None
    # a run whose clock is earlier than the last ingested attempt -> OutOfOrder inside ingest
    s = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t_early, schema=schema)
    assert s.db_error and "OutOfOrder" in s.db_error and s.ingested == 0
    assert len(list(iter_manifests(LocalFS(tmp_path / "archive")))) == 6  # archive still written
    conn = _db.connect(TEST_DSN, schema=schema)
    try:
        assert _db.try_lock(conn)  # lock was released despite the failure
        _db.unlock(conn)
    finally:
        conn.close()


def test_run_drains_pending_manifests_before_its_own(
    tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    """A manifest archived while the DB was down is replayed by the NEXT run, not lost."""
    from tests.store.helpers import make_manifest

    settings = replace(_settings(tmp_path), database_url=TEST_DSN)
    schema = pg.execute("SELECT current_schema() AS s").fetchone()["s"]
    t0 = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    s0 = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t0, schema=schema)
    assert s0.db_error is None
    # day 1: archive-only (the DB was down that day)
    store = LocalFS(tmp_path / "archive")
    rev = s0.registry_revision
    body = fixture_bytes("ashby_board.json")
    make_manifest(store, "ashby", "ramp", t0 + timedelta(days=1), body, registry_revision=rev)
    # day 2: a normal run must drain day 1 before ingesting its own manifests
    t2 = t0 + timedelta(days=2)
    s2 = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t2, schema=schema)
    assert s2.db_error is None and s2.replayed == 1 and s2.gaps == []
    days = [r["started_at"].day for r in pg.execute(
        "SELECT started_at FROM fetch_attempts WHERE board = 'ramp' ORDER BY started_at"
    ).fetchall()]
    assert days == [18, 19, 20]
    runs_col = [r["runs"] for r in pg.execute(
        "SELECT runs FROM presence WHERE uid LIKE 'ab:%' ORDER BY first_at"
    ).fetchall()]
    assert runs_col == [3]  # one continuous interval; no fabricated continuity


def test_run_pings_after_fetch(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), ping_url="https://hc.example.com/ping/uuid")
    pings: list[str] = []
    t = datetime(2026, 8, 18, 6, tzinfo=UTC)
    run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, ingest=False, ping=pings.append)
    assert pings == ["https://hc.example.com/ping/uuid"]


def test_run_skips_ping_without_url_or_on_dry_run(tmp_path: Path) -> None:
    pings: list[str] = []
    t = datetime(2026, 8, 18, 6, tzinfo=UTC)
    run(_settings(tmp_path), fetcher=_fetcher(_fake_ats), now=lambda: t, ingest=False,
        ping=pings.append)
    assert pings == []
    dry = replace(_settings(tmp_path), ping_url="https://hc.example.com/ping/uuid")
    run(dry, fetcher=_fetcher(_fake_ats), now=lambda: t, ingest=False, dry_run=True,
        ping=pings.append)
    assert pings == []


def test_run_survives_ping_failure(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), ping_url="https://hc.example.com/ping/uuid")

    def boom(url: str) -> None:
        raise OSError("ping endpoint down")

    t = datetime(2026, 8, 18, 6, tzinfo=UTC)
    s = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, ingest=False, ping=boom)
    assert s.db_error is None and len(s.outcomes) == 3


# ---- two-phase boards (list + detail), spec 2026-09-04 §3.2/§3.4

WD_REG = """
[[boards]]
company="NVIDIA"
source="workday"
board="nvidia"
host="wd5"
site="NVIDIAExternalCareerSite"
"""


class FakeTwoPhase:
    """A TwoPhaseSource with no I/O at all: it only describes requests and parses bodies."""

    name = "workday"
    adapter_version = "fake/1"

    def __init__(self, page_size: int = 2) -> None:
        self.page_size = page_size

    def list_url(self, board: Board, offset: int) -> RequestSpec:
        return RequestSpec(
            f"https://wd.example/{board.board}/jobs",
            "POST",
            {"offset": offset, "limit": self.page_size},
        )

    def parse_list(self, body: bytes) -> ListPage:
        data = json.loads(body)
        rows = tuple(
            ListRow(uid=j["id"], detail_path=f"/job/{j['id']}", title=j["title"], payload=j)
            for j in data["jobs"]
        )
        return ListPage(rows=rows, total=int(data["total"]))

    def detail_url(self, board: Board, row: ListRow) -> RequestSpec:
        return RequestSpec(f"https://wd.example{row.detail_path}")

    def normalize_detail(self, body: bytes, row: ListRow, board: Board) -> PostingVersion:
        d = json.loads(body)
        return PostingVersion(
            source=self.name, board=board.board, source_id=row.uid, title=row.title or "",
            company=board.company, locations=(), workplace_type=None, is_remote=None,
            department=None, team=None, employment_type=None, compensation=None,
            url=None, apply_url=None, source_created_at=None, source_updated_at=None,
            description_html=d["description"],
        )


class RecordingStore:
    """An archive that remembers the order of its writes, to prove archive-first ordering."""

    def __init__(self, inner: ArchiveStore) -> None:
        self.inner = inner
        self.writes: list[str] = []

    def put(self, key: str, data: bytes) -> bool:
        self.writes.append(key)
        return self.inner.put(key, data)

    def get(self, key: str) -> bytes:
        return self.inner.get(key)

    def exists(self, key: str) -> bool:
        return self.inner.exists(key)

    def list(self, prefix: str, start_after: str | None = None) -> Iterator[str]:
        return self.inner.list(prefix, start_after=start_after)


@pytest.fixture
def two_phase(monkeypatch: pytest.MonkeyPatch) -> FakeTwoPhase:
    src = FakeTwoPhase()
    monkeypatch.setitem(sources_mod.TWO_PHASE_SOURCES, "workday", src)
    return src


def _wd_settings(tmp_path: Path) -> Settings:
    (tmp_path / "companies.toml").write_text(WD_REG)
    return Settings(archive_url=f"file://{tmp_path / 'archive'}",
                    registry_path=tmp_path / "companies.toml", home=tmp_path, database_url=None)


def _wd_handler(
    *,
    total: int = 5,
    list_status: int = 200,
    list_body: bytes | None = None,
    detail_status: int = 200,
    calls: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def h(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(f"{req.method} {req.url.path}")
        if req.url.path.endswith("/jobs"):
            if list_body is not None or list_status != 200:
                return httpx.Response(list_status, content=list_body or b"denied")
            q = json.loads(req.content)
            off, lim = int(q["offset"]), int(q["limit"])
            jobs = [{"id": f"j{i}", "title": f"Engineer {i}"} for i in range(off, min(off + lim,
                                                                                     total))]
            return httpx.Response(200, content=json.dumps({"total": total, "jobs": jobs}).encode())
        uid = req.url.path.rsplit("/", 1)[-1]
        body = json.dumps({"id": uid, "description": f"<p>{uid}</p>"}).encode()
        return httpx.Response(detail_status, content=body)

    return h


def _detail_uids(m: AttemptManifest) -> list[str]:
    return [d.uid for d in m.details or ()]


def test_two_phase_pages_the_list_and_archives_pages_before_the_manifest(
    tmp_path: Path, two_phase: FakeTwoPhase
) -> None:
    store = RecordingStore(LocalFS(tmp_path / "archive"))
    t = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
    s = run(_wd_settings(tmp_path), store=store, fetcher=_fetcher(_wd_handler(total=5)),
            now=lambda: t, ingest=False, budget=TwoPhaseBudget(detail_budget=0))
    m = s.outcomes[0].manifest

    assert m.transport == "ok" and m.error is None and m.blob_sha256 is None
    assert m.page_blobs is not None and len(m.page_blobs) == 3  # 5 rows at 2/page
    assert m.record_count == 5 and m.details == ()
    assert m.attempt_id == "attempts/workday/nvidia/2026/09/04T060000Z.json"
    assert m.adapter_version == "fake/1"

    for sha in m.page_blobs:
        assert store.exists(blob_key(sha))
    first = json.loads(gzip.decompress(store.get(blob_key(m.page_blobs[0]))))
    assert [j["id"] for j in first["jobs"]] == ["j0", "j1"]

    # archive-first: every page blob is written before the manifest that names it
    assert store.writes[-1] == m.attempt_id
    page_keys = [blob_key(sha) for sha in m.page_blobs]
    assert store.writes[-4:-1] == page_keys


def test_two_phase_stops_at_the_page_cap_and_says_so(
    tmp_path: Path, two_phase: FakeTwoPhase
) -> None:
    t = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
    s = run(_wd_settings(tmp_path), fetcher=_fetcher(_wd_handler(total=1000)), now=lambda: t,
            ingest=False, budget=TwoPhaseBudget(page_cap=3, detail_budget=0))
    m = s.outcomes[0].manifest
    assert m.page_blobs is not None and len(m.page_blobs) == 3
    assert m.record_count == 6  # 3 pages x 2 rows, of 1000 listed
    assert m.error is not None and m.error.startswith("page cap:")
    assert not is_healthy(m)  # truncated coverage never reads as a full snapshot


def test_two_phase_budget_spends_new_uids_first_and_never_exceeds_the_limit(
    tmp_path: Path, two_phase: FakeTwoPhase
) -> None:
    store = LocalFS(tmp_path / "archive")
    t = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
    s = run(_wd_settings(tmp_path), fetcher=_fetcher(_wd_handler(total=5)), now=lambda: t,
            ingest=False, budget=TwoPhaseBudget(detail_budget=2))
    m = s.outcomes[0].manifest
    assert _detail_uids(m) == ["j0", "j1"]  # list order: newest listed first
    assert m.record_count == 5  # presence still covers every row, budget or not
    for d in m.details or ():
        assert d.blob_sha256 is not None and store.exists(blob_key(d.blob_sha256))
        assert json.loads(gzip.decompress(store.get(blob_key(d.blob_sha256))))["id"] == d.uid


def test_two_phase_budget_skips_details_already_archived_and_still_fresh(
    tmp_path: Path, two_phase: FakeTwoPhase
) -> None:
    settings = _wd_settings(tmp_path)
    t1 = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
    t2 = t1 + timedelta(hours=1)
    budget = TwoPhaseBudget(detail_budget=2)
    run(settings, fetcher=_fetcher(_wd_handler(total=5)), now=lambda: t1, ingest=False,
        budget=budget)
    s2 = run(settings, fetcher=_fetcher(_wd_handler(total=5)), now=lambda: t2, ingest=False,
             budget=budget)
    assert _detail_uids(s2.outcomes[0].manifest) == ["j2", "j3"]


def test_two_phase_budget_sweeps_details_older_than_redetail_days(
    tmp_path: Path, two_phase: FakeTwoPhase
) -> None:
    settings = _wd_settings(tmp_path)
    t1 = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
    t2 = t1 + timedelta(days=8)
    budget = TwoPhaseBudget(detail_budget=2, redetail_days=7)
    s1 = run(settings, fetcher=_fetcher(_wd_handler(total=3)), now=lambda: t1, ingest=False,
             budget=budget)
    assert _detail_uids(s1.outcomes[0].manifest) == ["j0", "j1"]
    s2 = run(settings, fetcher=_fetcher(_wd_handler(total=3)), now=lambda: t2, ingest=False,
             budget=budget)
    # the one uid never fetched first, then the oldest detail; still exactly the budget
    assert _detail_uids(s2.outcomes[0].manifest) == ["j2", "j0"]


def test_two_phase_budget_of_zero_fetches_no_details_but_still_lists(
    tmp_path: Path, two_phase: FakeTwoPhase
) -> None:
    t = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
    s = run(_wd_settings(tmp_path), fetcher=_fetcher(_wd_handler(total=5)), now=lambda: t,
            ingest=False, budget=TwoPhaseBudget(detail_budget=0))
    m = s.outcomes[0].manifest
    assert m.details == () and m.record_count == 5


def test_two_phase_blocked_on_a_403_list_skips_the_board_without_retry(
    tmp_path: Path, two_phase: FakeTwoPhase
) -> None:
    calls: list[str] = []
    t = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
    s = run(_wd_settings(tmp_path), fetcher=_fetcher(_wd_handler(list_status=403, calls=calls)),
            now=lambda: t, ingest=False)
    m = s.outcomes[0].manifest
    assert m.transport == "blocked" and m.http_status == 403
    assert m.error is not None and m.error.startswith("blocked:")
    assert m.page_blobs == () and m.details == () and m.record_count is None
    assert not is_healthy(m)
    assert calls == ["POST /nvidia/jobs"]  # one request: no retry, no details


def test_two_phase_blocked_on_a_challenge_page_body(
    tmp_path: Path, two_phase: FakeTwoPhase
) -> None:
    challenge = b"<html><head><title>Just a moment...</title></head><body>captcha</body></html>"
    calls: list[str] = []
    t = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
    s = run(_wd_settings(tmp_path), fetcher=_fetcher(_wd_handler(list_body=challenge, calls=calls)),
            now=lambda: t, ingest=False)
    m = s.outcomes[0].manifest
    assert m.transport == "blocked" and m.http_status == 200
    assert m.page_blobs == () and m.details == ()
    assert calls == ["POST /nvidia/jobs"]


def test_two_phase_detail_failure_is_recorded_and_the_run_survives(
    tmp_path: Path, two_phase: FakeTwoPhase
) -> None:
    t = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
    s = run(_wd_settings(tmp_path), fetcher=_fetcher(_wd_handler(total=3, detail_status=404)),
            now=lambda: t, ingest=False, budget=TwoPhaseBudget(detail_budget=2))
    m = s.outcomes[0].manifest
    assert m.transport == "ok" and m.error is None and m.record_count == 3
    assert [(d.uid, d.blob_sha256, d.http_status, d.error) for d in m.details or ()] == [
        ("j0", None, 404, "HTTP 404"), ("j1", None, 404, "HTTP 404"),
    ]
    # a failed detail leaves the uid new, so the next run retries it ahead of the sweep
    s2 = run(_wd_settings(tmp_path), fetcher=_fetcher(_wd_handler(total=3)),
             now=lambda: t + timedelta(hours=1), ingest=False,
             budget=TwoPhaseBudget(detail_budget=2))
    assert _detail_uids(s2.outcomes[0].manifest) == ["j0", "j1"]


def test_two_phase_manifest_round_trips_through_the_archive(
    tmp_path: Path, two_phase: FakeTwoPhase
) -> None:
    t = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
    s = run(_wd_settings(tmp_path), fetcher=_fetcher(_wd_handler(total=3)), now=lambda: t,
            ingest=False, budget=TwoPhaseBudget(detail_budget=1))
    (stored,) = list(iter_manifests(LocalFS(tmp_path / "archive"), "workday", "nvidia"))
    assert stored == s.outcomes[0].manifest


def test_two_phase_dry_run_writes_nothing(tmp_path: Path, two_phase: FakeTwoPhase) -> None:
    t = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
    s = run(_wd_settings(tmp_path), fetcher=_fetcher(_wd_handler(total=3)), now=lambda: t,
            dry_run=True)
    assert s.outcomes[0].manifest.record_count == 3
    assert list(iter_manifests(LocalFS(tmp_path / "archive"))) == []
