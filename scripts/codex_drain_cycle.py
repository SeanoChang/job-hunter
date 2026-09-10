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
empty the loop exits 0. A pool that keeps offering documents the drain refuses
(it skips any document whose attempt is already in the outbox) is finished in
the same sense — the cycle logs "no progress" and the loop stops or switches
pools, rather than buying a CI run per cycle forever. Exit 3 means codex stayed
throttled through the whole `--throttle-retries` budget — the outbox is still
uploaded first, because partial progress is real progress.

Each cycle ships only the attempts whose ingest has not yet been watched green,
tracked in `outbox/cycle-shipped.json` (driver bookkeeping, never attempt
content): transport stays linear in the backlog instead of re-sending the whole
accumulated outbox once per cycle, and anything an interrupted cycle left
behind still ships with the next batch.

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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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
# GitHub stamps `createdAt` on its own clock; this machine's may sit seconds
# ahead of it between NTP syncs. Without slack a freshly created run reads as
# stale and the driver abandons a CI job it just paid for. Time is therefore
# only a coarse backstop against ancient runs — identity is what actually
# separates this dispatch's run from every other, via `seen`.
CLOCK_SKEW = timedelta(seconds=60)
# How far back the pre-dispatch snapshot records existing run ids. One would do
# (the resolve poll only ever looks at the newest run), but the listing is one
# cheap call and a few extra ids cost nothing, while a missed one costs a
# wrongly adopted run.
SNAPSHOT_LIMIT = 20
# Driver-owned, inside the outbox because that is what survives between
# invocations (the work dir may be a temp dir): the outbox-relative keys whose
# ingest has been watched green. Never shipped, never read by anything else.
SHIPPED_LEDGER = "cycle-shipped.json"

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


def snapshot_runs(workflow: str, *, run: Run, seen: set[int]) -> None:
    """Record the ids of `workflow` runs that exist now, before a dispatch adds one.

    This is the whole anti-stale guarantee, and it has to happen before the
    dispatch. Time cannot provide it: GitHub stamps `createdAt` on its own
    clock, so a dispatch's own run must be accepted with seconds of slack — and
    anything already inside that slack window would be accepted too. Two real
    things sit in that window: the operator's manual `gh workflow run
    extract-queue-dump.yml` from the runbook, and the dump a previous
    invocation of this driver left behind when it stopped on a throttle or a
    Ctrl-C (the loop restarts from cycle 1 with an empty `seen`). Adopting one
    of those would watch and drain a foreign dump while the run this cycle paid
    for went unwatched. Identity has no window: an id that existed before the
    dispatch is not the dispatch's id.
    """
    res = _check(
        run(["gh", "run", "list", f"--workflow={workflow}", "--limit", str(SNAPSHOT_LIMIT),
             "--json", "databaseId"]),
        "gh run list",
    )
    for row in json.loads(res.stdout or "[]"):
        seen.add(int(row["databaseId"]))


def dispatch(
    workflow: str, inputs: Sequence[str], *, run: Run, sleep: Sleep, seen: set[int]
) -> int:
    """Dispatch `workflow` with `-f` inputs; return the id of the run it created."""
    snapshot_runs(workflow, run=run, seen=seen)
    started = utcnow()
    argv: list[str] = ["gh", "workflow", "run", workflow]
    for value in inputs:
        argv += ["-f", value]
    _check(run(argv), f"{workflow} dispatch")
    return resolve_run(workflow, started, run=run, sleep=sleep, seen=seen)


def resolve_run(workflow: str, after: datetime, *, run: Run, sleep: Sleep, seen: set[int]) -> int:
    """The id of the `workflow` run this dispatch created; records it in `seen`.

    A dispatch prints no id, so the run has to be looked up — and the lookup
    must never adopt a run that was already there. `seen` is what rules them
    out, and it is exact: it holds every id the pre-dispatch snapshot saw plus
    every id this process has already adopted, and GitHub never reissues one.
    `after` (the whole-second floored instant just before the dispatch, less
    `CLOCK_SKEW`) is the coarse backstop for ids no snapshot could have seen —
    a run that surfaces in the listing late, days after it was created.
    """
    floor = after - CLOCK_SKEW
    for _ in range(POLL_TRIES):
        res = _check(
            run(["gh", "run", "list", f"--workflow={workflow}", "--limit", "1",
                 "--json", "databaseId,createdAt"]),
            "gh run list",
        )
        for row in json.loads(res.stdout or "[]"):
            run_id = int(row["databaseId"])
            if run_id not in seen and parse_iso(str(row["createdAt"])) >= floor:
                seen.add(run_id)
                return run_id
        sleep(POLL_SECONDS)
    raise CycleError(f"no {workflow} run appeared after {iso(after)}")


def dump(pool: str, batch: int, workdir: Path, *, run: Run, sleep: Sleep, seen: set[int]) -> Dump:
    """Dispatch a queue dump, wait for it, download it into a fresh dir.

    The download goes into a directory named for the run, and the queue is
    resolved inside that directory alone: a reused `--work-dir` (resuming after
    a throttle stop or a Ctrl-C) otherwise holds an earlier invocation's dump
    too, and draining that one would burn a CI run to extract nothing new.
    """
    run_id = dispatch(
        DUMP_WORKFLOW, [f"count={batch}", f"pool={pool}"], run=run, sleep=sleep, seen=seen
    )
    _check(run(["gh", "run", "watch", str(run_id), "--exit-status"], capture=False),
           f"queue-dump run {run_id}")
    dest = workdir / f"dump-{run_id}"
    dest.mkdir(parents=True, exist_ok=True)
    res = run(["gh", "run", "download", str(run_id), "--dir", str(dest)])
    if res.returncode != 0:
        # An empty dump can end up with no artifact at all; that is a dry pool,
        # not a broken cycle, and the loop must be able to move on unattended.
        if "no artifact" in ((res.stdout or "") + (res.stderr or "")).lower():
            return Dump(run_id, None, 0)
        raise CycleError(f"gh run download {run_id} failed (exit {res.returncode}): {_detail(res)}")
    queue = next(iter(sorted(dest.rglob("queue.jsonl"))), None)
    if queue is None:
        raise CycleError(f"run {run_id} produced no queue.jsonl under {dest}")
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


def shipped(outbox: Path) -> set[str]:
    """Keys whose ingest has been watched green, from the ledger.

    A missing or unreadable ledger means "nothing is known shipped", which
    ships more than necessary rather than less — the ingest skips keys already
    in the archive, so the cost of being wrong here is one redundant upload,
    never a lost attempt.
    """
    path = outbox / SHIPPED_LEDGER
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(k) for k in row.get("shipped", [])} if isinstance(row, dict) else set()


def record_shipped(outbox: Path, keys: Iterable[str]) -> None:
    """Add `keys` to the ledger, atomically. Called only after an ingest ran green.

    Write-then-replace, not truncate-then-write: an unattended run is stopped
    with Ctrl-C by design, and an interrupt in the middle of an in-place
    rewrite leaves invalid JSON, which `shipped()` reads as "nothing shipped".
    Nothing would be lost, but the next cycle would put the entire accumulated
    outbox back on the wire — the linear transport this ledger exists to
    provide, silently gone.
    """
    path = outbox / SHIPPED_LEDGER
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"shipped": sorted(shipped(outbox) | set(keys))}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def member_list(outbox: Path, keys: Sequence[str], dest: Path) -> Path:
    """Write the tar member list: outbox-relative keys, prefixed by the outbox.

    `tar -C <parent> -T <list>` ships exactly these paths, so the tarball
    carries the `outbox/…` prefix the ingest untars and nothing else — no
    cycle log, no ledger, and no blob that already reached the archive.
    """
    dest.write_text(
        "".join(f"{outbox.name}/{k}\n" for k in sorted(keys)), encoding="utf-8"
    )
    return dest


def tar_outbox(outbox: Path, listing: Path, tgz: Path, *, run: Run) -> None:
    """The transport's one tar invocation: listed members only, no AppleDouble.

    `COPYFILE_DISABLE=1` is an invariant, not a preference — without it macOS
    tar adds a `._*` sidecar per member, and those reached the archive as junk
    keys once already (114 of them, the 2026-09-09 ingest).
    """
    env = {**os.environ, "COPYFILE_DISABLE": "1"}
    _check(
        run(["tar", "czf", str(tgz), "-C", str(outbox.parent), "-T", str(listing)], env=env), "tar"
    )


def upload(
    outbox: Path, workdir: Path, key: Path, members: Sequence[str],
    *, run: Run, sleep: Sleep, seen: set[int],
) -> Upload:
    """Tar, encrypt, attach to a draft release, ingest, and watch it green.

    Only `members` ship. Re-tarring the whole outbox every cycle would make
    transport quadratic in cycle count — at a hundred documents a cycle against
    a 47k backlog, hundreds of releases each carrying the entire accumulated
    outbox. Resumability comes from the ledger instead: a key is recorded only
    after its ingest ran green, so anything an interrupted cycle left behind is
    still pending and ships with the next batch.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    listing = member_list(outbox, members, workdir / "outbox-members.txt")
    tgz = workdir / "outbox.tgz"
    enc = workdir / "outbox.tgz.enc"
    tar_outbox(outbox, listing, tgz, run=run)
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
    run_id = dispatch(INGEST_WORKFLOW, [f"tag={tag}"], run=run, sleep=sleep, seen=seen)
    _check(run(["gh", "run", "watch", str(run_id), "--exit-status"], capture=False),
           f"outbox-ingest run {run_id}")
    # The release holds the copy that matters; the tarballs would just grow the
    # work dir. The queue dump and the member list stay — small, and evidence.
    tgz.unlink(missing_ok=True)
    enc.unlink(missing_ok=True)
    return Upload(tag, run_id, len(members))


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
    # stdout stays the cycle log; the banner (and the throttle notices) are for
    # the operator watching an unattended run.
    print(f"drain-cycle: pool={args.pool} batch={args.batch} cycles={args.cycles or 'until dry'} "
          f"outbox={outbox} work-dir={work_root}", file=sys.stderr, flush=True)
    seen: set[int] = set()  # every run id this process has adopted; never reused
    cycle = 0
    while args.cycles == 0 or cycle < args.cycles:
        cycle += 1
        workdir = work_root / f"cycle-{cycle:03d}"
        before = blobs(outbox)
        dumped = dump(pool, args.batch, workdir, run=run, sleep=sleep, seen=seen)
        base: dict[str, Any] = {
            "cycle": cycle, "pool": pool, "dump_run": dumped.run_id, "dumped": dumped.docs,
            "at": iso(utcnow()),
        }
        if dumped.docs == 0 or dumped.queue is None:
            _log(outbox, {**base, "drained": 0, "counts": {}, "blobs": 0, "shipped": 0,
                          "tag": None, "ingest_run": None, "throttled": False, "note": "pool dry"})
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

        held = blobs(outbox)
        new = held - before
        # The contract words this as "skip when the outbox gained no new
        # blobs"; the gate is on *unshipped* blobs, which is the same condition
        # for a cycle starting from a clean outbox and deliberately wider
        # otherwise. Blobs an interrupted cycle left behind have to reach the
        # archive too, and waiting for some later cycle to happen to write
        # would strand them. Being wrong here costs one redundant upload — the
        # ingest skips keys already in the archive — never an attempt.
        pending = sorted(held - shipped(outbox))
        sent = (
            upload(outbox, workdir, key, pending, run=run, sleep=sleep, seen=seen)
            if pending else None
        )
        if sent is not None:
            record_shipped(outbox, pending)
        # A dump that hands back documents the drain refuses — the quarantined
        # pool re-offers a document until its row moves, and the drain skips any
        # document already in the outbox — is the end of the work, not a reason
        # to buy another CI run. Without this the loop would dispatch a dump per
        # cycle forever, attempting nothing.
        # A cycle that spent its whole throttle budget also attempted nothing,
        # but its pool is untouched, not finished: calling that "no progress"
        # would tell the operator — and any campaign script reading the log —
        # that the backlog is done when codex was merely rate-limited.
        stuck = code != 3 and totals["attempted"] == 0 and not new
        _log(outbox, {**base, "drained": totals["attempted"], "counts": totals["counts"],
                      "blobs": len(new), "shipped": len(pending) if sent else 0,
                      "tag": sent.tag if sent else None,
                      "ingest_run": sent.run_id if sent else None,
                      "throttled": code == 3,
                      "note": "throttled" if code == 3 else ("no progress" if stuck else None)})
        if code == 3:
            return 3
        if stuck:
            print(f"drain-cycle: {dumped.docs} documents offered, none attempted and no new "
                  f"attempts written — pool {pool} has nothing left this driver can do",
                  file=sys.stderr, flush=True)
            if args.pool == "auto" and pool == "queue":
                pool = "quarantined"
                continue
            return 0
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
