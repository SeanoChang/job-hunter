"""Run the local codex drain unattended: dump → drain → encrypt → ingest, in a
loop, until the pools are dry or codex throttles past the retry budget.

This is the runbook loop (docs/runbooks/2026-09-09-local-codex-drain.md) with
the six hand-run commands folded into one process. Nothing about the transport
changes: CI dumps the queue as an artifact, `scripts/local_codex_drain.py`
produces archive-format attempt objects locally through codex-cli, and
`outbox-ingest.yml` writes them into R2 where the runner's catch-up scan
records and settles them. The driver only orchestrates processes — it never
stamps, rewrites, or even reads an attempt key; the drain script owns attempt
content, including its timestamps.

Usage:
    uv run python scripts/codex_drain_cycle.py --batch 100 --cycles 0 --pool auto

`--pool auto` starts on the never-extracted queue and falls through to the
quarantined retry pool when the queue comes back empty; when both come back
empty the loop exits 0. Exit 3 means codex stayed throttled through the whole
`--throttle-retries` budget — the outbox is still uploaded first, because
partial progress is real progress.

Every subprocess goes through the injectable `run` (default `shell`), so the
test suite drives the whole cycle without gh, codex, or a network; `sleep` is
injected for the same reason. No heredocs anywhere — they hang in this
environment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jobhunter.timeutil import iso, parse_iso, utcnow

REPO = Path(__file__).resolve().parents[1]
DRAIN_SCRIPT = REPO / "scripts" / "local_codex_drain.py"
DUMP_WORKFLOW = "extract-queue-dump.yml"
INGEST_WORKFLOW = "outbox-ingest.yml"
DEFAULT_KEY = Path.home() / ".config" / "job-hunter" / "outbox.key"
POLL_SECONDS = 5.0
POLL_TRIES = 60

Run = Callable[..., "subprocess.CompletedProcess[str]"]
Sleep = Callable[[float], None]


class CycleError(RuntimeError):
    """A cycle could not proceed; the message is the operator-facing reason."""


def shell(
    cmd: Sequence[Any],
    *,
    capture: bool = True,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """The default `run`: argv only, never a shell string, never a heredoc."""
    return subprocess.run(
        [str(c) for c in cmd], cwd=cwd, env=env, text=True, capture_output=capture, check=False
    )


@dataclass
class Dump:
    run_id: int
    queue: Path | None
    docs: int


@dataclass
class Upload:
    tag: str
    run_id: int
    blobs: int


def _detail(res: subprocess.CompletedProcess[str]) -> str:
    text = ((res.stderr or "") + (res.stdout or "")).strip()
    return text.splitlines()[-1] if text else ""


def _check(res: subprocess.CompletedProcess[str], what: str) -> subprocess.CompletedProcess[str]:
    if res.returncode != 0:
        raise CycleError(f"{what} failed (exit {res.returncode}): {_detail(res)}")
    return res


def resolve_run(workflow: str, after: datetime, *, run: Run, sleep: Sleep) -> int:
    """The id of the `workflow` run this dispatch created.

    A dispatch prints no id, so the run has to be looked up — and the lookup
    must never adopt a run that was already there. `after` is the (whole-second
    floored) instant just before the dispatch, so a run created in that same
    second still counts; anything older is somebody else's run and the poll
    continues.
    """
    for _ in range(POLL_TRIES):
        res = _check(
            run(["gh", "run", "list", f"--workflow={workflow}", "--limit", "1",
                 "--json", "databaseId,createdAt"]),
            "gh run list",
        )
        for row in json.loads(res.stdout or "[]"):
            if parse_iso(str(row["createdAt"])) >= after:
                return int(row["databaseId"])
        sleep(POLL_SECONDS)
    raise CycleError(f"no {workflow} run appeared after {iso(after)}")


def dump(pool: str, batch: int, workdir: Path, *, run: Run, sleep: Sleep) -> Dump:
    """Dispatch a queue dump, wait for it, download it into a fresh dir."""
    started = utcnow()
    _check(
        run(["gh", "workflow", "run", DUMP_WORKFLOW, "-f", f"count={batch}", "-f", f"pool={pool}"]),
        "queue-dump dispatch",
    )
    run_id = resolve_run(DUMP_WORKFLOW, started, run=run, sleep=sleep)
    _check(run(["gh", "run", "watch", str(run_id), "--exit-status"], capture=False),
           f"queue-dump run {run_id}")
    workdir.mkdir(parents=True, exist_ok=True)
    res = run(["gh", "run", "download", str(run_id), "--dir", str(workdir)])
    if res.returncode != 0:
        # An empty dump can end up with no artifact at all; that is a dry pool,
        # not a broken cycle, and the loop must be able to move on unattended.
        if "no artifact" in ((res.stdout or "") + (res.stderr or "")).lower():
            return Dump(run_id, None, 0)
        raise CycleError(f"gh run download {run_id} failed (exit {res.returncode}): {_detail(res)}")
    queue = next(iter(sorted(workdir.rglob("queue.jsonl"))), None)
    if queue is None:
        raise CycleError(f"run {run_id} produced no queue.jsonl under {workdir}")
    docs = sum(1 for line in queue.read_text(encoding="utf-8").splitlines() if line.strip())
    return Dump(run_id, queue, docs)


def _last_summary(stdout: str) -> dict[str, Any]:
    """The drain's final JSON line: {run_id, attempted, counts}."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "counts" in row:
            return row
    return {}


def drain(queue: Path, outbox: Path, batch: int, *, run: Run) -> tuple[int, dict[str, Any]]:
    """One local codex drain. Returns its exit code and its summary line.

    Exit 0 is a finished pass, 3 is "codex throttled" (the caller decides
    whether to wait it out); anything else means the script itself broke.
    """
    res = run(["uv", "run", "python", str(DRAIN_SCRIPT), str(queue), str(outbox),
               "--max-docs", str(batch)])
    out = res.stdout or ""
    if out:
        print(out, end="" if out.endswith("\n") else "\n", flush=True)
    if res.returncode not in (0, 3):
        raise CycleError(f"local_codex_drain exited {res.returncode}: {_detail(res)}")
    return res.returncode, _last_summary(out)


def blobs(outbox: Path) -> set[str]:
    """Attempt objects currently in the outbox, by outbox-relative key."""
    if not outbox.is_dir():
        return set()
    return {p.relative_to(outbox).as_posix() for p in outbox.rglob("*.json.gz")}


def upload(outbox: Path, workdir: Path, key: Path, count: int, *, run: Run, sleep: Sleep) -> Upload:
    """Tar, encrypt, attach to a draft release, ingest, and watch it green.

    The whole outbox ships every time: the ingest skips keys already in the
    archive, which is what makes the pipeline resumable.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    tgz = workdir / "outbox.tgz"
    enc = workdir / "outbox.tgz.enc"
    env = {**os.environ, "COPYFILE_DISABLE": "1"}  # no macOS AppleDouble (._*) members
    _check(run(["tar", "czf", str(tgz), "-C", str(outbox.parent), outbox.name], env=env), "tar")
    _check(
        run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-pass", f"file:{key}",
             "-in", str(tgz), "-out", str(enc)]),
        "openssl enc",
    )
    tag = "outbox-" + utcnow().strftime("%Y%m%dT%H%M%SZ")
    _check(
        run(["gh", "release", "create", tag, "--draft", "--notes", "codex outbox",
             f"{enc}#outbox.tgz.enc"]),
        "gh release create",
    )
    started = utcnow()
    _check(run(["gh", "workflow", "run", INGEST_WORKFLOW, "-f", f"tag={tag}"]), "ingest dispatch")
    run_id = resolve_run(INGEST_WORKFLOW, started, run=run, sleep=sleep)
    _check(run(["gh", "run", "watch", str(run_id), "--exit-status"], capture=False),
           f"outbox-ingest run {run_id}")
    return Upload(tag, run_id, count)


def _merge(total: dict[str, Any], summary: dict[str, Any]) -> None:
    total["attempted"] += int(summary.get("attempted") or 0)
    for outcome, n in (summary.get("counts") or {}).items():
        total["counts"][outcome] = total["counts"].get(outcome, 0) + int(n)


def _log(outbox: Path, line: dict[str, Any]) -> None:
    outbox.mkdir(parents=True, exist_ok=True)
    text = json.dumps(line, ensure_ascii=False)
    print(text, flush=True)
    with (outbox / "cycle-log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def loop(args: argparse.Namespace, *, run: Run, sleep: Sleep) -> int:
    outbox = Path(args.outbox).resolve()
    if outbox.name != "outbox":
        # outbox-ingest.yml untars into `outbox/` and reads that path; a
        # differently named directory would ingest nothing, silently.
        raise CycleError(f"--outbox must be a directory named 'outbox' (got {outbox.name!r})")
    key = Path(args.key).expanduser()
    if not key.is_file():
        raise CycleError(f"encryption key not found at {key} (see the local-codex-drain runbook)")
    work_root = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="drain-"))

    pool = "queue" if args.pool == "auto" else args.pool
    cycle = 0
    while args.cycles == 0 or cycle < args.cycles:
        cycle += 1
        workdir = work_root / f"cycle-{cycle:03d}"
        before = blobs(outbox)
        dumped = dump(pool, args.batch, workdir, run=run, sleep=sleep)
        base: dict[str, Any] = {
            "cycle": cycle, "pool": pool, "dump_run": dumped.run_id, "dumped": dumped.docs,
            "at": iso(utcnow()),
        }
        if dumped.docs == 0 or dumped.queue is None:
            _log(outbox, {**base, "drained": 0, "counts": {}, "blobs": 0, "tag": None,
                          "ingest_run": None, "throttled": False, "note": "pool dry"})
            if args.pool == "auto" and pool == "queue":
                pool = "quarantined"
                continue
            return 0

        totals: dict[str, Any] = {"attempted": 0, "counts": {}}
        code, summary = drain(dumped.queue, outbox, args.batch, run=run)
        _merge(totals, summary)
        retries = 0
        while code == 3 and retries < args.throttle_retries:
            retries += 1
            print(f"drain-cycle: throttled, waiting {args.throttle_wait:g}m "
                  f"(retry {retries}/{args.throttle_retries})", flush=True)
            sleep(args.throttle_wait * 60)
            code, summary = drain(dumped.queue, outbox, args.batch, run=run)
            _merge(totals, summary)

        new = blobs(outbox) - before
        shipped = upload(outbox, workdir, key, len(new), run=run, sleep=sleep) if new else None
        _log(outbox, {**base, "drained": totals["attempted"], "counts": totals["counts"],
                      "blobs": len(new), "tag": shipped.tag if shipped else None,
                      "ingest_run": shipped.run_id if shipped else None,
                      "throttled": code == 3})
        if code == 3:
            return 3
    return 0


def main(argv: Sequence[str] | None = None, *, run: Run = shell, sleep: Sleep = time.sleep) -> int:
    ap = argparse.ArgumentParser(description="Unattended codex drain cycle.")
    ap.add_argument("--pool", choices=("auto", "queue", "quarantined"), default="auto",
                    help="auto starts on 'queue' and falls through to 'quarantined'")
    ap.add_argument("--batch", type=int, default=100, help="documents per cycle")
    ap.add_argument("--cycles", type=int, default=0, help="0 = until a pool is dry")
    ap.add_argument("--outbox", type=Path, default=Path("outbox"))
    ap.add_argument("--work-dir", type=Path, default=None,
                    help="where dumps and tarballs land (default: a temp dir)")
    ap.add_argument("--key", type=Path, default=DEFAULT_KEY, help="outbox encryption key file")
    ap.add_argument("--throttle-wait", type=float, default=45.0,
                    help="minutes to wait after a throttled drain")
    ap.add_argument("--throttle-retries", type=int, default=4,
                    help="throttled retries per cycle before giving up with exit 3")
    args = ap.parse_args(argv)
    try:
        return loop(args, run=run, sleep=sleep)
    except CycleError as exc:
        print(f"drain-cycle: {exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - operator stop
        print("drain-cycle: interrupted", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
