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
    r = runner.invoke(cli.app, ["version"])
    assert r.exit_code == 0 and "0.1.0" in r.stdout
    rj = runner.invoke(cli.app, ["version", "--json"])
    assert rj.exit_code == 0 and json.loads(rj.stdout) == {"version": "0.1.0"}


def test_fetch_json_summary(env: Path) -> None:
    r = runner.invoke(cli.app, ["fetch", "--json"])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)
    assert data["counts"] == {
        "boards": 2, "ok": 2, "envelope_error": 0, "http_error": 0, "transport_error": 0,
        "new_blobs": 2,
    }


def test_fetch_human_summary_and_status(env: Path) -> None:
    r = runner.invoke(cli.app, ["fetch"])
    assert r.exit_code == 0 and "greenhouse:anthropic" in r.stdout and "ok" in r.stdout
    s = runner.invoke(cli.app, ["status"])
    assert s.exit_code == 0
    assert "greenhouse:anthropic" in s.stdout and "2026-08-18T06:00:00Z" in s.stdout
    sj = runner.invoke(cli.app, ["status", "--json"])
    rows = json.loads(sj.stdout)["boards"]
    assert {row["board"] for row in rows} == {"greenhouse:anthropic", "lever:palantir"}
    assert all(row["last_transport"] == "ok" for row in rows)


def test_status_marks_boards_never_fetched(env: Path) -> None:
    sj = runner.invoke(cli.app, ["status", "--json"])
    rows = json.loads(sj.stdout)["boards"]
    assert all(row["last_attempt"] is None for row in rows)


def test_archive_ls(env: Path) -> None:
    runner.invoke(cli.app, ["fetch"])
    r = runner.invoke(cli.app, ["archive", "ls", "--board", "lever:palantir", "--json"])
    items = json.loads(r.stdout)
    assert len(items) == 1 and items[0]["board"] == "lever:palantir"
    assert items[0]["attempt_id"].startswith("attempts/lever/palantir/2026/08/18T060000Z")


def test_registry_check_ok_and_bad(env: Path, tmp_path: Path) -> None:
    ok = runner.invoke(cli.app, ["registry", "check"])
    assert ok.exit_code == 0 and "2 boards" in ok.stdout
    (tmp_path / "companies.toml").write_text('[[boards]]\ncompany="X"\nsource="nope"\nboard="x"\n')
    bad = runner.invoke(cli.app, ["registry", "check"])
    assert bad.exit_code == 2 and "unknown source" in bad.stdout


def test_missing_archive_url_is_systemic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOB_HUNTER_ARCHIVE_URL", raising=False)
    r = runner.invoke(cli.app, ["status"])
    assert r.exit_code == 2 and "JOB_HUNTER_ARCHIVE_URL" in r.stdout


def test_fetch_all_boards_failed_is_systemic(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    monkeypatch.setattr(cli, "_make_fetcher", lambda: Fetcher(
        httpx.Client(transport=httpx.MockTransport(down)), sleep=lambda s: None))
    r = runner.invoke(cli.app, ["fetch"])
    assert r.exit_code == 2


def test_fetch_board_option_is_validated(env: Path) -> None:
    r = runner.invoke(cli.app, ["fetch", "--board", "anthropic"])
    assert r.exit_code == 2 and "source:board" in r.stdout


def test_fetch_unregistered_board_is_systemic(env: Path) -> None:
    r = runner.invoke(cli.app, ["fetch", "--board", "greenhouse:not-registered", "--dry-run"])
    assert r.exit_code == 2 and "not-registered" in r.stdout


def test_fetch_all_envelope_failures_is_systemic(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def html(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>maintenance</html>")

    monkeypatch.setattr(cli, "_make_fetcher", lambda: Fetcher(
        httpx.Client(transport=httpx.MockTransport(html)), sleep=lambda s: None))
    r = runner.invoke(cli.app, ["fetch", "--json"])
    assert r.exit_code == 2
    assert json.loads(r.stdout)["counts"]["envelope_error"] == 2


def test_fetch_requires_database_url_and_ingest_command(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JOB_HUNTER_DATABASE_URL", raising=False)
    r = runner.invoke(cli.app, ["fetch"])
    assert r.exit_code == 2 and "JOB_HUNTER_DATABASE_URL" in r.stdout
    monkeypatch.setenv("JOB_HUNTER_DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none")
    r = runner.invoke(cli.app, ["fetch", "--json"])
    assert r.exit_code == 2
    assert json.loads(r.stdout)["db_error"]
    r = runner.invoke(cli.app, ["ingest"])
    assert r.exit_code == 2 and "database error" in r.stdout


def test_report_and_registry_list_and_rebuild(env: Path) -> None:
    assert runner.invoke(cli.app, ["fetch"]).exit_code == 0
    r = runner.invoke(cli.app, ["report", "--since", "1d", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert data["counts"]["opened"] == 1 and data["events"][0]["kind"] == "opened"
    r = runner.invoke(cli.app, ["registry", "list", "--json"])
    assert r.exit_code == 0
    assert {row["board"] for row in json.loads(r.stdout)} == {
        "greenhouse:anthropic", "lever:palantir"
    }
    r = runner.invoke(cli.app, ["rebuild", "--json"])
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["swapped"] is True
    r = runner.invoke(cli.app, ["status", "--json"])
    rows = {row["board"]: row for row in json.loads(r.stdout)["boards"]}
    assert rows["greenhouse:anthropic"]["health"] == "ok"
    assert rows["greenhouse:anthropic"]["open"] == 1


def test_report_since_parsing() -> None:
    from jobhunter.cli import _parse_since

    assert _parse_since("24h").total_seconds() == 86400
    assert _parse_since("2d").total_seconds() == 172800
    assert _parse_since("30m").total_seconds() == 1800
    with pytest.raises(typer.BadParameter):
        _parse_since("soon")


def test_lock_held_branches_honour_json(env: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    from jobhunter.store import db as _db

    assert _db.try_lock(pg)
    try:
        for args in (["ingest", "--json"], ["rebuild", "--json"], ["fetch", "--json"]):
            r = runner.invoke(cli.app, args)
            assert r.exit_code == 0, (args, r.stdout)
            data = json.loads(r.stdout)
            assert data.get("lock_held") is True
    finally:
        _db.unlock(pg)


def test_db_version_on_half_created_schema_is_systemic(
    env: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    schema = pg.execute("SELECT current_schema() AS s").fetchone()["s"]
    pg.execute("DROP TABLE schema_meta")
    pg.commit()
    r = runner.invoke(cli.app, ["db", "version", "--json"])
    assert r.exit_code == 2
    assert json.loads(r.stdout)["db"] is None
    assert schema  # silence unused warning


def test_ingest_exits_2_on_gap_manifests(
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
    r = runner.invoke(cli.app, ["ingest", "--json"])
    assert r.exit_code == 2
    data = json.loads(r.stdout)
    assert len(data["gaps"]) == 1 and "rebuild" in data["hint"]


def test_status_reports_db_size(env: Path) -> None:
    runner.invoke(cli.app, ["fetch"])
    sj = runner.invoke(cli.app, ["status", "--json"])
    data = json.loads(sj.stdout)
    assert isinstance(data["db_size_bytes"], int) and data["db_size_bytes"] > 0
    sh = runner.invoke(cli.app, ["status"])
    assert sh.exit_code == 0 and "db size" in sh.stdout


def test_verify_pass_and_fail(tmp_path: Path) -> None:
    from tests.l2.conftest import DOC_MD, minimal_record

    doc = tmp_path / "doc.md"
    doc.write_text(DOC_MD, encoding="utf-8")
    good = tmp_path / "good.json"
    good.write_text(json.dumps(minimal_record()), encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(good), str(doc), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "pass"

    bad_record = minimal_record()
    bad_record["demand_profile"]["areas"][0]["claims"][0]["quote"]["text"] = "fabricated"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_record), encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(bad), str(doc)])
    assert result.exit_code == 1, result.output
    assert "text_mismatch" in result.stdout
    assert "line " in result.stdout  # derived line:col shown for span findings


def test_verify_systemic(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("x", encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(tmp_path / "missing.json"), str(doc)])
    assert result.exit_code == 2


def test_verify_systemic_non_dict_and_non_utf8(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("x", encoding="utf-8")
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2]", encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(arr), str(doc)])
    assert result.exit_code == 2, result.output

    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00")
    good = tmp_path / "good.json"
    good.write_text("{}", encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(good), str(bad)])
    assert result.exit_code == 2, result.output


def test_verify_human_output_shows_mismatch_diagnostics(tmp_path: Path) -> None:
    from tests.l2.conftest import DOC_MD, minimal_record

    doc = tmp_path / "doc.md"
    doc.write_text(DOC_MD, encoding="utf-8")
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["claims"][1]["quote"]["text"] = "0-2 YOE preferrd"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(rec), encoding="utf-8")
    result = runner.invoke(cli.app, ["verify", str(bad), str(doc)])
    assert result.exit_code == 1
    assert "expected:" in result.stdout and "found:" in result.stdout
    assert "longest matching prefix:" in result.stdout


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

    r = runner.invoke(cli.app, ["extract", "run", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["validated"] == 1

    r = runner.invoke(cli.app, ["extract", "review", "list", "--json"])
    assert json.loads(r.stdout)["inbox"] == []

    r = runner.invoke(cli.app, ["extract", "review", "flag", DH, "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["status"] == "needs_review"

    r = runner.invoke(cli.app, ["extract", "review", "list", "--json"])
    inbox = json.loads(r.stdout)["inbox"]
    assert len(inbox) == 1 and inbox[0]["document_hash"] == DH

    r = runner.invoke(cli.app, ["extract", "review", "show", DH])
    assert r.exit_code == 0 and "needs_review" in r.stdout and "a1 ok" in r.stdout

    r = runner.invoke(cli.app, ["extract", "review", "accept", DH, "--json"])
    assert json.loads(r.stdout)["status"] == "validated"

    r = runner.invoke(cli.app, ["verify", DH, "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["status"] == "pass"

    r = runner.invoke(cli.app, ["status", "--json"])
    assert r.exit_code == 0, r.output
    block = json.loads(r.stdout).get("extraction")
    assert block and block["by_status"] == {"validated": 1}
    assert block["observed_models_7d"] == ["z-ai/glm-5.2:free"]

    r = runner.invoke(cli.app, ["extract", "rebuild", "--json"])
    assert r.exit_code == 0, r.output
    counts = json.loads(r.stdout)
    assert counts["attempts"] == 1 and counts["reviews"] == 2


def test_extract_run_requires_l2_config(xenv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOB_HUNTER_L2_BASE_URL")
    r = runner.invoke(cli.app, ["extract", "run"])
    assert r.exit_code == 2 and "JOB_HUNTER_L2" in r.stdout


def test_reject_requires_note(xenv: Path) -> None:
    from tests.l2.test_runner import DH

    runner.invoke(cli.app, ["extract", "run"])
    r = runner.invoke(cli.app, ["extract", "review", "reject", DH])
    assert r.exit_code != 0  # typer enforces the missing --note
