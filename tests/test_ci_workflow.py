"""The scheduled `fetch` workflow, exercised the way GitHub runs it.

Since 2026-09-11 the workflow is collection-only (owner instruction):
`sync --no-extract` archives ATS payloads and advances the store, and L2
never runs in CI — extraction is local (docs/runbooks/2026-09-09-local-
codex-drain.md). These tests pin that contract: the step body is extracted
from the YAML and run under `bash -eo pipefail` (the workflow's
`defaults.run.shell`) against a stub `uv`, rather than trusted by reading.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "fetch.yml"

needs_shell = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the step body needs bash (present on ubuntu-latest runners)",
)


def _workflow() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    loaded: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _workflow()["jobs"]["fetch"]["steps"]
    return steps


def _step(name: str) -> dict[str, Any]:
    return next(s for s in _steps() if s.get("name") == name)


def _run_sync_step(tmp_path: Path, stdout: str, code: int) -> subprocess.CompletedProcess[str]:
    """Run the `sync` step with a stub `uv` printing `stdout` and exiting `code`;
    the stub also writes its argv to argv.txt for flag assertions."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(
        f'#!/usr/bin/env bash\necho "$@" >> argv.txt\n'
        f"cat <<'PAYLOAD'\n{stdout}\nPAYLOAD\nexit {code}\n"
    )
    stub.chmod(0o755)
    body = tmp_path / "step.sh"
    body.write_text(_step("sync")["run"], encoding="utf-8")
    return subprocess.run(
        ["bash", "-eo", "pipefail", str(body)],
        cwd=tmp_path, text=True, capture_output=True, check=False,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )


@needs_shell
def test_sync_always_passes_no_extract(tmp_path: Path) -> None:
    """Collection-only is the contract, not a toggle: every invocation carries
    --no-extract, so L2 can never run in CI regardless of repo variables."""
    r = _run_sync_step(tmp_path, '{"ok": true}', 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--no-extract" in (tmp_path / "argv.txt").read_text()


@needs_shell
def test_collection_failure_fails_the_run(tmp_path: Path) -> None:
    """Collection is irreplaceable — a sync failure must go red, and pipefail
    must carry it through the `tee` (an unspecified shell would swallow it)."""
    assert _run_sync_step(tmp_path, '{"ok": false}', 6).returncode == 6


@needs_shell
def test_summary_is_captured_for_the_artifact(tmp_path: Path) -> None:
    _run_sync_step(tmp_path, '{"ok": true}', 0)
    assert (tmp_path / "summary.json").read_text().strip() == '{"ok": true}'


def test_no_l2_configuration_anywhere() -> None:
    """The off-switch history (JOB_HUNTER_L2_MAX_DOCS=0 failing Settings before
    collection, run 34412983679) is why no L2 knob may even appear: nothing to
    misconfigure, nothing to fail collection over."""
    for step in _steps():
        for key in (step.get("env") or {}):
            assert not key.startswith("JOB_HUNTER_L2_"), (step.get("name"), key)
        assert "extract" not in str(step.get("run", "")).replace("--no-extract", "")


def test_the_schedule_exists() -> None:
    """Collection rides a cron again (2026-09-11): fetch-only was the condition
    for re-enabling schedules after the L2 burn."""
    wf = _workflow()
    triggers = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on:` key as True
    assert "schedule" in triggers


def test_keepalive_fires_on_failed_scheduled_runs() -> None:
    """Without a status function GitHub ANDs in success(), so a red run would let
    the 60-day idle rule silence the schedule — exactly when it must not."""
    assert _step("keepalive")["if"] == "always() && github.event_name == 'schedule'"
