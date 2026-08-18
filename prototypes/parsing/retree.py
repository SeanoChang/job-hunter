#!/usr/bin/env python3
"""Tier-2 re-treeing: LLM judge rebuilds the expression tree for bullets the
rules flagged (`needs_tier2`). Bring-your-own-agent: calls the local `claude`
CLI in print mode with a JSON schema — no API key handling here.

The judge may change: op, atoms (kind/target/text/exemplars/open_class).
The judge may NOT change: importance, demand, min_months — those are the
deterministic facts from tier-1 and are copied through unchanged.

Usage:
  python3 retree.py --out silver.jsonl            # flagged bullets only
  python3 retree.py --out silver.jsonl --all      # every requirement bullet (calibration)
  python3 retree.py --dry-run                     # print prompts, no calls
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jobparse  # noqa: E402

KINDS = ["skill", "family", "abstract", "capability", "trait", "credential"]
OPS = ["SINGLE", "AND", "OR", "MIXED"]
KNOWN = {cid for _, cid, _ in jobparse._ALIASES} | {cid for _, _, cid in jobparse.UMBRELLAS} \
        | {"credential:degree"}

SCHEMA = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": OPS},
        "atoms": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": KINDS},
                "target": {"type": ["string", "null"]},
                "text": {"type": "string"},
                "exemplars": {"type": "array", "items": {"type": "string"}},
                "open_class": {"type": "boolean"},
            },
            "required": ["kind", "target", "text"]}},
        "confidence": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": ["op", "atoms", "confidence"],
}

SYSTEM = """You are a strict parser for job-posting requirement bullets.
You convert ONE bullet into a requirement expression: {op, atoms[]}.
Be faithful to the text. Never add requirements the bullet does not state.
Return only the structured object."""

GUIDE = f"""Atom kinds:
- skill: a nameable concept from the known list below (use its exact id).
- family: an umbrella phrase from the known list (e.g. family:web-frontend for "modern web stack",
  family:cloud-infra for "cloud infrastructure"). If a parenthetical lists examples, put their ids
  in exemplars (only ids from the known list); open_class=true iff the text says "or similar",
  "or equivalent", "etc.", "e.g.", "such as", or "ideally".
- abstract: a decomposable high-level competence (abstract:swe-fundamentals, abstract:fullstack).
- capability: a demonstrated act or artifact ("built X", "shipped Y", "worked with vendors").
  target=null; text = a short faithful paraphrase of WHAT was built/done, keeping qualifiers.
- trait: a disposition or soft skill (care about, communicate, thrive, desire, alignment...).
  target=null.
- credential: a degree/certification (credential:degree).

Structure rules:
- A bullet whose whole content is one disposition or one act is ONE atom, even if it contains
  commas or "and" ("Eager to learn quickly, take ownership, and mentor others" -> one trait).
- A comma list inside a noun phrase ("payment, billing, or invoicing systems")
  is ONE atom, not three.
- Contrastive/elaborating clauses ("not just X", "— e.g. ...") do not add atoms.
- op: SINGLE iff exactly one atom; OR if the atoms are alternatives; AND if all are required;
  MIXED only when a genuine top-level mix cannot be expressed otherwise.
- Do not invent concept ids. If a named technology/skill is not in the known list, keep it as
  kind=capability ONLY when the bullet describes something built or done with it; otherwise
  describe it in the text of the atom whose kind fits (a career background, personality, or
  attitude is a trait, never a capability).

Known concept ids: {", ".join(sorted(cid for _, cid, _ in jobparse._ALIASES))}
Known family/abstract ids: {", ".join(sorted({cid for _, _, cid in jobparse.UMBRELLAS}))}
"""


def gold_id(source, posting_id, node):
    pid = str(posting_id)
    if source == "ashby":
        pid = pid[:8]
    return f"{'gh' if source == 'greenhouse' else 'as'}:{pid}:n{node}"


def build_prompt(req):
    t1 = [{"kind": a["kind"], "target": a.get("target"), "text": a.get("text")}
          for a in req["expr"]["atoms"]]
    return (f"{GUIDE}\n"
            f"Section: {req['section']}\n"
            f"Bullet: {req['text']}\n\n"
            f"Rule-based first pass (may be wrong; flagged because: "
            f"{', '.join(req['tier2_reasons']) or 'calibration run'}):\n"
            f"{json.dumps({'op': req['expr']['op'], 'atoms': t1}, ensure_ascii=False)}\n\n"
            f"Produce the correct expression for this bullet.")


def validate(out):
    """Checks beyond JSON-schema shape. Returns list of error strings (empty = ok)."""
    errs = []
    atoms = out.get("atoms", [])
    if out.get("op") == "SINGLE" and len(atoms) != 1:
        errs.append(f"op SINGLE requires exactly 1 atom, got {len(atoms)}")
    if out.get("op") != "SINGLE" and len(atoms) == 1:
        errs.append("one atom must use op SINGLE")
    if not atoms:
        errs.append("no atoms")
    for a in atoms:
        k = a.get("kind")
        if k in ("trait", "capability") and a.get("target"):
            errs.append(f"{k} atom must have target=null, got {a.get('target')!r}")
        if k in ("skill", "family", "abstract", "credential"):
            if a.get("target") not in KNOWN:
                errs.append(f"unknown target {a.get('target')!r} for {k} atom")
        for ex in a.get("exemplars") or []:
            if ex not in KNOWN:
                errs.append(f"unknown exemplar {ex!r}")
    return errs


def call_claude(prompt, model, effort=None, system=None, schema=None):
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json",
           "--no-session-persistence", "--tools", "", "--strict-mcp-config",
           "--mcp-config", '{"mcpServers":{}}', "--system-prompt", system or SYSTEM,
           "--json-schema", json.dumps(schema or SCHEMA)]
    if effort:
        cmd += ["--effort", effort]
    for attempt in range(3):            # the CLI auto-updates in place; the binary
        exe = shutil.which("claude")    # can vanish for a moment between calls
        if exe:
            try:
                r = subprocess.run([exe] + cmd[1:], capture_output=True, text=True,
                                   timeout=180, cwd=HERE)
                break
            except FileNotFoundError:
                pass
        time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError("claude CLI not found on PATH")
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {r.stderr[:500]}")
    d = json.loads(r.stdout)
    if d.get("is_error") or "structured_output" not in d:
        raise RuntimeError(f"no structured output: {d.get('result', '')[:300]}")
    return d["structured_output"], {"cost_usd": d.get("total_cost_usd"),
                                    "model": next(iter(d.get("modelUsage", {})), model)}


def retree(req, model, effort=None, max_attempts=2):
    prompt = build_prompt(req)
    errs, out, meta = ["not run"], None, {}
    for attempt in range(max_attempts):
        p = prompt if not out else prompt + "\n\nYour previous answer failed validation: " \
            + "; ".join(errs) + ". Fix and return again."
        out, meta = call_claude(p, model, effort)
        errs = validate(out)
        if not errs:
            break
    return out, errs, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "silver.jsonl"))
    ap.add_argument("--all", action="store_true", help="also run non-flagged bullets")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows, total_cost = [], 0.0
    for fx in sorted(glob.glob(os.path.join(HERE, "fixtures", "*.json"))):
        doc = jobparse.parse_fixture(fx)
        for req in doc["requirements"]:
            if not (req["needs_tier2"] or args.all):
                continue
            rid = gold_id(doc["source"], doc["id"], req["node"])
            if args.dry_run:
                print(f"--- {rid}\n{build_prompt(req)}\n")
                continue
            out, errs, meta = retree(req, args.model, args.effort)
            total_cost += meta.get("cost_usd") or 0.0
            status = "ok" if not errs else "INVALID"
            print(f"{rid:<22} {status:<8} op={out['op']:<6} "
                  f"{[a['target'] or a['kind'] for a in out['atoms']]}"
                  f"  conf={out.get('confidence')}  ${meta.get('cost_usd', 0):.3f}")
            if errs:
                for e in errs:
                    print(f"      ! {e}")
            rows.append({"id": rid, "source": doc["source"], "node": req["node"],
                         "text": req["text"],
                         # deterministic facts copied through — judge cannot alter
                         "importance": req["importance"], "demand": req["demand"],
                         "min_months": req["min_months"],
                         "expr": {"op": out["op"], "atoms": out["atoms"]},
                         "confidence": out.get("confidence"), "notes": out.get("notes"),
                         "validation_errors": errs, "tier1": req["expr"],
                         "tier2_reasons": req["tier2_reasons"], "meta": meta})
    if not args.dry_run:
        with open(args.out, "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\n{len(rows)} bullets -> {args.out}   total ${total_cost:.2f}")


if __name__ == "__main__":
    main()
