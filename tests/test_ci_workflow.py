"""The scheduled `fetch` workflow, exercised the way GitHub runs it.

The hourly job's meaning of "failed" lives in the `sync` step's shell, not in
Python: `sync` exits 6 both when collection broke and when the extraction engine
stalled, and only the step tells those apart. So the step body is extracted from
the YAML and run under `bash -eo pipefail` (the workflow's `defaults.run.shell`)
against a stub `uv`, rather than trusted by reading.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "fetch.yml"

needs_shell = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="the step body needs bash and jq (both present on ubuntu-latest runners)",
)


def _steps() -> list[dict[str, Any]]:
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = workflow["jobs"]["fetch"]["steps"]
    return steps


def _step(name: str) -> dict[str, Any]:
    return next(s for s in _steps() if s.get("name") == name)


def _envelope(extract: dict[str, Any], *, gaps: list[str] | None = None) -> str:
    return json.dumps({
        "ok": True,
        "data": {"ingest": {"gaps": gaps or []}, "fetch": {"counts": {"ok": 1}},
                 "extract": extract},
        "meta": {"truncated": False},
    })


def _run_sync_step(tmp_path: Path, stdout: str, code: int) -> subprocess.CompletedProcess[str]:
    """Run the `sync` step with a stub `uv` printing `stdout` and exiting `code`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(f"#!/usr/bin/env bash\ncat <<'PAYLOAD'\n{stdout}\nPAYLOAD\nexit {code}\n")
    stub.chmod(0o755)
    body = tmp_path / "step.sh"
    body.write_text(_step("sync")["run"], encoding="utf-8")
    return subprocess.run(
        ["bash", "-eo", "pipefail", str(body)],
        cwd=tmp_path, text=True, capture_output=True, check=False,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
             "JOB_HUNTER_L2_API_KEY": "k", "EXTRACT_MAX_DOCS": ""},
    )


def _run_sync_step_recording(
    tmp_path: Path, argv_log: Path, *, stdout: str, code: int, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """`_run_sync_step`, but the stub `uv` also appends its argv to `argv_log` —
    for tests that must see which flags the guard actually passed, not just
    the exit code. `env` is merged over the minimal base (PATH + a blank
    EXTRACT_MAX_DOCS), same override shape as the inline env in `_run_sync_step`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(
        f'#!/usr/bin/env bash\necho "$@" >> "{argv_log}"\n'
        f"cat <<'PAYLOAD'\n{stdout}\nPAYLOAD\nexit {code}\n"
    )
    stub.chmod(0o755)
    body = tmp_path / "step.sh"
    body.write_text(_step("sync")["run"], encoding="utf-8")
    run_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "EXTRACT_MAX_DOCS": ""}
    run_env.update(env)
    return subprocess.run(
        ["bash", "-eo", "pipefail", str(body)],
        cwd=tmp_path, text=True, capture_output=True, check=False,
        env=run_env,
    )


@needs_shell
@pytest.mark.parametrize("extract", [
    {"validated": 0, "throttled": True, "breaker_abort": False},  # free-tier daily cap
    {"validated": 0, "throttled": False, "breaker_abort": True},  # 5 model rejections
])
def test_stalled_extraction_does_not_fail_the_collection_run(
    tmp_path: Path, extract: dict[str, Any]
) -> None:
    """Extraction is recomputable from the archive; collection is not. A stalled
    engine is a warning, so the run — and the keepalive after it — stays green."""
    r = _run_sync_step(tmp_path, _envelope(extract), 6)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "::warning::extraction stalled" in r.stdout
    assert json.loads((tmp_path / "summary.json").read_text())["data"]["extract"] == extract


@needs_shell
def test_collection_failure_still_fails_the_run(tmp_path: Path) -> None:
    """Ingest gaps mean the store is behind the archive: that must go red."""
    envelope = _envelope({"skipped_reason": "ingest gaps"}, gaps=["attempt-1"])
    assert _run_sync_step(tmp_path, envelope, 6).returncode == 6


@needs_shell
@pytest.mark.parametrize("stdout,code", [("not json at all", 6), ("", 3)])
def test_an_unreadable_summary_leaves_the_exit_code_alone(
    tmp_path: Path, stdout: str, code: int
) -> None:
    r = _run_sync_step(tmp_path, stdout, code)
    assert r.returncode == code, r.stdout + r.stderr


@needs_shell
def test_a_successful_sync_exits_zero(tmp_path: Path) -> None:
    envelope = _envelope({"validated": 3, "throttled": False, "breaker_abort": False})
    assert _run_sync_step(tmp_path, envelope, 0).returncode == 0


@needs_shell
def test_extract_toggle_off_passes_no_extract(tmp_path: Path) -> None:
    """JOB_HUNTER_EXTRACT_IN_FETCH="false" is the real off-switch: unlike
    JOB_HUNTER_L2_MAX_DOCS=0, it never reaches Settings validation because the
    guard routes it to --no-extract before job-hunter sync is even invoked."""
    argv_log = tmp_path / "argv.txt"
    proc = _run_sync_step_recording(
        tmp_path, argv_log, stdout=_envelope({"validated": 0}), code=0,
        env={"JOB_HUNTER_L2_API_KEY": "k", "JOB_HUNTER_EXTRACT_IN_FETCH": "false"},
    )
    assert proc.returncode == 0
    assert "--no-extract" in argv_log.read_text()


@needs_shell
def test_extract_toggle_unset_leaves_extraction_on_with_a_key_present(tmp_path: Path) -> None:
    """The variable defaults open: unset must never silently disable extraction."""
    argv_log = tmp_path / "argv.txt"
    proc = _run_sync_step_recording(
        tmp_path, argv_log, stdout=_envelope({"validated": 0}), code=0,
        env={"JOB_HUNTER_L2_API_KEY": "k"},
    )
    assert proc.returncode == 0
    assert "--no-extract" not in argv_log.read_text()


def test_keepalive_fires_on_failed_scheduled_runs() -> None:
    """Without a status function GitHub ANDs in success(), so a red run would let
    the 60-day idle rule silence the schedule — exactly when it must not."""
    assert _step("keepalive")["if"] == "always() && github.event_name == 'schedule'"
