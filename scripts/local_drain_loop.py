"""Continuous local codex drain: run `extract run` batches back to back,
back off on throttle, stop when the queue is empty. Local-only — no GitHub,
no cloud. Settings come from ./.env (codex-cli engine, local store/archive).

Usage:
    uv run python scripts/local_drain_loop.py [--batch 500] [--throttle-wait 30]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time


def one_batch(batch: int) -> dict[str, object]:
    proc = subprocess.run(
        ["uv", "run", "job-hunter", "extract", "run",
         "--max-docs", str(batch), "--max-usd", "0", "-o", "json"],
        capture_output=True, text=True, check=False,
        cwd="/Users/balanoi/dev/job-hunter",
    )
    try:
        envelope = json.loads(proc.stdout)
        data = envelope.get("data") or {}
    except ValueError:
        data = {"parse_error": proc.stdout[-300:], "stderr": proc.stderr[-300:]}
    data["exit_code"] = proc.returncode
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--throttle-wait", type=int, default=30, help="minutes")
    ap.add_argument("--max-throttles", type=int, default=48)
    args = ap.parse_args()

    throttles = 0
    cycle = 0
    while True:
        cycle += 1
        data = one_batch(args.batch)
        line = {k: data.get(k) for k in
                ("run_id", "docs_attempted", "validated", "quarantined",
                 "pending", "throttled", "breaker_abort", "replayed",
                 "lock_held", "exit_code")}
        line["cycle"] = cycle
        queued = data.get("queued")
        line["queued_n"] = len(queued) if isinstance(queued, list) else 0
        print(json.dumps(line), flush=True)
        if data.get("lock_held"):
            print(json.dumps({"stop": "another writer holds the lock"}), flush=True)
            return 1
        if data.get("breaker_abort"):
            print(json.dumps({"stop": "model breaker tripped"}), flush=True)
            return 1
        if data.get("throttled"):
            throttles += 1
            if throttles > args.max_throttles:
                print(json.dumps({"stop": "throttle budget exhausted"}), flush=True)
                return 3
            time.sleep(args.throttle_wait * 60)
            continue
        if not data.get("queued") and not data.get("docs_attempted"):
            print(json.dumps({"stop": "queue empty"}), flush=True)
            return 0
        throttles = 0


if __name__ == "__main__":
    sys.exit(main())
