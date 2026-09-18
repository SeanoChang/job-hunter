"""Offline validator-19 replay evaluation (plan Task 6 Step 1). READ-ONLY.

Re-judges archived v2 raw emits under the validator at HEAD — assembly,
verification and the agreement comparator — with zero engine calls and zero
database writes, against two sets:

- the failed set: every needs_review + quarantined doc at validator 18
- a control set: N validated-18 docs (deterministic order by document_hash)

Reported: FALSE ACCEPTANCE (control records that passed 18 and fail a 19
check — each a silently wrong value or unevidenced coverage claim 18 let
through), movement on the failed set, and per-check deltas.

Usage:
    uv run python scripts/replay_eval_v19.py --failed-set --control 200 --dry-run
(--dry-run is accepted for the ticket predicate; the script never writes.)
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import psycopg

from jobhunter.l2.agreement import agree
from jobhunter.l2.assemble import AssembleError
from jobhunter.l2.bundles import get_bundle
from jobhunter.l2.runner import NORMALIZER_VERSION, normalize_emit, validate_emit

DB = os.environ.get(
    "JOB_HUNTER_DATABASE_URL", "postgresql://jobhunter:jobhunter@localhost:5432/jobhunter"
)
ARCHIVE = Path(os.environ.get("JOB_HUNTER_ARCHIVE_DIR", "data/archive")) / "extractions/attempts"
FNAME = re.compile(r"^[^-]+-(?P<doc>[0-9a-f]{12})-s(?P<s>\d+)a(?P<a>\d+)\.json\.gz$")


def _load(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt") as f:
        return json.load(f)


def _rejudge(bundle: Any, raw: str, markdown: str, dh: str) -> tuple[str, list[str]]:
    """One archived raw response under the HEAD contract: outcome + error codes."""
    try:
        emit = json.loads(raw)
        if not isinstance(emit, dict):
            raise ValueError("top level is not an object")
        emit = normalize_emit(emit, bundle.schema_version)
    except ValueError as exc:
        return "unparseable", [str(exc)[:60]]
    if schema_errors := validate_emit(emit, bundle.schema_version):
        return "schema_invalid", sorted({e.split(":")[0] for e in schema_errors})
    try:
        record = bundle.assemble(emit, markdown, document_hash=dh,
                                 normalizer_version=NORMALIZER_VERSION,
                                 observed_model="replay", at="2026-09-16T00:00:00+00:00")
    except AssembleError as exc:
        return "attribution_failed", sorted({e.split(":")[0].split(" ")[0] for e in exc.errors})
    report = bundle.verify(record, markdown)
    codes = sorted({f"{f.check}:{f.code}" for f in report.findings if f.severity == "error"})
    return ("verify_fail", codes) if report.status == "fail" else ("ok", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--failed-set", action="store_true")
    ap.add_argument("--control", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")  # documentation: always true
    args = ap.parse_args()
    bundle = get_bundle("v2")

    with psycopg.connect(DB) as conn:
        rows = conn.execute(
            """SELECT e.document_hash, e.status, e.chosen_attempt,
                      e.profile->'quality'->>'sampling', d.markdown
               FROM extractions e JOIN documents d ON d.document_hash = e.document_hash
               WHERE e.validator_version = '18'""",
        ).fetchall()
    control = sorted(
        [r for r in rows if r[1] == "validated"], key=lambda r: r[0]
    )[: args.control] if args.control else []
    failed = [r for r in rows if r[1] in ("needs_review", "quarantined")] \
        if args.failed_set else []
    take = {r[0]: r for r in control + failed}
    print(f"replaying: control={len(control)} failed={len(failed)} "
          f"(validator-18 rows total: {len(rows)})", file=sys.stderr)

    # index attempt artifacts for the taken docs
    by_doc: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    by12 = {h[:12]: h for h in take}
    for p in ARCHIVE.rglob("*.json.gz"):
        m = FNAME.match(p.name)
        if m and (full := by12.get(m.group("doc"))):
            by_doc[full].append((p.name, p))

    ctrl_outcome = Counter()
    ctrl_codes = Counter()
    ctrl_gate = Counter()
    false_accept: list[dict[str, Any]] = []
    for dh, _status, chosen, _sampling, markdown in control:
        arts = dict(by_doc.get(dh, []))
        chosen_name = (chosen or "").rsplit("/", 1)[-1]
        art = arts.get(chosen_name)
        if art is None:
            ctrl_outcome["missing_artifact"] += 1
            continue
        d = _load(art)
        if d.get("schema_version") != "2":
            ctrl_outcome["not_schema_2"] += 1
            continue
        outcome, codes = _rejudge(bundle, d["raw_response"], markdown, dh)
        ctrl_outcome[outcome] += 1
        for c in codes:
            ctrl_codes[c] += 1
        if outcome != "ok":
            false_accept.append({"doc": dh[:12], "outcome": outcome, "codes": codes})
        # agreement under the 19 comparator: the last parsed-ok emit per slot
        by_slot: dict[int, dict[str, Any]] = {}
        for name, p in sorted(arts.items()):
            m = FNAME.match(name)
            if m is None:
                continue
            a = _load(p)
            if a.get("schema_version") != "2" or a.get("outcome") != "ok":
                continue
            try:
                emit = normalize_emit(json.loads(a["raw_response"]), bundle.schema_version)
                rec = bundle.assemble(emit, markdown, document_hash=dh,
                                      normalizer_version=NORMALIZER_VERSION,
                                      observed_model="replay",
                                      at="2026-09-16T00:00:00+00:00")
            except (ValueError, AssembleError):
                continue
            by_slot[int(m.group("s"))] = bundle.profile_of(rec)
        records = [by_slot[s] for s in sorted(by_slot)]
        if len(records) >= 2:
            r = agree(records, f1_min=bundle.agreement_f1_min)
            if not r.passed:
                ctrl_gate[tuple(sorted(r.report["failures"]))] += 1

    print("\n== CONTROL SET (validated under 18) ==")
    for k, n in ctrl_outcome.most_common():
        print(f"{n:5d}  chosen record now: {k}")
    print("false-acceptance rate: "
          f"{len(false_accept)}/{max(len(control), 1)} "
          f"({len(false_accept) / max(len(control), 1):.1%})")
    for k, n in ctrl_codes.most_common(15):
        print(f"{n:5d}  {k}")
    if ctrl_gate:
        print("cohorts that would now DISAGREE under the 19 comparator:")
        for k, n in ctrl_gate.most_common(10):
            print(f"{n:5d}  {','.join(k)}")
    for fa in false_accept[:20]:
        print("  FA:", json.dumps(fa))

    fail_move = Counter()
    fail_codes = Counter()
    for dh, status, _chosen, _sampling, markdown in failed:
        arts = dict(by_doc.get(dh, []))
        if not arts:
            fail_move[(status, "no_artifacts")] += 1
            continue
        # the last attempt per failing doc (quarantined/incomplete) or the
        # medoid-side chosen attempt (disagreement docs have chosen=None)
        name, p = sorted(arts.items())[-1]
        d = _load(p)
        if d.get("schema_version") != "2":
            continue
        was = d.get("outcome")
        outcome, codes = _rejudge(bundle, d["raw_response"], markdown, dh)
        fail_move[(status, f"{was}->{outcome}")] += 1
        for c in codes:
            fail_codes[c] += 1

    if failed:
        print("\n== FAILED SET (needs_review + quarantined under 18): last attempt re-judged ==")
        for (st, mv), n in sorted(fail_move.items(), key=lambda kv: -kv[1]):
            print(f"{n:5d}  [{st}] {mv}")
        print("top error codes now:")
        for k, n in fail_codes.most_common(15):
            print(f"{n:5d}  {k}")
    print("\nno writes performed (read-only by construction)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
