import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
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
