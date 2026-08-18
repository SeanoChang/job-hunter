#!/usr/bin/env python3
"""Diff the parser's requirement expressions against the hand-labeled gold set.

Usage:
  python3 eval_gold.py                      # tier-1 rules vs gold
  python3 eval_gold.py --silver out.jsonl   # also score tier-2 (judge) output

Exit code 1 if any tier-1 bullet mismatches or any tier-2 bullet is unflagged.
"""
import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jobparse  # noqa: E402  (does not exist yet -> RED)

GOLD = os.path.join(HERE, "gold", "requirement_bullets.jsonl")
FIXTURES = sorted(glob.glob(os.path.join(HERE, "fixtures", "*.json")))


def norm_atom(a):
    k = a["kind"]
    if k in ("trait", "capability"):
        return (k,)
    if k == "family":
        return (k, a["target"], tuple(sorted(a.get("exemplars") or [])),
                bool(a.get("open_class")))
    return (k, a.get("target"))


def norm_expr(expr):
    atoms = sorted(norm_atom(a) for a in expr["atoms"] if a["kind"] != "other")
    return expr["op"], atoms


def fmt_atoms(atoms):
    out = []
    for a in atoms:
        if a[0] in ("trait", "capability"):
            out.append(a[0])
        elif a[0] == "family":
            out.append(f"{a[1]}[{','.join(x.split(':')[1] for x in a[2])}]{'~' if a[3] else ''}")
        else:
            out.append(a[1] or a[0])
    return " + ".join(out) or "∅"


def compare(gold, got):
    """Return list of (field, gold_value, got_value) mismatches."""
    diffs = []
    for f in ("importance", "demand", "min_months"):
        if gold[f] != got[f]:
            diffs.append((f, gold[f], got[f]))
    gop, gatoms = norm_expr(gold["expr"])
    pop, patoms = norm_expr(got["expr"])
    if gop != pop:
        diffs.append(("op", gop, pop))
    if gatoms != patoms:
        diffs.append(("atoms", fmt_atoms(gatoms), fmt_atoms(patoms)))
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--silver", help="jsonl of tier-2 (judge) outputs keyed by id")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    gold = [json.loads(l) for l in open(GOLD) if l.strip()]
    silver = {}
    if args.silver:
        silver = {r["id"]: r for r in (json.loads(l) for l in open(args.silver) if l.strip())}

    parsed = {}
    for fx in FIXTURES:
        doc = jobparse.parse_fixture(fx)
        for r in doc["requirements"]:
            parsed[(doc["source"], r["node"])] = r

    t1_pass = t1_total = 0
    t2_flagged = t2_total = t1_flagged = 0
    t2_pass = t2_scored = 0
    field_ok = {"importance": 0, "demand": 0, "min_months": 0}
    residue_total = 0
    failures = []

    for g in gold:
        key = (g["source"], g["node"])
        got = parsed.get(key)
        if got is None:
            failures.append((g["id"], [("missing", "requirement", None)]))
            if g["tier"] == 1:
                t1_total += 1
            else:
                t2_total += 1
            continue
        for f in field_ok:
            field_ok[f] += g[f] == got[f]
        residue_total += len(got.get("residue", []))
        diffs = compare(g, got)
        if g["tier"] == 1:
            t1_total += 1
            t1_flagged += bool(got["needs_tier2"])
            if not diffs:
                t1_pass += 1
            else:
                failures.append((g["id"], diffs))
        else:
            t2_total += 1
            if got["needs_tier2"]:
                t2_flagged += 1
            else:
                failures.append((g["id"], [("needs_tier2", True, False)]))
            if g["id"] in silver:
                t2_scored += 1
                sd = compare(g, silver[g["id"]])
                if not sd:
                    t2_pass += 1
                else:
                    failures.append((g["id"] + " (silver)", sd))
        if args.verbose:
            mark = ("ok " if not diffs else "BAD") if g["tier"] == 1 else \
                   ("ok " if got["needs_tier2"] else "BAD")
            print(f"  {mark} t{g['tier']} {g['id']:<22} op={got['expr']['op']:<6} "
                  f"{fmt_atoms(norm_expr(got['expr'])[1])}"
                  f"{'  [flag: ' + ','.join(got['tier2_reasons']) + ']' if got['needs_tier2'] else ''}")

    n = len(gold)
    print(f"\ntier-1 exact tree : {t1_pass}/{t1_total}")
    print(f"tier-2 flagged    : {t2_flagged}/{t2_total}   (false flags on tier-1: {t1_flagged}/{t1_total})")
    if silver:
        print(f"tier-2 silver ok  : {t2_pass}/{t2_scored}")
    for f, c in field_ok.items():
        print(f"{f:<18}: {c}/{n}")
    print(f"residue chunks    : {residue_total}")
    if failures:
        print("\nFAILURES")
        for fid, diffs in failures:
            print(f"  {fid}")
            for f, gv, pv in diffs:
                print(f"     {f:<11} gold={gv!r:<40} got={pv!r}")
    ok = t1_pass == t1_total and t2_flagged == t2_total and (not silver or t2_pass == t2_scored)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
