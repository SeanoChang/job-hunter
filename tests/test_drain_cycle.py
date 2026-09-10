"""The unattended drain-cycle driver (`scripts/codex_drain_cycle.py`).

Every shell-out the driver makes goes through its injectable `run`, so the
suite exercises the real orchestration — dispatch, run-id resolution, watch,
download, drain, tar/encrypt/release/ingest, the cycle log — with no `gh`, no
codex, and no network. `sleep` is injected too: the throttle backoff is
asserted as numbers, never waited on.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunter.timeutil import iso, utcnow_precise

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "codex_drain_cycle.py"


def _load_driver() -> Any:
    """Import the script by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("codex_drain_cycle", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = _load_driver()


def kind_of(argv: Sequence[str]) -> str:
    """Classify one invocation by its argv alone — no driver internals."""
    argv = list(argv)
    if argv[:3] == ["gh", "workflow", "run"]:
        return f"dispatch:{argv[3]}"
    if argv[:3] == ["gh", "run", "list"]:
        return "list"
    if argv[:3] == ["gh", "run", "watch"]:
        return "watch"
    if argv[:3] == ["gh", "run", "download"]:
        return "download"
    if argv[:3] == ["gh", "release", "create"]:
        return "release"
    if argv[0] == "tar":
        return "tar"
    if argv[0] == "openssl":
        return "openssl"
    if any(a.endswith("local_codex_drain.py") for a in argv):
        return "drain"
    return f"?{argv[0]}"


@dataclass
class Call:
    argv: list[str]
    env: dict[str, str] | None = None
    cwd: Path | None = None
    capture: bool = True

    @property
    def kind(self) -> str:
        return kind_of(self.argv)


@dataclass
class FakeRun:
    """Scripted `gh` / `tar` / `openssl` / drain doubles that record every argv.

    `docs` is the document count each successive queue dump comes back with;
    `drain_codes` the exit code of each successive drain (the last repeats);
    `blobs_per_drain` how many attempt blobs a *successful* drain drops in the
    outbox (a throttled one writes none, so the upload-skip path is reachable).
    """

    docs: Sequence[int] = ()
    drain_codes: Sequence[int] = (0,)
    blobs_per_drain: int = 0
    stale_first_list: bool = False
    calls: list[Call] = field(default_factory=list)
    drain_calls: int = 0
    _pending: list[int] = field(default_factory=list)
    _run_id: int = 4400
    _listed: int = 0
    _blob_no: int = 0

    def __post_init__(self) -> None:
        self._pending = list(self.docs)

    # the injected callable ------------------------------------------------
    def __call__(
        self,
        cmd: Sequence[Any],
        *,
        capture: bool = True,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = [str(c) for c in cmd]
        call = Call(argv, env, cwd, capture)
        self.calls.append(call)
        if call.kind == "list":
            return self._list(argv)
        if call.kind == "download":
            return self._download(argv)
        if call.kind == "drain":
            return self._drain(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    # scripted responses ---------------------------------------------------
    def _list(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self._listed += 1
        if self.stale_first_list and self._listed == 1:
            # a run from a previous day: the driver must refuse it and poll on
            rows = [{"databaseId": 1, "createdAt": "2026-01-01T00:00:00Z"}]
        else:
            self._run_id += 1
            rows = [{"databaseId": self._run_id, "createdAt": iso(utcnow_precise())}]
        return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

    def _download(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        n = self._pending.pop(0) if self._pending else 0
        target = Path(argv[argv.index("--dir") + 1]) / f"queue-{self._run_id}"
        target.mkdir(parents=True, exist_ok=True)
        rows = [
            json.dumps({"document_hash": f"{i:064x}", "markdown": "# job", "next_attempt_no": 1})
            for i in range(n)
        ]
        (target / "queue.jsonl").write_text("".join(r + "\n" for r in rows), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _drain(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        i = argv.index(next(a for a in argv if a.endswith("local_codex_drain.py")))
        outbox = Path(argv[i + 2])
        code = self.drain_codes[min(self.drain_calls, len(self.drain_codes) - 1)]
        self.drain_calls += 1
        written = self.blobs_per_drain if code == 0 else 0
        for _ in range(written):
            self._blob_no += 1
            blob = outbox / "extractions/attempts/2026/09/10" / f"a{self._blob_no}.json.gz"
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(b"\x1f\x8b")
        lines = [json.dumps({"doc": f"{n:012x}", "disposition": "ok"}) for n in range(written)]
        lines.append(json.dumps({"run_id": "xlocal-1", "attempted": written,
                                 "counts": {"ok": written} if written else {}}))
        return subprocess.CompletedProcess(argv, code, "".join(ln + "\n" for ln in lines), "")

    # assertions helpers ---------------------------------------------------
    @property
    def kinds(self) -> list[str]:
        return [c.kind for c in self.calls]

    def of_kind(self, kind: str) -> list[Call]:
        return [c for c in self.calls if c.kind == kind]

    @property
    def dispatch_pools(self) -> list[str]:
        return [
            arg.split("=", 1)[1]
            for c in self.of_kind("dispatch:extract-queue-dump.yml")
            for arg in c.argv
            if arg.startswith("pool=")
        ]


def _fixture(tmp_path: Path) -> list[str]:
    """A repo-free workspace: an outbox, a work dir, a stand-in key file."""
    (tmp_path / "outbox").mkdir(exist_ok=True)
    key = tmp_path / "outbox.key"
    key.write_text("stand-in-not-a-secret\n", encoding="utf-8")
    return [
        "--outbox", str(tmp_path / "outbox"),
        "--work-dir", str(tmp_path / "work"),
        "--key", str(key),
        "--batch", "5",
    ]


def _log_lines(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "outbox" / "cycle-log.jsonl"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_auto_pool_switches_then_exits(tmp_path: Path) -> None:
    """queue comes back empty → switch to quarantined; both empty → exit 0."""
    fake = FakeRun(docs=[0, 0])
    sleeps: list[float] = []
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "auto", "--cycles", "0"],
        run=fake, sleep=sleeps.append,
    )
    assert code == 0
    assert fake.dispatch_pools == ["queue", "quarantined"]
    assert "drain" not in fake.kinds  # a dry pool never pays for a codex call
    assert "release" not in fake.kinds
    lines = _log_lines(tmp_path)
    assert [ln["pool"] for ln in lines] == ["queue", "quarantined"]
    assert [ln["dumped"] for ln in lines] == [0, 0]


def test_throttle_backoff_and_budget(tmp_path: Path) -> None:
    """exit 3 → wait --throttle-wait minutes and retry; budget spent → exit 3."""
    fake = FakeRun(docs=[5], drain_codes=[3])
    sleeps: list[float] = []
    code = driver.main(
        _fixture(tmp_path)
        + ["--pool", "queue", "--cycles", "1", "--throttle-retries", "2", "--throttle-wait", "45"],
        run=fake, sleep=sleeps.append,
    )
    assert code == 3
    assert fake.drain_calls == 3  # the first call plus the two-retry budget
    assert [s for s in sleeps if s >= 60] == [2700.0, 2700.0]
    assert "tar" not in fake.kinds and "release" not in fake.kinds  # nothing new to ship
    line = _log_lines(tmp_path)[-1]
    assert line["throttled"] is True
    assert line["blobs"] == 0


def test_empty_outbox_delta_skips_upload(tmp_path: Path) -> None:
    """A cycle that produced no new blobs must not tar, encrypt, or ingest."""
    fake = FakeRun(docs=[5], drain_codes=[0], blobs_per_drain=0)
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "queue", "--cycles", "1"],
        run=fake, sleep=lambda _s: None,
    )
    assert code == 0
    assert fake.drain_calls == 1
    for absent in ("tar", "openssl", "release", "dispatch:outbox-ingest.yml"):
        assert absent not in fake.kinds
    line = _log_lines(tmp_path)[-1]
    assert line["blobs"] == 0
    assert line["ingest_run"] is None


def test_full_cycle_orders_the_pipeline(tmp_path: Path) -> None:
    """dump → drain → tar → encrypt → release → ingest, in that order."""
    fake = FakeRun(docs=[5], drain_codes=[0], blobs_per_drain=3)
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "queue", "--cycles", "1"],
        run=fake, sleep=lambda _s: None,
    )
    assert code == 0
    assert fake.kinds == [
        "dispatch:extract-queue-dump.yml", "list", "watch", "download",
        "drain",
        "tar", "openssl", "release", "dispatch:outbox-ingest.yml", "list", "watch",
    ]

    dump = fake.of_kind("dispatch:extract-queue-dump.yml")[0]
    assert "count=5" in dump.argv and "pool=queue" in dump.argv
    drain = fake.of_kind("drain")[0]
    assert drain.argv[:3] == ["uv", "run", "python"]
    assert drain.argv[-2:] == ["--max-docs", "5"]

    # macOS AppleDouble suppression is an invariant of the transport
    tar = fake.of_kind("tar")[0]
    assert (tar.env or {}).get("COPYFILE_DISABLE") == "1"
    # the ingest untars `outbox/...`, so the member prefix must be the outbox itself
    assert tar.argv[-2:] == ["-C", str(tmp_path)] or tar.argv[-1] == "outbox"

    # the key never travels as a value, only as a file reference
    openssl = fake.of_kind("openssl")[0]
    assert any(a.startswith("file:") and a.endswith("outbox.key") for a in openssl.argv)
    assert "stand-in-not-a-secret" not in " ".join(openssl.argv)

    release = fake.of_kind("release")[0]
    tag = release.argv[3]
    assert "--draft" in release.argv and tag.startswith("outbox-")
    ingest = fake.of_kind("dispatch:outbox-ingest.yml")[0]
    assert f"tag={tag}" in ingest.argv

    # no heredoc ever reaches a subprocess (they hang in this environment)
    assert not any("<<" in a for c in fake.calls for a in c.argv)

    line = _log_lines(tmp_path)[-1]
    assert line["cycle"] == 1
    assert line["pool"] == "queue"
    assert line["dumped"] == 5
    assert line["drained"] == 3
    assert line["counts"] == {"ok": 3}
    assert line["blobs"] == 3
    assert line["tag"] == tag
    assert line["ingest_run"] == int(fake.of_kind("watch")[-1].argv[3])


def test_stale_run_ids_are_refused(tmp_path: Path) -> None:
    """A run created before the dispatch is never adopted as this cycle's run."""
    fake = FakeRun(docs=[0], stale_first_list=True)
    sleeps: list[float] = []
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "queue", "--cycles", "1"],
        run=fake, sleep=sleeps.append,
    )
    assert code == 0
    assert len(fake.of_kind("list")) == 2  # the stale row forced another poll
    watched = fake.of_kind("watch")[0].argv[3]
    assert watched != "1"
    assert sleeps  # the poll waited between listings


def test_cycles_limit_stops_the_loop(tmp_path: Path) -> None:
    """--cycles N runs exactly N cycles even with work left in the pool."""
    fake = FakeRun(docs=[5, 5, 5], drain_codes=[0], blobs_per_drain=1)
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "quarantined", "--cycles", "2"],
        run=fake, sleep=lambda _s: None,
    )
    assert code == 0
    assert fake.drain_calls == 2
    assert fake.dispatch_pools == ["quarantined", "quarantined"]
    assert [ln["cycle"] for ln in _log_lines(tmp_path)] == [1, 2]
