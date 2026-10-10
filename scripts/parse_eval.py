"""A tiny parse eval: score prompt variants on a frozen case set, through codex.

Hillclimbing a prompt needs three things the drain does not give: the SAME
documents every time, repeated runs per document (stability is agreement
between runs, not a single score), and a rule for when a change is a real win.
This script is those three things and nothing else. It never touches the
database's extraction tables or the archive: each call's raw response, verdict
and derived profile go to `data/evals/parse/<run>/`.

    build-cases   draw ~60 validated v15 documents across six strata, split
                  train/test (2:1, seeded), freeze them in evals/parse/cases.jsonl
    run           render each case through a prompt VARIANT, k samples each,
                  one shot (no retry ladder), then the bundle's own assemble +
                  verify + profile_of -- the production code path
    score         per-run quality and stability, per split
    rejudge       re-judge a run's saved raw responses under today's code
                  (a validator change, measured on identical model output)
    compare       baseline vs candidate: deltas with bootstrap 95% CIs and the
                  keep/revert verdict

A variant is `evals/parse/variants/<name>.py` with `transform(template) ->
template`, applied to the active bundle's prompt template. `baseline` is the
identity. The keep rule follows the hillclimb practice of reading train
failures only: keep a variant when train AND test both improve beyond the
noise measured between two baseline runs; train-only gains are overfitting;
any regression reverts.

Usage:
    uv run python scripts/parse_eval.py build-cases
    uv run python scripts/parse_eval.py run --variant baseline --k 3
    uv run python scripts/parse_eval.py score data/evals/parse/<run>
    uv run python scripts/parse_eval.py compare data/evals/parse/<a> data/evals/parse/<b>
"""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import random
import re
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jobhunter.config import Settings, env_snapshot
from jobhunter.l2.assemble import AssembleError
from jobhunter.l2.bundles import get_bundle
from jobhunter.l2.engines import CodexCli, EngineTransportError
from jobhunter.l2.schemas import emit_schema, normalize_emit, validate_emit
from jobhunter.l2.v2.prompt import _render, _split
from jobhunter.markdown import NORMALIZER_VERSION
from jobhunter.store import db
from jobhunter.timeutil import iso, utcnow

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "evals" / "parse"
CASES = EVAL / "cases.jsonl"
RUNS = ROOT / "data" / "evals" / "parse"
BUNDLE = "v3"

# Each stratum is a slice of the corpus a parse must get right for this
# reader: intern/new-grad roles, hourly pay, years-of-experience asks,
# sponsorship text, non-English postings, and a random draw for the rest.
STRATA: dict[str, str] = {
    "entry": r"v.title ~* '\m(intern|internship|co-?op|new grad|new graduate|university grad"
             r"|early career|entry[- ]level|apprentice)\M'",
    "hourly": r"d.markdown ~* '(hourly|per hour|/hr\M|/hour)'",
    "experience": r"d.markdown ~* '\m[0-9]+\s*\+\s*years'",
    "sponsorship": r"d.markdown ~* '(sponsor|visa|work authori[sz]ation)'",
    "non_english": r"d.markdown ~* '(\mvous\M|\mnous\M|\mwir\M|\mund\M|\mpara\M|\mcon\M)'"
                   r" AND d.markdown !~* '\mthe\M.*\mthe\M.*\mthe\M'",
    "random": "true",
}


def _connect(settings: Settings) -> Any:
    if not settings.database_url:
        sys.exit("JOB_HUNTER_DATABASE_URL is not set")
    return db.connect(settings.database_url)


# --- cases --------------------------------------------------------------------


def build_cases(per_stratum: int, seed: int) -> None:
    settings = Settings.load(env_snapshot())
    conn = _connect(settings)
    b = get_bundle(BUNDLE)
    taken: set[str] = set()
    rows: list[dict[str, Any]] = []
    for name, where in STRATA.items():
        found = conn.execute(
            f"""
            SELECT DISTINCT ON (x.document_hash) x.document_hash, v.title, v.uid
            FROM extractions x
            JOIN documents d ON d.document_hash = x.document_hash
            JOIN posting_versions v ON v.version_hash = d.version_hash
            WHERE x.prompt_version = %(pv)s AND x.status = 'validated' AND ({where})
            ORDER BY x.document_hash, md5(x.document_hash || %(seed)s)
            """,
            {"pv": b.prompt_version, "seed": str(seed)},
        ).fetchall()
        pool = sorted((r for r in found if r["document_hash"] not in taken),
                      key=lambda r: r["document_hash"])
        pick = random.Random(f"{seed}:{name}").sample(pool, min(per_stratum, len(pool)))
        for r in pick:
            taken.add(r["document_hash"])
            rows.append({"document_hash": r["document_hash"], "stratum": name,
                         "uid": r["uid"], "title": r["title"]})
    rng = random.Random(seed)
    rng.shuffle(rows)
    for i, r in enumerate(rows):
        r["split"] = "test" if i % 3 == 2 else "train"
    rows.sort(key=lambda r: (r["split"], r["stratum"], r["document_hash"]))
    EVAL.mkdir(parents=True, exist_ok=True)
    CASES.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    counts = {s: sum(r["split"] == s for r in rows) for s in ("train", "test")}
    print(json.dumps({"cases": len(rows), **counts, "file": str(CASES.relative_to(ROOT))}))


def load_cases(split: str) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if split == "all" or r["split"] == split]


# --- run ----------------------------------------------------------------------


def load_variant(name: str) -> Callable[[str], str]:
    path = EVAL / "variants" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"variant_{name}", path)
    if spec is None or spec.loader is None:
        sys.exit(f"no variant at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn: Callable[[str], str] = module.transform
    return fn


def one_shot(engine: CodexCli, model: str, bundle: Any, parts: tuple[str, str, str],
             case: dict[str, Any], markdown: str, rep: int) -> dict[str, Any]:
    """One extraction, judged exactly as the runner judges a first attempt."""
    out: dict[str, Any] = {"document_hash": case["document_hash"], "rep": rep,
                           "stratum": case["stratum"], "split": case["split"]}
    prompt = _render(parts, markdown, [], None)
    schema = (bundle.engine_emit_schema() if bundle.engine_emit_schema
              else emit_schema(bundle.schema_version))
    t0 = time.monotonic()
    try:
        res = engine.complete(prompt, schema, model)
    except EngineTransportError as exc:
        return {**out, "outcome": "transport_error", "errors": [str(exc)[:300]]}
    out.update(seconds=round(time.monotonic() - t0, 1), raw=res.raw_text,
               observed_model=res.observed_model,
               tokens=[res.input_tokens, res.output_tokens])
    return {**out, **judge(bundle, case["document_hash"], markdown, res.raw_text,
                           res.observed_model)}


def judge(bundle: Any, document_hash: str, markdown: str, raw: str,
          observed_model: str | None) -> dict[str, Any]:
    """A raw response's outcome, errors and profile under the CURRENT code."""
    try:
        emit = json.loads(raw)
        if not isinstance(emit, dict):
            raise ValueError("top level is not an object")
        emit = normalize_emit(emit, bundle.schema_version)
    except ValueError as exc:
        return {"outcome": "schema_invalid", "errors": [f"not JSON: {exc}"]}
    if errs := validate_emit(emit, bundle.schema_version):
        return {"outcome": "schema_invalid", "errors": errs[:20]}
    try:
        record = bundle.assemble(emit, markdown, document_hash=document_hash,
                                 normalizer_version=NORMALIZER_VERSION,
                                 observed_model=observed_model or "", at=iso(utcnow()))
    except AssembleError as exc:
        return {"outcome": "attribution_failed", "errors": exc.errors[:20]}
    report = bundle.verify(record, markdown)
    failed = report.status == "fail"
    return {"outcome": "attribution_failed" if failed else "ok",
            "errors": [f"{f.check}:{f.code} at {f.path}" for f in report.findings
                       if f.severity == "error"][:20],
            "profile": bundle.profile_of(record)}


def rejudge(run_dir: Path) -> Path:
    """Re-judge a run's saved raw responses under today's code: same model
    output, so a score difference is the code's alone. Writes a sibling run."""
    settings = Settings.load(env_snapshot())
    bundle = get_bundle(BUNDLE)
    rows = [json.loads(line) for line in
            (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    conn = _connect(settings)
    md = {r["document_hash"]: r["markdown"] for r in conn.execute(
        "SELECT document_hash, markdown FROM documents WHERE document_hash = ANY(%s)",
        (sorted({r["document_hash"] for r in rows}),)).fetchall()}
    conn.close()
    out_dir = run_dir.with_name(f"{run_dir.name}-rejudged-{bundle.validator_version}")
    out_dir.mkdir()
    meta = json.loads((run_dir / "meta.json").read_text())
    (out_dir / "meta.json").write_text(json.dumps(
        {**meta, "rejudged_from": run_dir.name,
         "validator": bundle.validator_version}, indent=1))
    with (out_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            if r.get("raw") is not None:
                kept = {k: r[k] for k in r if k not in ("outcome", "errors", "profile")}
                r = {**kept, **judge(bundle, r["document_hash"], md[r["document_hash"]],
                                     r["raw"], r.get("observed_model"))}
            fh.write(json.dumps(r) + "\n")
    print(json.dumps({"run": str(out_dir), "calls": len(rows)}))
    return out_dir


def run(variant: str, k: int, split: str, concurrency: int, limit: int | None) -> Path:
    settings = Settings.load(env_snapshot())
    bundle = get_bundle(BUNDLE)
    model = settings.l2_model_candidates[0]
    template = load_variant(variant)(bundle.template)
    parts = _split(template)
    cases = load_cases(split)[:limit]
    conn = _connect(settings)
    md = {r["document_hash"]: r["markdown"] for r in conn.execute(
        "SELECT document_hash, markdown FROM documents WHERE document_hash = ANY(%s)",
        ([c["document_hash"] for c in cases],)).fetchall()}
    conn.close()
    engine = CodexCli(reasoning_effort=settings.l2_reasoning_effort,
                      trust_requested_model=True, strict=settings.l2_schema_strict)
    run_id = f"{iso(utcnow()).replace(':', '').replace('-', '')}-{variant}-k{k}"
    out_dir = RUNS / run_id
    out_dir.mkdir(parents=True)
    (out_dir / "meta.json").write_text(json.dumps({
        "variant": variant, "k": k, "split": split, "bundle": BUNDLE, "model": model,
        "effort": settings.l2_reasoning_effort, "cases": len(cases),
        "template_sha": __import__("hashlib").sha256(template.encode()).hexdigest()[:12],
    }, indent=1))
    jobs = [(c, rep) for c in cases for rep in range(k)]
    done = 0
    with (out_dir / "results.jsonl").open("w", encoding="utf-8") as fh, \
            ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(one_shot, engine, model, bundle, parts, c,
                               md[c["document_hash"]], rep) for c, rep in jobs]
        for fut in futures:
            fh.write(json.dumps(fut.result()) + "\n")
            fh.flush()
            done += 1
            if done % 25 == 0:
                print(f"{done}/{len(jobs)}", file=sys.stderr, flush=True)
    print(json.dumps({"run": str(out_dir.relative_to(ROOT)), "calls": len(jobs)}))
    return out_dir


# --- score --------------------------------------------------------------------

# A doc-level measure is a function of the k results for one document; the
# run's score for it is the mean over documents. Quality measures read each
# sample on its own; stability measures compare the k samples to each other.


def _profiles(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r["profile"] for r in results if r.get("profile")]


def _facts(p: dict[str, Any], family: str) -> list[dict[str, Any]]:
    return [f for f in p["facts"]["entries"] if f["family"] == family]


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def q_valid(rs: list[dict[str, Any]]) -> float | None:
    return _rate(sum(r["outcome"] == "ok" for r in rs), len(rs))


def q_experience_parsed(rs: list[dict[str, Any]]) -> float | None:
    fs = [f for p in _profiles(rs) for f in _facts(p, "experience")]
    return _rate(sum(f["derived"]["state"] == "parsed" for f in fs), len(fs))


def q_pay_period(rs: list[dict[str, Any]]) -> float | None:
    ms = [f["derived"]["money"] for p in _profiles(rs) for f in _facts(p, "compensation")
          if f["derived"].get("money")]
    return _rate(sum(bool(m.get("period")) for m in ms), len(ms))


def q_pay_currency(rs: list[dict[str, Any]]) -> float | None:
    ms = [f["derived"]["money"] for p in _profiles(rs) for f in _facts(p, "compensation")
          if f["derived"].get("money")]
    return _rate(sum(bool(m.get("currency")) for m in ms), len(ms))


_PREFERRED = re.compile(r"\b(prefer|preferred|nice to have|bonus|a plus|plus\b|ideally|desired)",
                        re.I)


def q_preferred_marked(rs: list[dict[str, Any]]) -> float | None:
    """Of qualifications under a 'preferred'-style heading or with a 'plus'-
    style modality, the share whose demand claim says anything but contextual."""
    hit = tot = 0
    for p in _profiles(rs):
        for a in p["demand_profile"]["areas"]:
            if a["kind"] != "qualification":
                continue
            for c in a["claims"]:
                cue = f"{c.get('section_heading') or ''} {c.get('modality') or ''}"
                if _PREFERRED.search(cue):
                    tot += 1
                    hit += c.get("requirement", c.get("importance")) == "preferred"
    return _rate(hit, tot)


def q_requirement_labeled(rs: list[dict[str, Any]]) -> float | None:
    """Share of qualification areas that say required or preferred."""
    areas = [a for p in _profiles(rs) for a in p["demand_profile"]["areas"]
             if a["kind"] == "qualification"]
    return _rate(sum(a.get("requirement") in ("required", "preferred") for a in areas),
                 len(areas))


def _jaccard(a: set[Any], b: set[Any]) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def _pairwise(sets: list[set[Any]]) -> float | None:
    if len(sets) < 2:
        return None
    return statistics.fmean(_jaccard(a, b) for a, b in itertools.combinations(sets, 2))


def s_skills(rs: list[dict[str, Any]]) -> float | None:
    return _pairwise([{m.get("normalized_key") or m["surface"].lower() for m in p["mentions"]
                       if m["type"] == "skill"} for p in _profiles(rs)])


def s_statements(rs: list[dict[str, Any]]) -> float | None:
    return _pairwise([{(s["kind"], s["evidence"][0]["text"][:60]) for s in p["statements"]
                       if s["evidence"]} for p in _profiles(rs)])


def s_facts(rs: list[dict[str, Any]]) -> float | None:
    def key(f: dict[str, Any]) -> str:
        d = f["derived"]
        return json.dumps([f["family"], d["state"], d.get("money"), d.get("quantity")],
                          sort_keys=True)
    return _pairwise([{key(f) for f in p["facts"]["entries"]
                       if f["family"] in ("experience", "compensation")}
                      for p in _profiles(rs)])


def s_authorization(rs: list[dict[str, Any]]) -> float | None:
    vals = [(p["authorization"]["sponsorship"], p["authorization"]["citizenship_required"])
            for p in _profiles(rs)]
    return _pairwise([{v} for v in vals])


QUALITY: dict[str, Callable[[list[dict[str, Any]]], float | None]] = {
    "valid_first_shot": q_valid,
    "experience_parsed": q_experience_parsed,
    "pay_period": q_pay_period,
    "pay_currency": q_pay_currency,
    "preferred_marked": q_preferred_marked,
    "requirement_labeled": q_requirement_labeled,
}
STABILITY: dict[str, Callable[[list[dict[str, Any]]], float | None]] = {
    "stab_skills": s_skills,
    "stab_statements": s_statements,
    "stab_facts": s_facts,
    "stab_authorization": s_authorization,
}
METRICS = {**QUALITY, **STABILITY}


def per_doc(run_dir: Path) -> dict[str, dict[str, Any]]:
    """document_hash -> {split, stratum, metric: value-or-None}."""
    by: dict[str, list[dict[str, Any]]] = {}
    for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        by.setdefault(r["document_hash"], []).append(r)
    return {dh: {"split": rs[0]["split"], "stratum": rs[0]["stratum"],
                 **{m: fn(rs) for m, fn in METRICS.items()}} for dh, rs in by.items()}


def _mean(docs: list[dict[str, Any]], metric: str) -> float | None:
    xs = [d[metric] for d in docs if d[metric] is not None]
    return statistics.fmean(xs) if xs else None


def score(run_dir: Path) -> dict[str, Any]:
    docs = per_doc(run_dir)
    meta = json.loads((run_dir / "meta.json").read_text())
    out: dict[str, Any] = {"run": run_dir.name, "variant": meta["variant"], "k": meta["k"]}
    for split in ("train", "test", "all"):
        ds = [d for d in docs.values() if split == "all" or d["split"] == split]
        out[split] = {m: _mean(ds, m) for m in METRICS} | {"docs": len(ds)}
    stab = [out["all"][m] for m in STABILITY if out["all"][m] is not None]
    out["all"]["stability"] = statistics.fmean(stab) if stab else None
    outcomes: dict[str, int] = {}
    for line in (run_dir / "results.jsonl").read_text().splitlines():
        o = json.loads(line)["outcome"]
        outcomes[o] = outcomes.get(o, 0) + 1
    out["outcomes"] = outcomes
    return out


#: Every compare runs one interval per metric per split. At 95% each, a run
#: against itself crossed one of its 18 intervals by chance (2026-10-10 A/A
#: test), so the family is Bonferroni-corrected: 95% across all of them.
FAMILY_ALPHA = 0.05


def _boot(a: list[float], b: list[float], alpha: float, n: int = 10000,
          seed: int = 0) -> tuple[float, float]:
    """The (1 - alpha) interval of mean(b - a) over paired documents."""
    rng = random.Random(seed)
    diffs = [y - x for x, y in zip(a, b, strict=True)]
    means = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(n))
    return means[int(alpha / 2 * n)], means[min(n - 1, int((1 - alpha / 2) * n))]


def compare(base_dir: Path, cand_dir: Path) -> dict[str, Any]:
    a, b = per_doc(base_dir), per_doc(cand_dir)
    out: dict[str, Any] = {"baseline": base_dir.name, "candidate": cand_dir.name}
    verdicts = []
    for split in ("train", "test"):
        res: dict[str, dict[str, Any]] = {}
        for m in METRICS:
            pairs = [(a[d][m], b[d][m]) for d in a if d in b and a[d]["split"] == split
                     and a[d][m] is not None and b[d][m] is not None]
            if len(pairs) < 3:
                res[m] = {"n": len(pairs)}
                continue
            xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
            lo, hi = _boot(xs, ys, FAMILY_ALPHA / (2 * len(METRICS)))
            res[m] = {"n": len(pairs), "base": round(statistics.fmean(xs), 3),
                      "cand": round(statistics.fmean(ys), 3),
                      "delta": round(statistics.fmean(ys) - statistics.fmean(xs), 3),
                      "ci": [round(lo, 3), round(hi, 3)],
                      "sig": "up" if lo > 0 else "down" if hi < 0 else "flat"}
        out[split] = res
    for m in METRICS:
        tr, te = out["train"][m].get("sig"), out["test"][m].get("sig")
        if "down" in (tr, te):
            verdicts.append(f"{m}: regressed -> revert")
        elif tr == "up" and te == "up":
            verdicts.append(f"{m}: improved on train and test")
        elif tr == "up":
            verdicts.append(f"{m}: train only -> suspect overfitting")
    out["verdict"] = ("revert" if any("revert" in v for v in verdicts)
                      else "keep" if any("train and test" in v for v in verdicts)
                      else "no clear change")
    out["notes"] = verdicts
    return out


# --- cli ----------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    bc = sub.add_parser("build-cases")
    bc.add_argument("--per-stratum", type=int, default=10)
    bc.add_argument("--seed", type=int, default=7)
    r = sub.add_parser("run")
    r.add_argument("--variant", default="baseline")
    r.add_argument("--k", type=int, default=3)
    r.add_argument("--split", choices=("train", "test", "all"), default="all")
    r.add_argument("--concurrency", type=int, default=24)
    r.add_argument("--limit", type=int, default=None, help="first N cases only (smoke)")
    s = sub.add_parser("score")
    s.add_argument("run_dir", type=Path)
    rj = sub.add_parser("rejudge")
    rj.add_argument("run_dir", type=Path)
    c = sub.add_parser("compare")
    c.add_argument("baseline", type=Path)
    c.add_argument("candidate", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "build-cases":
        build_cases(args.per_stratum, args.seed)
    elif args.cmd == "run":
        run(args.variant, args.k, args.split, args.concurrency, args.limit)
    elif args.cmd == "rejudge":
        rejudge(args.run_dir)
    elif args.cmd == "score":
        print(json.dumps(score(args.run_dir), indent=1))
    else:
        print(json.dumps(compare(args.baseline, args.candidate), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
