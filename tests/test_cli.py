import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import typer
from typer.testing import CliRunner

from jobhunter import cli
from jobhunter.http import Fetcher
from tests.conftest import TEST_DSN, fixture_bytes

runner = CliRunner()

REG = """
[[boards]]
company="Anthropic"
source="greenhouse"
board="anthropic"
[[boards]]
company="Palantir"
source="lever"
board="palantir"
"""


def _fake_ats(req: httpx.Request) -> httpx.Response:
    if "greenhouse" in req.url.host:
        return httpx.Response(200, content=fixture_bytes("greenhouse_board.json"))
    return httpx.Response(200, content=b"[]")


@pytest.fixture
def env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pg: psycopg.Connection[dict[str, Any]],
) -> Path:
    (tmp_path / "companies.toml").write_text(REG)
    monkeypatch.setenv("JOB_HUNTER_ARCHIVE_URL", f"file://{tmp_path / 'archive'}")
    monkeypatch.setenv("JOB_HUNTER_REGISTRY", str(tmp_path / "companies.toml"))
    monkeypatch.setenv("JOB_HUNTER_DATABASE_URL", TEST_DSN)
    row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert row is not None
    monkeypatch.setattr(cli, "_schema", str(row["s"]))
    monkeypatch.setattr(cli, "_make_fetcher", lambda: Fetcher(
        httpx.Client(transport=httpx.MockTransport(_fake_ats)), sleep=lambda s: None))
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 8, 18, 6, tzinfo=UTC))
    return tmp_path


def test_version() -> None:
    r = runner.invoke(cli.app, ["version", "-o", "table"])
    assert r.exit_code == 0 and "0.1.0" in r.stdout
    rj = runner.invoke(cli.app, ["version", "-o", "json"])
    assert rj.exit_code == 0 and json.loads(rj.stdout)["data"] == {"version": "0.1.0"}


def test_version_pipes_envelope_by_default() -> None:
    r = runner.invoke(cli.app, ["version"])
    body = json.loads(r.stdout)
    assert body["ok"] is True and body["data"]["version"]


def test_fetch_json_summary(env: Path) -> None:
    r = runner.invoke(cli.app, ["fetch", "-o", "json"])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)["data"]
    assert data["counts"] == {
        "boards": 2, "ok": 2, "envelope_error": 0, "http_error": 0, "transport_error": 0,
        "new_blobs": 2,
    }


def test_fetch_human_summary_and_status(env: Path) -> None:
    r = runner.invoke(cli.app, ["fetch", "-o", "table"])
    assert r.exit_code == 0 and "greenhouse:anthropic" in r.stdout and "ok" in r.stdout
    s = runner.invoke(cli.app, ["status", "-o", "table"])
    assert s.exit_code == 0
    assert "greenhouse:anthropic" in s.stdout and "2026-08-18T06:00:00Z" in s.stdout
    sj = runner.invoke(cli.app, ["status", "-o", "json"])
    rows = json.loads(sj.stdout)["data"]["boards"]
    assert {row["board"] for row in rows} == {"greenhouse:anthropic", "lever:palantir"}
    assert all(row["last_transport"] == "ok" for row in rows)


def test_status_marks_boards_never_fetched(env: Path) -> None:
    sj = runner.invoke(cli.app, ["status", "-o", "json"])
    rows = json.loads(sj.stdout)["data"]["boards"]
    assert all(row["last_attempt"] is None for row in rows)


def test_archive_ls(env: Path) -> None:
    runner.invoke(cli.app, ["fetch"])
    r = runner.invoke(cli.app, ["archive", "ls", "--board", "lever:palantir", "-o", "json"])
    items = json.loads(r.stdout)["data"]
    assert len(items) == 1 and items[0]["board"] == "lever:palantir"
    assert items[0]["attempt_id"].startswith("attempts/lever/palantir/2026/08/18T060000Z")


def test_registry_check_ok_and_bad(env: Path, tmp_path: Path) -> None:
    ok = runner.invoke(cli.app, ["registry", "check", "-o", "table"])
    assert ok.exit_code == 0 and "2 boards" in ok.stdout
    (tmp_path / "companies.toml").write_text('[[boards]]\ncompany="X"\nsource="nope"\nboard="x"\n')
    bad = runner.invoke(cli.app, ["registry", "check"])
    assert bad.exit_code == 6
    body = json.loads(bad.stdout)
    assert body["ok"] is False and "unknown source" in body["error"]["message"]


def test_missing_archive_url_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOB_HUNTER_ARCHIVE_URL", raising=False)
    r = runner.invoke(cli.app, ["status"])
    assert r.exit_code == 3
    assert "JOB_HUNTER_ARCHIVE_URL" in json.loads(r.stdout)["error"]["message"]


def test_config_error_on_a_tty_stays_off_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOB_HUNTER_ARCHIVE_URL", raising=False)
    r = runner.invoke(cli.app, ["status", "-o", "table"])
    assert r.exit_code == 3 and r.stdout == ""
    assert "JOB_HUNTER_ARCHIVE_URL" in r.stderr


def test_fetch_all_boards_failed_is_systemic(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    monkeypatch.setattr(cli, "_make_fetcher", lambda: Fetcher(
        httpx.Client(transport=httpx.MockTransport(down)), sleep=lambda s: None))
    r = runner.invoke(cli.app, ["fetch"])
    assert r.exit_code == 6


def test_fetch_board_option_is_validated(env: Path) -> None:
    r = runner.invoke(cli.app, ["fetch", "--board", "anthropic"])
    assert r.exit_code == 2 and "source:board" in json.loads(r.stdout)["error"]["message"]


def test_fetch_unregistered_board_is_systemic(env: Path) -> None:
    r = runner.invoke(cli.app, ["fetch", "--board", "greenhouse:not-registered", "--dry-run"])
    assert r.exit_code == 6 and "not-registered" in json.loads(r.stdout)["error"]["message"]


def test_fetch_all_envelope_failures_is_systemic(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def html(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>maintenance</html>")

    monkeypatch.setattr(cli, "_make_fetcher", lambda: Fetcher(
        httpx.Client(transport=httpx.MockTransport(html)), sleep=lambda s: None))
    r = runner.invoke(cli.app, ["fetch", "-o", "json"])
    assert r.exit_code == 6
    assert json.loads(r.stdout)["data"]["counts"]["envelope_error"] == 2


def test_fetch_requires_database_url_and_ingest_command(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JOB_HUNTER_DATABASE_URL", raising=False)
    r = runner.invoke(cli.app, ["fetch"])
    assert r.exit_code == 3
    assert "JOB_HUNTER_DATABASE_URL" in json.loads(r.stdout)["error"]["message"]
    monkeypatch.setenv("JOB_HUNTER_DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none")
    r = runner.invoke(cli.app, ["fetch", "-o", "json"])
    assert r.exit_code == 6
    assert json.loads(r.stdout)["data"]["db_error"]
    r = runner.invoke(cli.app, ["ingest"])
    assert r.exit_code == 5 and "database error" in json.loads(r.stdout)["error"]["message"]


def test_registry_list_and_rebuild(env: Path) -> None:
    assert runner.invoke(cli.app, ["fetch"]).exit_code == 0
    r = runner.invoke(cli.app, ["registry", "list", "-o", "json"])
    assert r.exit_code == 0
    assert {row["board"] for row in json.loads(r.stdout)["data"]} == {
        "greenhouse:anthropic", "lever:palantir"
    }
    r = runner.invoke(cli.app, ["rebuild", "--yes", "-o", "json"])
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["data"]["swapped"] is True
    r = runner.invoke(cli.app, ["status", "-o", "json"])
    rows = {row["board"]: row for row in json.loads(r.stdout)["data"]["boards"]}
    assert rows["greenhouse:anthropic"]["health"] == "ok"
    assert rows["greenhouse:anthropic"]["open"] == 1


def test_rebuild_off_tty_requires_yes(env: Path) -> None:
    r = runner.invoke(cli.app, ["rebuild"])
    assert r.exit_code == 2
    body = json.loads(r.stdout)
    assert body["ok"] is False and "--yes" in body["error"]["hint"]


def test_since_window_parsing() -> None:
    from jobhunter.cli import _parse_since

    assert _parse_since("24h").total_seconds() == 86400
    assert _parse_since("2d").total_seconds() == 172800
    assert _parse_since("30m").total_seconds() == 1800
    with pytest.raises(typer.Exit) as e:
        _parse_since("soon", "table")
    assert e.value.exit_code == 2


def test_lock_held_branches_honour_json(env: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    from jobhunter.store import db as _db

    assert _db.try_lock(pg)
    try:
        for args in (["ingest", "-o", "json"], ["rebuild", "--yes", "-o", "json"],
                     ["fetch", "-o", "json"]):
            r = runner.invoke(cli.app, args)
            assert r.exit_code == 0, (args, r.stdout)
            data = json.loads(r.stdout)["data"]
            assert data.get("lock_held") is True
    finally:
        _db.unlock(pg)


def test_db_version_on_half_created_schema_is_systemic(
    env: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    schema = pg.execute("SELECT current_schema() AS s").fetchone()["s"]
    pg.execute("DROP TABLE schema_meta")
    pg.commit()
    r = runner.invoke(cli.app, ["db", "version", "-o", "json"])
    assert r.exit_code == 6
    assert json.loads(r.stdout)["data"]["db"] is None
    assert schema  # silence unused warning


def test_ingest_exits_systemic_on_gap_manifests(
    env: Path, tmp_path: Path, pg: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from jobhunter.archive.local import LocalFS
    from jobhunter.models import Board
    from jobhunter.store.lifecycle import Ingestor
    from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry

    archive_root = tmp_path / "gap-archive"
    store = LocalFS(archive_root)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    body = board_payload("ashby", [ab_record("x", "T", "<p>t</p>")])
    ing = Ingestor(pg, store)
    ing.ingest(make_manifest(store, "ashby", "ramp", t0, body, registry_revision=rev))
    make_manifest(store, "ashby", "ramp", t0 + timedelta(days=1), body, registry_revision=rev)
    m2 = make_manifest(store, "ashby", "ramp", t0 + timedelta(days=2), body,
                       registry_revision=rev)
    ing.ingest(m2)
    pg.commit()
    monkeypatch.setenv("JOB_HUNTER_ARCHIVE_URL", f"file://{archive_root}")
    r = runner.invoke(cli.app, ["ingest", "-o", "json"])
    assert r.exit_code == 6
    data = json.loads(r.stdout)["data"]
    assert len(data["gaps"]) == 1 and "rebuild" in data["hint"]


def test_status_reports_db_size(env: Path) -> None:
    runner.invoke(cli.app, ["fetch"])
    sj = runner.invoke(cli.app, ["status", "-o", "json"])
    data = json.loads(sj.stdout)["data"]
    assert isinstance(data["db_size_bytes"], int) and data["db_size_bytes"] > 0
    sh = runner.invoke(cli.app, ["status", "-o", "table"])
    assert sh.exit_code == 0 and "db size" in sh.stdout


def test_verify_pass_and_fail(tmp_path: Path) -> None:
    from tests.l2.conftest import DOC_MD, minimal_record

    doc = tmp_path / "doc.md"
    doc.write_text(DOC_MD, encoding="utf-8")
    good = tmp_path / "good.json"
    good.write_text(json.dumps(minimal_record()), encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(good), str(doc), "-o", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["status"] == "pass"

    bad_record = minimal_record()
    bad_record["demand_profile"]["areas"][0]["claims"][0]["quote"]["text"] = "fabricated"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_record), encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(bad), str(doc), "-o", "table"])
    assert result.exit_code == 1, result.output
    assert "text_mismatch" in result.stdout
    assert "line " in result.stdout  # derived line:col shown for span findings


def test_verify_systemic(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("x", encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(tmp_path / "missing.json"), str(doc)])
    assert result.exit_code == 6


def test_verify_needs_a_document_or_a_hash(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["verify", str(tmp_path / "rec.json")])
    assert result.exit_code == 2
    assert "DOCUMENT_FILE" in json.loads(result.stdout)["error"]["message"]


def test_verify_systemic_non_dict_and_non_utf8(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("x", encoding="utf-8")
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2]", encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(arr), str(doc)])
    assert result.exit_code == 6, result.output

    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00")
    good = tmp_path / "good.json"
    good.write_text("{}", encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(good), str(bad)])
    assert result.exit_code == 6, result.output


def test_verify_human_output_shows_mismatch_diagnostics(tmp_path: Path) -> None:
    from tests.l2.conftest import DOC_MD, minimal_record

    doc = tmp_path / "doc.md"
    doc.write_text(DOC_MD, encoding="utf-8")
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["claims"][1]["quote"]["text"] = "0-2 YOE preferrd"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(rec), encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(bad), str(doc), "-o", "table"])
    assert result.exit_code == 1
    assert "expected:" in result.stdout and "found:" in result.stdout
    assert "matches the document for" in result.stdout


@pytest.fixture
def xenv(
    pg: psycopg.Connection[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    (tmp_path / "companies.toml").write_text(
        '[[boards]]\ncompany="X"\nsource="greenhouse"\nboard="x"\n'
    )
    monkeypatch.setenv("JOB_HUNTER_ARCHIVE_URL", f"file://{tmp_path / 'archive'}")
    monkeypatch.setenv("JOB_HUNTER_REGISTRY", str(tmp_path / "companies.toml"))
    monkeypatch.setenv("JOB_HUNTER_DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("JOB_HUNTER_L2_BASE_URL", "https://openrouter.test/api/v1")
    monkeypatch.setenv("JOB_HUNTER_L2_MODELS", "z-ai/*")
    monkeypatch.setenv("JOB_HUNTER_L2_MODEL_CANDIDATES", "z-ai/glm-5.2:free")
    row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert row is not None
    monkeypatch.setattr(cli, "_schema", str(row["s"]))
    from tests.l2.test_runner import GOOD, FakeEngine, _seed_doc

    _seed_doc(pg)
    monkeypatch.setattr(cli, "_make_engine", lambda settings: FakeEngine([GOOD]))
    return tmp_path


def test_extract_run_review_verify_status_end_to_end(
    xenv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    from tests.l2.test_runner import DH

    r = runner.invoke(cli.app, ["extract", "run", "-o", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)["data"]
    assert data["validated"] == 1

    r = runner.invoke(cli.app, ["extract", "review", "list", "-o", "json"])
    assert json.loads(r.stdout)["data"]["inbox"] == []

    r = runner.invoke(cli.app, ["extract", "review", "flag", DH, "-o", "json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["data"]["status"] == "needs_review"

    r = runner.invoke(cli.app, ["extract", "review", "list", "-o", "json"])
    inbox = json.loads(r.stdout)["data"]["inbox"]
    assert len(inbox) == 1 and inbox[0]["document_hash"] == DH

    r = runner.invoke(cli.app, ["extract", "review", "show", DH, "-o", "table"])
    assert r.exit_code == 0 and "needs_review" in r.stdout and "a1 ok" in r.stdout

    r = runner.invoke(cli.app, ["extract", "review", "accept", DH, "-o", "json"])
    assert json.loads(r.stdout)["data"]["status"] == "validated"

    r = runner.invoke(cli.app, ["verify", DH, "-o", "json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["data"]["status"] == "pass"

    r = runner.invoke(cli.app, ["status", "-o", "json"])
    assert r.exit_code == 0, r.output
    block = json.loads(r.stdout)["data"].get("extraction")
    assert block and block["by_status"] == {"validated": 1}
    assert block["observed_models_7d"] == ["z-ai/glm-5.2:free"]

    r = runner.invoke(cli.app, ["extract", "rebuild", "-o", "json"])
    assert r.exit_code == 0, r.output
    counts = json.loads(r.stdout)["data"]
    assert counts["attempts"] == 1 and counts["reviews"] == 2


def test_extract_run_requires_l2_config(xenv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOB_HUNTER_L2_BASE_URL")
    r = runner.invoke(cli.app, ["extract", "run"])
    assert r.exit_code == 3 and "JOB_HUNTER_L2" in json.loads(r.stdout)["error"]["message"]


def test_extract_show_unknown_prefix_is_not_found(xenv: Path) -> None:
    r = runner.invoke(cli.app, ["extract", "show", "deadbeef"])
    assert r.exit_code == 4
    assert "no document matches" in json.loads(r.stdout)["error"]["message"]


def test_reject_requires_note(xenv: Path) -> None:
    from tests.l2.test_runner import DH

    runner.invoke(cli.app, ["extract", "run"])
    r = runner.invoke(cli.app, ["extract", "review", "reject", DH])
    assert r.exit_code != 0  # typer enforces the missing --note


def test_verify_hash_with_missing_archive_object_is_systemic(
    xenv: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    from tests.l2.test_runner import DH

    r = runner.invoke(cli.app, ["extract", "run"])
    assert r.exit_code == 0, r.output
    row = pg.execute("SELECT chosen_attempt FROM extractions").fetchone()
    assert row is not None
    archive_root = xenv / "archive" / row["chosen_attempt"]
    archive_root.unlink()  # simulate a lost/unreplicated attempt object
    r = runner.invoke(cli.app, ["verify", DH])
    assert r.exit_code == 6, r.output  # infrastructure failure, never "findings failed"


def test_throttled_zero_progress_run_is_systemic(
    xenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobhunter.l2.engines import EngineThrottled
    from tests.l2.test_runner import FakeEngine

    monkeypatch.setattr(cli, "_make_engine", lambda s: FakeEngine([EngineThrottled("429")]))
    r = runner.invoke(cli.app, ["extract", "run"])
    assert r.exit_code == 6, r.output  # a scheduled run that did nothing must not report success


@pytest.fixture
def syncenv(
    env: Path,
    pg: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """`env` narrowed to the board whose fake payload is empty, plus L2 on a
    scripted engine: sync's extraction pass then sees exactly the seeded document."""
    (env / "companies.toml").write_text(
        '[[boards]]\ncompany="Palantir"\nsource="lever"\nboard="palantir"\n'
    )
    monkeypatch.setenv("JOB_HUNTER_L2_BASE_URL", "https://openrouter.test/api/v1")
    monkeypatch.setenv("JOB_HUNTER_L2_MODELS", "z-ai/*")
    monkeypatch.setenv("JOB_HUNTER_L2_MODEL_CANDIDATES", "z-ai/glm-5.2:free")
    from tests.l2.test_runner import GOOD, FakeEngine, _seed_doc

    _seed_doc(pg)
    monkeypatch.setattr(cli, "_make_engine", lambda settings: FakeEngine([GOOD]))
    return env


def test_sync_runs_ingest_then_fetch_then_extract(syncenv: Path) -> None:
    r = runner.invoke(cli.app, ["sync", "-o", "json"])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)["data"]
    assert list(data) == ["ingest", "fetch", "extract"]
    assert data["ingest"]["ingested"] == 0 and data["ingest"]["gaps"] == []
    assert data["fetch"]["counts"] == {
        "boards": 1, "ok": 1, "envelope_error": 0, "http_error": 0, "transport_error": 0,
        "new_blobs": 1,
    }
    assert data["extract"]["validated"] == 1


def test_sync_human_output_names_every_phase(syncenv: Path) -> None:
    r = runner.invoke(cli.app, ["sync", "-o", "table"])
    assert r.exit_code == 0, r.stderr
    assert "ingest:" in r.stdout and "fetch:" in r.stdout and "extract:" in r.stdout


def test_sync_no_extract_skips_the_pass(syncenv: Path) -> None:
    r = runner.invoke(cli.app, ["sync", "--no-extract", "-o", "json"])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)["data"]
    assert data["extract"] == {"skipped_reason": "--no-extract"}
    assert data["fetch"]["counts"]["ok"] == 1  # collection still ran


def test_sync_without_l2_candidates_says_so(env: Path) -> None:
    r = runner.invoke(cli.app, ["sync", "-o", "json"])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)["data"]
    assert data["extract"] == {"skipped_reason": "no JOB_HUNTER_L2_MODEL_CANDIDATES"}
    assert data["fetch"]["counts"]["ok"] == 2


def test_sync_reports_extraction_failure_without_failing_the_run(
    syncenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobhunter.l2.engines import EngineFatalError
    from tests.l2.test_runner import FakeEngine

    monkeypatch.setattr(
        cli, "_make_engine", lambda s: FakeEngine([EngineFatalError("credentials rejected")])
    )
    r = runner.invoke(cli.app, ["sync", "-o", "json"])
    # collection is irreplaceable, extraction recomputable: a bad engine day is reported
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)["data"]
    assert "credentials rejected" in data["extract"]["error"]
    assert data["fetch"]["counts"]["ok"] == 1


def test_sync_reports_a_payment_failure_as_an_engine_error(
    syncenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobhunter.l2.engines import EngineAuthError
    from tests.l2.test_runner import FakeEngine

    monkeypatch.setattr(
        cli, "_make_engine", lambda s: FakeEngine([EngineAuthError(402, "Insufficient credits")])
    )
    r = runner.invoke(cli.app, ["sync", "-o", "json"])
    assert r.exit_code == 0, r.stdout
    error = json.loads(r.stdout)["data"]["extract"]["error"]
    # the operator must read who refused and why, not "database error"
    assert error.startswith("engine error:") and "402" in error
    assert "Insufficient credits" in error and "database" not in error


def test_sync_throttled_extraction_with_no_progress_is_systemic(
    syncenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobhunter.l2.engines import EngineThrottled
    from tests.l2.test_runner import FakeEngine

    monkeypatch.setattr(cli, "_make_engine", lambda s: FakeEngine([EngineThrottled("429")]))
    r = runner.invoke(cli.app, ["sync", "-o", "json"])
    assert r.exit_code == 6, r.stdout
    data = json.loads(r.stdout)["data"]
    assert data["extract"]["throttled"] is True and data["extract"]["validated"] == 0
    assert data["fetch"]["counts"]["ok"] == 1


def test_sync_stops_and_exits_systemic_on_gap_manifests(
    env: Path, tmp_path: Path, pg: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from jobhunter.archive.local import LocalFS
    from jobhunter.models import Board
    from jobhunter.store.lifecycle import Ingestor
    from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry

    archive_root = tmp_path / "gap-archive"
    store = LocalFS(archive_root)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    body = board_payload("ashby", [ab_record("x", "T", "<p>t</p>")])
    ing = Ingestor(pg, store)
    ing.ingest(make_manifest(store, "ashby", "ramp", t0, body, registry_revision=rev))
    make_manifest(store, "ashby", "ramp", t0 + timedelta(days=1), body, registry_revision=rev)
    ing.ingest(make_manifest(store, "ashby", "ramp", t0 + timedelta(days=2), body,
                             registry_revision=rev))
    pg.commit()
    monkeypatch.setenv("JOB_HUNTER_ARCHIVE_URL", f"file://{archive_root}")
    r = runner.invoke(cli.app, ["sync", "-o", "json"])
    assert r.exit_code == 6, r.stdout
    data = json.loads(r.stdout)["data"]
    assert len(data["ingest"]["gaps"]) == 1
    # fetching would advance the watermark past the gap: nothing after ingest runs
    assert data["fetch"] == {"skipped_reason": "ingest gaps"}
    assert data["extract"] == {"skipped_reason": "ingest gaps"}


def test_doctor_on_a_healthy_environment(env: Path) -> None:
    r = runner.invoke(cli.app, ["doctor", "-o", "json"])
    assert r.exit_code == 0, r.stdout
    checks = json.loads(r.stdout)["data"]["checks"]
    assert [c["name"] for c in checks] == [
        "archive_url", "archive_probe", "database_url", "database_probe", "schema_version",
        "role", "l2",
    ]
    assert all(c["ok"] for c in checks), checks
    by_name = {c["name"]: c for c in checks}
    assert "extraction not configured" in by_name["l2"]["detail"]
    assert "writer" in by_name["role"]["detail"]  # the test DSN owns the schema


def test_doctor_empty_env_is_a_config_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JOB_HUNTER_ARCHIVE_URL", raising=False)
    monkeypatch.delenv("JOB_HUNTER_DATABASE_URL", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))  # no ~/.config/job-hunter/env
    monkeypatch.chdir(tmp_path)  # no ./.env
    r = runner.invoke(cli.app, ["doctor", "-o", "json"])
    assert r.exit_code == 3, r.stdout
    checks = {c["name"]: c for c in json.loads(r.stdout)["data"]["checks"]}
    assert checks["archive_url"]["ok"] is False
    assert checks["database_url"]["ok"] is False
    # every check runs: a missing variable never stops the report at the first failure
    assert set(checks) >= {"archive_probe", "database_probe", "schema_version", "role", "l2"}
    assert all(c["hint"] for c in checks.values() if not c["ok"]), checks


def test_doctor_reports_an_unreachable_database_as_backend(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOB_HUNTER_DATABASE_URL", "postgresql://nobody:secret@127.0.0.1:1/x")
    r = runner.invoke(cli.app, ["doctor", "-o", "json"])
    assert r.exit_code == 5, r.stdout
    checks = {c["name"]: c for c in json.loads(r.stdout)["data"]["checks"]}
    assert checks["database_url"]["ok"] is True  # the variable is set; the server is not there
    assert checks["database_probe"]["ok"] is False and checks["database_probe"]["hint"]
    assert "secret" not in r.stdout  # a DSN carries a password; doctor never echoes it
    h = runner.invoke(cli.app, ["doctor", "-o", "table"])
    assert h.exit_code == 5
    assert "FAIL  database_probe" in h.stdout and "hint:" in h.stdout


def test_doctor_names_the_missing_r2_variables(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobhunter.archive.local import LocalFS

    monkeypatch.setenv("JOB_HUNTER_ARCHIVE_URL", "s3://bucket/prefix")
    # the R2 variables are a config question, so the probe stays local and offline
    monkeypatch.setattr(cli, "open_store", lambda url: LocalFS(env / "archive"))
    for var in ("AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)
    r = runner.invoke(cli.app, ["doctor", "-o", "json"])
    assert r.exit_code == 3, r.stdout
    checks = {c["name"]: c for c in json.loads(r.stdout)["data"]["checks"]}
    assert checks["aws_credentials"]["ok"] is False
    assert "AWS_SECRET_ACCESS_KEY" in checks["aws_credentials"]["detail"]
    assert checks["archive_probe"]["ok"] is True  # credentials missing, backend still answers


def _schema_data() -> dict[str, Any]:
    r = runner.invoke(cli.app, ["schema", "-o", "json"])
    assert r.exit_code == 0, r.stdout
    data: dict[str, Any] = json.loads(r.stdout)["data"]
    return data


def test_schema_walks_the_live_command_tree() -> None:
    data = _schema_data()
    paths = {c["path"] for c in data["commands"]}
    assert {"pulse", "sync", "doctor", "q postings", "q claims",
            "extract review accept", "db init"} <= paths
    assert "q" not in paths and "extract review" not in paths  # groups are not invocable
    postings = next(c for c in data["commands"] if c["path"] == "q postings")
    assert "newest first" in postings["help"]
    params = {p["name"]: p for p in postings["params"]}
    assert set(params["after"]) == {"name", "opts", "type", "default", "choices"}
    assert params["limit"]["default"] == 50
    assert params["output"]["opts"] == ["--output", "-o"]
    # an agent must be able to type any flag the app accepts, arguments included
    assert {p["opts"][0] for p in params.values()} >= {"--board", "--status", "--fields"}
    assert next(p for p in next(c for c in data["commands"] if c["path"] == "q posting")["params"]
                if p["name"] == "uid")["opts"] == ["uid"]


def test_schema_declares_the_exit_table_and_active_versions() -> None:
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.markdown import NORMALIZER_VERSION
    from jobhunter.store import db

    data = _schema_data()
    codes = data["contract"]["exit_codes"]
    assert len(codes) == 7
    assert codes["0"] == "success" and codes["4"].startswith("unknown or ambiguous")
    assert codes["6"].startswith("systemic")
    assert data["versions"] == {
        "cli": "0.1.0", "schema_version": db.SCHEMA_VERSION, "normalizer": NORMALIZER_VERSION,
        "prompt": PROMPT_VERSION, "validator": VALIDATOR_VERSION,
    }


def test_schema_envelope_describes_what_the_verbs_actually_print(env: Path) -> None:
    import jsonschema

    envelope = _schema_data()["contract"]["envelope"]
    jsonschema.validate(json.loads(runner.invoke(cli.app, ["doctor"]).stdout), envelope)
    bad = runner.invoke(cli.app, ["q", "postings", "--status", "bogus"])
    assert bad.exit_code == 2
    jsonschema.validate(json.loads(bad.stdout), envelope)
    # the envelope is a contract, not a shape suggestion: a stray top-level key fails it
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"ok": True, "data": [], "meta": {"truncated": False},
                             "extra": 1}, envelope)


def test_report_is_gone_and_no_verb_still_takes_json() -> None:
    """The 2026-09-01 break, asserted against the live command tree: `report` is
    superseded by `pulse`/`q events`, and `-o json` is the only output switch."""
    data = _schema_data()
    paths = {c["path"] for c in data["commands"]}
    assert "report" not in paths
    assert {"pulse", "q events"} <= paths  # what carries the behaviour now
    flags = {opt for c in data["commands"] for p in c["params"] for opt in p["opts"]}
    assert "--json" not in flags
    assert runner.invoke(cli.app, ["report", "--since", "1d"]).exit_code != 0


def test_schema_human_output_lists_the_verbs() -> None:
    r = runner.invoke(cli.app, ["schema", "-o", "table"])
    assert r.exit_code == 0, r.stderr
    assert "q postings" in r.stdout and "--after" in r.stdout and "0 success" in r.stdout


def test_skill_prints_the_shipped_guide() -> None:
    r = runner.invoke(cli.app, ["skill", "-o", "table"])
    assert r.exit_code == 0, r.stderr
    assert r.stdout.startswith("---") and "name: job-hunter-cli" in r.stdout
    assert "pulse --cursor" in r.stdout
    assert "--fields" in r.stdout  # the token-economy section survives edits


def test_skill_piped_writes_an_installable_file() -> None:
    """The documented install is `job-hunter skill > .../SKILL.md`, so piped stdout
    must be the file itself — an envelope there has no frontmatter and never loads."""
    from importlib import resources

    shipped = resources.files("jobhunter.skill_data").joinpath("SKILL.md").read_text("utf-8")
    r = runner.invoke(cli.app, ["skill"])  # CliRunner stdout is a pipe, not a TTY
    assert r.exit_code == 0, r.stderr
    assert r.stdout == shipped


def test_skill_json_wraps_the_markdown() -> None:
    body = json.loads(runner.invoke(cli.app, ["skill", "-o", "json"]).stdout)
    assert body["ok"] is True and body["data"]["markdown"].startswith("---")
