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
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from jobhunter.timeutil import iso, utcnow, utcnow_precise

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
    """A fake GitHub plus scripted `tar` / `openssl` / drain, recording every argv.

    The `gh` side is a small server model rather than a canned reply per call,
    because the driver's run-id resolution is only as good as what the server
    can hand it: `_runs` holds every run that exists, a dispatch adds one, and
    `gh run list` answers out of that state — newest first, honouring `--limit`
    and projecting exactly the `--json` fields asked for.

    `preexisting` is the age in seconds of each run that is already on the
    server before the driver starts — the operator's own manual dump, or the
    run a previous invocation left behind. `appear_delay` is how many `gh run
    list` calls pass before a dispatched run becomes visible (real dispatches
    lag by seconds, which is why the driver polls at all). `list_skew`
    backdates the stamps of dispatched runs to model a machine whose clock runs
    ahead of GitHub's.

    `docs` is the document count each successive queue dump comes back with
    (`repeat_last` keeps re-offering the final entry, which is how a pool that
    hands back the same documents forever behaves); `drain_codes` the exit code
    of each successive drain (the last repeats); `blobs_per_drain` how many
    attempt blobs a *successful* drain drops in the outbox (a throttled one
    writes none, so the upload-skip path is reachable). `attempted_per_drain`
    overrides the drain's reported `attempted` count — a drain that skips every
    document it was handed reports 0 and writes nothing.

    `run_id_base` separates the run ids of two driver invocations in one test —
    GitHub never re-issues an id, so neither may a fake. `dispatch_cap` fails a
    test loudly instead of hanging when the driver never terminates.
    """

    docs: Sequence[int] = ()
    drain_codes: Sequence[int] = (0,)
    blobs_per_drain: int = 0
    attempted_per_drain: int | None = None
    repeat_last: bool = False
    stale_first_list: bool = False
    preexisting: Sequence[float] = ()
    appear_delay: int = 0
    run_id_base: int = 4400
    list_skew: float = 0.0
    dispatch_cap: int = 0
    calls: list[Call] = field(default_factory=list)
    drain_calls: int = 0
    _pending: list[int] = field(default_factory=list)
    _runs: list[dict[str, Any]] = field(default_factory=list)
    _dispatched_ids: list[int] = field(default_factory=list)
    _next_id: int = 0
    _blob_no: int = 0
    _dispatched: int = 0
    _stale_pending: bool = False

    def __post_init__(self) -> None:
        self._pending = list(self.docs)
        self._next_id = self.run_id_base
        for age in self.preexisting:
            self._mint(age, delay=0)

    def _mint(self, age: float, *, delay: int) -> int:
        """Put one run on the fake server, stamped `age` seconds in the past."""
        self._next_id += 1
        self._runs.append({
            "databaseId": self._next_id,
            "createdAt": iso(utcnow_precise() - timedelta(seconds=age)),
            "_delay": delay,
        })
        return self._next_id

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
        if call.kind.startswith("dispatch:"):
            self._dispatched += 1
            if self.dispatch_cap and self._dispatched > self.dispatch_cap:
                raise AssertionError(
                    f"driver still dispatching after {self.dispatch_cap} workflow runs"
                )
            self._dispatched_ids.append(self._mint(self.list_skew, delay=self.appear_delay))
            if self.stale_first_list and self._dispatched == 1:
                self._stale_pending = True
        if call.kind == "list":
            return self._list(argv)
        if call.kind == "download":
            return self._download(argv)
        if call.kind == "drain":
            return self._drain(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    # scripted responses ---------------------------------------------------
    def _list(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        limit = int(argv[argv.index("--limit") + 1])
        fields = argv[argv.index("--json") + 1].split(",")
        visible = [r for r in self._runs if r["_delay"] <= 0]
        for row in self._runs:
            if row["_delay"] > 0:
                row["_delay"] -= 1
        if self._stale_pending:
            # a run from a previous day surfacing in the listing: the driver
            # must refuse it on its stamp alone and poll on
            self._stale_pending = False
            visible = [{"databaseId": 1, "createdAt": "2026-01-01T00:00:00Z"}]
        newest = sorted(visible, key=lambda r: (r["createdAt"], r["databaseId"]), reverse=True)
        rows = [{f: r[f] for f in fields} for r in newest[:limit]]
        return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

    def _download(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if self._pending:
            n = self._pending[0] if self.repeat_last and len(self._pending) == 1 \
                else self._pending.pop(0)
        else:
            n = 0
        run_id = int(argv[3])
        target = Path(argv[argv.index("--dir") + 1]) / f"queue-{run_id}"
        target.mkdir(parents=True, exist_ok=True)
        rows = [
            json.dumps({"document_hash": f"{run_id:04x}{i:060x}", "markdown": "# job",
                        "next_attempt_no": 1})
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
        attempted = written if self.attempted_per_drain is None else self.attempted_per_drain
        lines = [json.dumps({"doc": f"{n:012x}", "disposition": "ok"}) for n in range(written)]
        lines.append(json.dumps({"run_id": "xlocal-1", "attempted": attempted,
                                 "counts": {"ok": written} if written else {}}))
        return subprocess.CompletedProcess(argv, code, "".join(ln + "\n" for ln in lines), "")

    # assertions helpers ---------------------------------------------------
    @property
    def kinds(self) -> list[str]:
        return [c.kind for c in self.calls]

    def of_kind(self, kind: str) -> list[Call]:
        return [c for c in self.calls if c.kind == kind]

    @property
    def watched(self) -> list[int]:
        """The run ids the driver actually waited on, in order."""
        return [int(c.argv[3]) for c in self.of_kind("watch")]

    @property
    def dispatched_ids(self) -> list[int]:
        """The run ids the driver's own dispatches created, in order."""
        return list(self._dispatched_ids)

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


def _drained_queue(call: Call) -> Path:
    """The queue file a drain invocation was actually handed."""
    return Path(next(a for a in call.argv if a.endswith("queue.jsonl")))


def _tar_members(call: Call) -> list[str]:
    """The exact members a tar invocation ships, read from its `-T` list file."""
    listed = Path(call.argv[call.argv.index("-T") + 1])
    return [ln for ln in listed.read_text(encoding="utf-8").splitlines() if ln.strip()]


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
    # a rate-limited cycle attempts nothing by definition; the log must not
    # report that as the pool having nothing left
    assert line["note"] == "throttled"


def test_an_outbox_with_nothing_unshipped_skips_upload(tmp_path: Path) -> None:
    """A cycle with nothing left to ship must not tar, encrypt, or ingest.

    The outbox starts empty here, so "gained no new blobs" and "holds nothing
    unshipped" are the same condition; the two come apart in
    `test_no_new_blobs_still_ships_what_never_shipped`, which pins which of
    them the gate is actually on.
    """
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


def test_no_new_blobs_still_ships_what_never_shipped(tmp_path: Path) -> None:
    """The gate is "nothing unshipped", not "nothing new this cycle".

    A deliberate widening of the contract's clause 4: a cycle that writes
    nothing new still ships blobs an earlier interrupted cycle left behind,
    because the alternative strands attempts in the outbox until some later
    cycle happens to write. It can only over-ship, and the ingest skips keys
    already in the archive.
    """
    outbox = tmp_path / "outbox"
    left_behind = ["extractions/attempts/2026/09/09/x.json.gz",
                   "extractions/attempts/2026/09/09/y.json.gz"]
    for key in left_behind:
        (outbox / key).parent.mkdir(parents=True, exist_ok=True)
        (outbox / key).write_bytes(b"\x1f\x8b")

    fake = FakeRun(docs=[5], drain_codes=[0], blobs_per_drain=0)
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "queue", "--cycles", "1"],
        run=fake, sleep=lambda _s: None,
    )
    assert code == 0
    assert _tar_members(fake.of_kind("tar")[0]) == sorted("outbox/" + k for k in left_behind)
    line = _log_lines(tmp_path)[-1]
    assert line["blobs"] == 0  # the cycle itself gained nothing
    assert line["shipped"] == 2  # and shipped what was still pending anyway


def test_full_cycle_orders_the_pipeline(tmp_path: Path) -> None:
    """dump → drain → tar → encrypt → release → ingest, in that order."""
    fake = FakeRun(docs=[5], drain_codes=[0], blobs_per_drain=3)
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "queue", "--cycles", "1"],
        run=fake, sleep=lambda _s: None,
    )
    assert code == 0
    assert fake.kinds == [
        "list", "dispatch:extract-queue-dump.yml", "list", "watch", "download",
        "drain",
        "tar", "openssl", "release", "list", "dispatch:outbox-ingest.yml", "list", "watch",
    ]  # each dispatch is preceded by the snapshot that makes its run id exact

    dump = fake.of_kind("dispatch:extract-queue-dump.yml")[0]
    assert "count=5" in dump.argv and "pool=queue" in dump.argv
    drain = fake.of_kind("drain")[0]
    assert drain.argv[:3] == ["uv", "run", "python"]
    assert drain.argv[-2:] == ["--max-docs", "5"]

    # macOS AppleDouble suppression is an invariant of the transport
    tar = fake.of_kind("tar")[0]
    assert (tar.env or {}).get("COPYFILE_DISABLE") == "1"
    # the ingest untars `outbox/...`, so every member must carry that prefix
    assert tar.argv[tar.argv.index("-C") + 1] == str(tmp_path)
    assert [m.split("/")[0] for m in _tar_members(tar)] == ["outbox"] * 3

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
    # the pre-dispatch snapshot, then the stale row forcing a second poll
    assert len(fake.of_kind("list")) == 3
    assert fake.watched == fake.dispatched_ids  # never the stale id 1
    assert sleeps  # the poll waited between listings


def test_a_run_created_just_before_the_dispatch_is_never_adopted(tmp_path: Path) -> None:
    """A run seconds older than the dispatch is a foreign run, not this cycle's.

    The clock-skew slack means "created 30 seconds ago" reads as fresh, and at
    the first resolve of a process there is no adopted-id history to fall back
    on — exactly the state every restart after a throttle stop or a Ctrl-C
    begins in, and the state the operator's own manual `gh workflow run
    extract-queue-dump.yml` lands in. Only the pre-dispatch snapshot separates
    the two: adopting the older run would watch and download someone else's
    dump while the run this driver paid for went unwatched.
    """
    fake = FakeRun(docs=[3], preexisting=[30.0], appear_delay=1, drain_codes=[0],
                   blobs_per_drain=1)
    sleeps: list[float] = []
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "queue", "--cycles", "1"],
        run=fake, sleep=sleeps.append,
    )
    assert code == 0
    stale, dump_run = fake.run_id_base + 1, fake.dispatched_ids[0]
    assert stale not in fake.watched
    assert fake.watched[0] == dump_run
    assert [c.argv[3] for c in fake.of_kind("download")] == [str(dump_run)]
    assert _log_lines(tmp_path)[-1]["dump_run"] == dump_run


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


def test_reused_work_dir_drains_this_cycle_s_dump(tmp_path: Path) -> None:
    """Resuming into the same --work-dir must drain the dump just paid for.

    Cycle numbering restarts with every invocation, so a second run reuses
    `cycle-001`; the queue it drains has to be the new run's artifact, never
    the one left behind by the first.
    """
    first = FakeRun(docs=[3], drain_codes=[0], blobs_per_drain=1, run_id_base=7000)
    assert driver.main(
        _fixture(tmp_path) + ["--pool", "quarantined", "--cycles", "1"],
        run=first, sleep=lambda _s: None,
    ) == 0

    second = FakeRun(docs=[9], drain_codes=[0], blobs_per_drain=1, run_id_base=8000)
    assert driver.main(
        _fixture(tmp_path) + ["--pool", "quarantined", "--cycles", "1"],
        run=second, sleep=lambda _s: None,
    ) == 0

    queue = _drained_queue(second.of_kind("drain")[0])
    drained = [ln for ln in queue.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(drained) == 9  # the fresh dump, not the 3-document leftover
    line = _log_lines(tmp_path)[-1]
    assert line["dumped"] == len(drained)  # what the log claims is what was drained


def test_no_progress_cycle_stops_the_loop(tmp_path: Path) -> None:
    """A pool that re-offers documents the drain skips must not loop forever.

    The quarantined pool hands back the same documents until their rows move,
    and the drain refuses any document whose blob is already in the outbox: the
    dump is non-empty, yet nothing is attempted and nothing is written. That is
    the end of the work, not a reason to buy another CI run.
    """
    fake = FakeRun(docs=[5], repeat_last=True, drain_codes=[0], blobs_per_drain=0,
                   attempted_per_drain=0, dispatch_cap=4)
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "quarantined", "--cycles", "0"],
        run=fake, sleep=lambda _s: None,
    )
    assert code == 0
    assert fake.drain_calls == 1
    assert fake.dispatch_pools == ["quarantined"]
    line = _log_lines(tmp_path)[-1]
    assert line["dumped"] == 5
    assert line["drained"] == 0
    assert line["blobs"] == 0
    assert line["note"] == "no progress"


def test_no_progress_switches_pool_under_auto(tmp_path: Path) -> None:
    """Under --pool auto a stuck queue falls through to quarantined, as a dry one does."""
    fake = FakeRun(docs=[5], repeat_last=True, drain_codes=[0], blobs_per_drain=0,
                   attempted_per_drain=0, dispatch_cap=4)
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "auto", "--cycles", "0"],
        run=fake, sleep=lambda _s: None,
    )
    assert code == 0
    assert fake.dispatch_pools == ["queue", "quarantined"]
    assert [ln["note"] for ln in _log_lines(tmp_path)] == ["no progress", "no progress"]


def test_small_clock_skew_still_adopts_the_new_run(tmp_path: Path) -> None:
    """GitHub's stamp lagging the local clock by seconds is not a stale run."""
    fake = FakeRun(docs=[0], list_skew=3.0)
    sleeps: list[float] = []
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "queue", "--cycles", "1"],
        run=fake, sleep=sleeps.append,
    )
    assert code == 0
    # the snapshot, then the resolve that adopts on its first listing: no polling
    assert len(fake.of_kind("list")) == 2
    assert fake.watched == fake.dispatched_ids
    assert sleeps == []


def test_resolve_run_refuses_an_already_adopted_run() -> None:
    """The skew tolerance must not let a cycle re-adopt the previous cycle's run."""
    ids = iter([900, 900, 901])

    def fake_run(
        cmd: Sequence[Any], *, capture: bool = True, cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        rows = [{"databaseId": next(ids), "createdAt": iso(utcnow_precise())}]
        return subprocess.CompletedProcess([str(c) for c in cmd], 0, json.dumps(rows), "")

    seen = {900}
    sleeps: list[float] = []
    got = driver.resolve_run(
        "extract-queue-dump.yml", utcnow(), run=fake_run, sleep=sleeps.append, seen=seen,
    )
    assert got == 901
    assert seen == {900, 901}  # adopting records it, so no later cycle can reuse it
    assert len(sleeps) == 2  # the two repeats of the known id forced polls


def test_only_unshipped_blobs_are_tarred(tmp_path: Path) -> None:
    """Transport stays linear: each blob ships once, not once per later cycle."""
    fake = FakeRun(docs=[5, 5], drain_codes=[0], blobs_per_drain=2)
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "quarantined", "--cycles", "2"],
        run=fake, sleep=lambda _s: None,
    )
    assert code == 0
    shipped = [_tar_members(t) for t in fake.of_kind("tar")]
    assert [len(m) for m in shipped] == [2, 2]
    assert set(shipped[0]).isdisjoint(shipped[1])
    assert not any(m.endswith(("cycle-log.jsonl", driver.SHIPPED_LEDGER)) for m in sum(shipped, []))
    assert [ln["shipped"] for ln in _log_lines(tmp_path)] == [2, 2]


def test_blobs_left_unshipped_by_a_failed_ingest_ship_next_cycle(tmp_path: Path) -> None:
    """An interrupted upload must not strand attempts: the ledger only records green ingests."""
    outbox = tmp_path / "outbox"
    orphan = outbox / "extractions/attempts/2026/09/09/orphan.json.gz"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"\x1f\x8b")

    fake = FakeRun(docs=[5], drain_codes=[0], blobs_per_drain=1)
    code = driver.main(
        _fixture(tmp_path) + ["--pool", "quarantined", "--cycles", "1"],
        run=fake, sleep=lambda _s: None,
    )
    assert code == 0
    members = _tar_members(fake.of_kind("tar")[0])
    assert "outbox/" + orphan.relative_to(outbox).as_posix() in members
    assert len(members) == 2  # the orphan plus this cycle's blob


def test_the_shipped_ledger_is_replaced_never_truncated(tmp_path: Path) -> None:
    """The ledger write is atomic: a torn one would re-ship the whole outbox.

    `shipped()` reads unparseable JSON as "nothing shipped", so a truncate-in-
    place interrupted by the operator's Ctrl-C — the documented way to stop an
    unattended run — would silently put every accumulated blob back on the wire.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    driver.record_shipped(outbox, ["a.json.gz"])
    ledger = outbox / driver.SHIPPED_LEDGER
    first = ledger.stat().st_ino

    driver.record_shipped(outbox, ["b.json.gz"])
    assert ledger.stat().st_ino != first  # a fresh file replaced it, byte-complete
    assert driver.shipped(outbox) == {"a.json.gz", "b.json.gz"}
    assert [p.name for p in outbox.iterdir()] == [driver.SHIPPED_LEDGER]  # no temp left


def test_a_torn_ledger_reships_rather_than_stranding(tmp_path: Path) -> None:
    """Whatever else goes wrong, an unreadable ledger never loses an attempt."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / driver.SHIPPED_LEDGER).write_text('{"shipped": ["a.json.g', encoding="utf-8")
    assert driver.shipped(outbox) == set()


@pytest.mark.skipif(shutil.which("tar") is None, reason="tar is the real transport here")
def test_tar_ships_exactly_the_listed_members(tmp_path: Path) -> None:
    """Against the real tar binary: the listed members, with no AppleDouble sidecars.

    The rest of the suite fakes `run`, so this is the one place the argv the
    driver builds meets a real tar — the form that ships a subset (`-C parent
    -T list`) and the `COPYFILE_DISABLE=1` that keeps macOS from adding a `._*`
    member per file, as it did to 114 archive keys once.
    """
    outbox = tmp_path / "outbox"
    keys = ["extractions/attempts/2026/09/10/a.json.gz",
            "extractions/attempts/2026/09/10/b.json.gz"]
    for key in [*keys, "extractions/attempts/2026/09/09/old.json.gz"]:
        path = outbox / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x1f\x8b\x08")
    (outbox / "cycle-log.jsonl").write_text("{}\n", encoding="utf-8")
    (outbox / driver.SHIPPED_LEDGER).write_text("{}\n", encoding="utf-8")

    listing = driver.member_list(outbox, keys, tmp_path / "members.txt")
    tgz = tmp_path / "outbox.tgz"
    driver.tar_outbox(outbox, listing, tgz, run=driver.shell)
    with tarfile.open(tgz) as tf:
        packed = sorted(m.name for m in tf.getmembers() if m.isfile())
    assert packed == sorted("outbox/" + k for k in keys)
