"""Opt-in live check: fetch each registered board once, print counts, write nothing.

Usage: uv run python scripts/live_smoke.py [companies.toml]
"""

from __future__ import annotations

import sys
from pathlib import Path

from jobhunter.http import Fetcher
from jobhunter.registry import load
from jobhunter.sources import get_source
from jobhunter.sources.base import EnvelopeError


def main() -> int:
    reg = load(Path(sys.argv[1] if len(sys.argv) > 1 else "companies.toml"))
    fetcher = Fetcher()
    worst = 0
    for b in reg.boards:
        src = get_source(b.source)
        res = fetcher.fetch(src.url(b))
        try:
            n: int | str = sum(1 for _ in src.parse(res.body)) if res.transport == "ok" else "-"
        except EnvelopeError as e:
            n = f"envelope error: {e}"
        print(f"{b.key:32} {res.transport:11} {res.status or '-':>4} {len(res.body):>9}B  {n}")
        worst = max(worst, 0 if res.transport == "ok" else 1)
    fetcher.close()
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
