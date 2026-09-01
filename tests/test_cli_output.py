"""The output contract: envelope shape, exit codes, TTY detection."""

import json

import pytest
import typer

from jobhunter.cli_output import Exit, emit, fail, use_json


def test_exit_codes_are_the_documented_table() -> None:
    assert [e.value for e in Exit] == [0, 1, 2, 3, 4, 5, 6]
    assert Exit.SYSTEMIC == 6 and Exit.USAGE == 2


def test_use_json_forced_modes() -> None:
    assert use_json("json") is True
    assert use_json("table") is False


def test_use_json_rejects_unknown_mode() -> None:
    with pytest.raises(typer.Exit) as e:
        use_json("yaml")
    assert e.value.exit_code == Exit.USAGE


def test_emit_json_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    emit([{"a": 1}], human="a", output="json", hint="next: q posting a")
    out = capsys.readouterr()
    body = json.loads(out.out)
    assert body["ok"] is True
    assert body["data"] == [{"a": 1}]
    assert body["meta"]["count"] == 1
    assert body["meta"]["truncated"] is False
    assert body["meta"]["hint"] == "next: q posting a"
    assert out.err == ""  # data stream stays pure


def test_emit_table_prints_human_and_hints_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    emit([{"a": 1}], human="one row", output="table", hint="try --full")
    out = capsys.readouterr()
    assert out.out == "one row\n"
    assert "try --full" in out.err


def test_fail_json_envelope_and_code(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as e:
        fail("not_found", "no document matches 'ab'", code=Exit.NOT_FOUND,
             hint="prefixes need >= 6 hex chars", output="json")
    assert e.value.exit_code == 4
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert body["error"]["kind"] == "not_found"
    assert body["error"]["hint"].startswith("prefixes")


def test_fail_table_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit):
        fail("config", "JOB_HUNTER_ARCHIVE_URL is required", code=Exit.CONFIG,
             valid=None, output="table")
    out = capsys.readouterr()
    assert out.out == ""
    assert "JOB_HUNTER_ARCHIVE_URL" in out.err
