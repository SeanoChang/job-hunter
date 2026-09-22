"""The v20 completion criterion: a live codex-cli drain of N fresh documents.

The offline half of the bump is provable offline — the case corpus, the
validator, the migration replay. What nothing offline can prove is that the
contract survives a real posting read by a real model: that the prompt asks for
a modal phrase and gets a quote, that the auditor's v4 vocabulary matches what
the extractor now emits, that sampling really is monitoring, and that no code
path still writes the field schema 3 retired. So the plan's completion criterion
is a drain, and this is it.

    uv run python scripts/live_gate_v20.py --docs 25 --dry-run   # selection only
    uv run python scripts/live_gate_v20.py --docs 25             # the gate

Documents are FRESH: the runner's own queue under the active tuple, which after
the migration replay holds only documents nothing has ever extracted. The drain
runs at `--max-usd 0`, which is not a trick — codex-cli reports no per-call cost,
so the budget gate never trips and the run is bounded by `--docs` alone.

Six checks, the first of them over the drain itself and the rest over the rows
it wrote — all of them things the v20 bump is supposed to have changed
(spec §2.1, §4, §6):

    drained             the drain ran, and took the documents it was asked for
    terminal            every selected document settled to a status
    audit_machinery     no candidate's audit came back `error`
    k_samples_on_slot   only the deterministic 5% slot took extra samples
    record_shape        every statement carries `section_heading` and a
                        `modality_evidence` that is null or a QUOTED reference
    no_importance       no served record carries a verdict field anywhere

`drained` is there because every other check is a "nothing went wrong in this
list" predicate, and all of them are vacuously true of an empty list. The
runner returns an empty queue for reasons that are the operator's normal
situation rather than an error — the extract writer lock held by a drain that
is already running, a provider throttling the re-audit pass before the queue is
ever built, a short fresh queue — and without this check the gate printed five
`ok` lines and exited 0 having called no engine at all. A predicate that cannot
fail for doing nothing cannot discharge ac-3.

`record_shape` and `no_importance` read the stored record, which is the blob
minus `demand_profile`: that key is v1's claim index by contract
(`l2/v2/serve.claim_index`) and its areas carry `importance: contextual` for a
schema-3 record because the agreement gate and the legacy renderers read that
shape. Reading it as a verdict would fail every clean drain.

The gate never writes anything of its own; the drain writes through the runner,
under the extract writer lock, exactly as `extract run` does.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from dataclasses import dataclass
from typing import Any

import psycopg

from jobhunter.l2.bundles import get_bundle

ACTIVE = get_bundle("v2")
TUPLE = (ACTIVE.prompt_version, ACTIVE.schema_version, ACTIVE.validator_version)
#: statuses a fold can leave behind; anything else (no row at all) is pending
TERMINAL = ("validated", "needs_review", "quarantined", "rejected")
#: what a completed audit says; `error` is the machinery failing, never a verdict
AUDIT_ERROR = "error"
#: the blob key that is v1's claim index rather than the schema-3 record
LEGACY_INDEX = "demand_profile"
#: the fields parsing contract v3 removed from a statement (spec §2.1)
RETIRED_FIELDS = ("importance", "importance_evidence", "proficiency", "proficiency_evidence")

Conn = psycopg.Connection[dict[str, Any]]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class GateResult:
    selected: list[str]
    checks: list[Check]
    plan: str
    summary: dict[str, Any]


def on_audit_slot(document_hash: str, audit_mod: int) -> bool:
    """The runner's own 5% slot rule (`runner._extract_doc`), restated: a
    document takes extra samples when its hash lands on the slot, and
    `audit_mod <= 1` means every document does."""
    return audit_mod <= 1 or int(document_hash[:8], 16) % audit_mod == 0


def _record_of(profile: dict[str, Any] | None) -> dict[str, Any]:
    """The schema-3 record inside a stored blob: everything but the legacy index."""
    return {k: v for k, v in (profile or {}).items() if k != LEGACY_INDEX}


def _keys_anywhere(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = set(value)
        for nested in value.values():
            found |= _keys_anywhere(nested)
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for nested in value:
            found |= _keys_anywhere(nested)
        return found
    return set()


def _shape_defects(row: dict[str, Any]) -> list[str]:
    """What is wrong with one row's statements, in the contract's own words."""
    dh = row["document_hash"][:12]
    defects: list[str] = []
    for statement in (row.get("profile") or {}).get("statements") or []:
        sid = statement.get("id")
        if "section_heading" not in statement:
            defects.append(f"{dh}/{sid}: no section_heading")
        if "modality_evidence" not in statement:
            defects.append(f"{dh}/{sid}: no modality_evidence")
            continue
        refs = statement["modality_evidence"]
        if refs is None:
            continue  # null is the contract's answer for "the posting said nothing"
        if not isinstance(refs, list) or not refs:
            defects.append(f"{dh}/{sid}: modality_evidence is neither null nor a reference")
            continue
        for ref in refs:
            if not isinstance(ref.get("text"), str) or not ref["text"]:
                defects.append(f"{dh}/{sid}: modality_evidence carries no quote")
    return defects


#: the summary counters that each mean "the drain did not happen as asked"
NOT_DRAINED = (
    ("lock_held", "the extract writer lock is held; a drain is already running"),
    ("throttled", "the provider throttled the run"),
    ("breaker_abort", "the breaker aborted the run"),
    ("aborted", "the run aborted"),
)


def drained_check(selected: list[str], summary: dict[str, Any], *, docs: int) -> Check:
    """Did the drain the gate asked for actually happen?

    `runner.run` reports an empty queue the same way whether the corpus had no
    fresh documents or the run never started, so this reads the summary's own
    counters first and says which one it was. A short queue is a failure too:
    ac-3 is "25 fresh docs", and a gate that passes on three has measured three.
    """
    extraction = summary.get("extraction") or {}
    stopped = [reason for key, reason in NOT_DRAINED if extraction.get(key)]
    if stopped:
        return Check("drained", False, "; ".join(stopped))
    if len(selected) < docs:
        return Check(
            "drained", False,
            f"{len(selected)}/{docs} document(s) drained"
            + ("; the fresh queue is empty" if not selected else ""),
        )
    return Check("drained", True, f"{len(selected)}/{docs} document(s) drained")


def gate_checks(
    selected: list[str],
    rows: list[dict[str, Any]],
    *,
    audit_mod: int,
    docs: int,
    summary: dict[str, Any] | None = None,
) -> list[Check]:
    """The six checks, over the drain and the rows it left.

    Pure, so the predicates are testable without a drain: `rows` are
    `extractions` rows under the active tuple, keyed by `document_hash`.
    """
    by_doc = {row["document_hash"]: row for row in rows}

    missing = [dh[:12] for dh in selected if by_doc.get(dh, {}).get("status") not in TERMINAL]
    errored = [
        row["document_hash"][:12]
        for row in rows
        if AUDIT_ERROR in (
            str((row.get("profile") or {}).get("quality", {}).get("semantics")),
            str((row.get("profile") or {}).get("quality", {}).get("completeness")),
            str((row.get("flags") or {}).get("audit")),
        )
    ]
    off_slot = [
        row["document_hash"][:12]
        for row in rows
        if (row.get("k") or 1) > 1 and not on_audit_slot(row["document_hash"], audit_mod)
    ]
    defects = [defect for row in rows for defect in _shape_defects(row)]
    verdicts = sorted({
        f"{row['document_hash'][:12]}:{retired}"
        for row in rows
        for retired in RETIRED_FIELDS
        if retired in _keys_anywhere(_record_of(row.get("profile")))
    })
    sampled = [row["document_hash"][:12] for row in rows if (row.get("k") or 1) > 1]
    return [
        drained_check(selected, summary or {}, docs=docs),
        Check("terminal", not missing,
              f"{len(selected) - len(missing)}/{len(selected)} terminal"
              + (f"; pending: {', '.join(missing[:5])}" if missing else "")),
        Check("audit_machinery", not errored,
              f"{len(errored)} audit error(s)"
              + (f": {', '.join(errored[:5])}" if errored else "")),
        Check("k_samples_on_slot", not off_slot,
              f"{len(sampled)} sampled cohort(s), {len(off_slot)} off the 5% slot"
              + (f": {', '.join(off_slot[:5])}" if off_slot else "")),
        Check("record_shape", not defects,
              f"{len(rows)} record(s) checked, {len(defects)} defect(s)"
              + (f": {'; '.join(defects[:5])}" if defects else "")),
        Check("no_importance", not verdicts,
              f"{len(verdicts)} retired field(s)"
              + (f": {', '.join(verdicts[:5])}" if verdicts else "")),
    ]


def verdict(checks: list[Check]) -> str:
    return "PASS" if all(c.ok for c in checks) else "FAIL"


def settled_rows(conn: Conn, documents: list[str]) -> list[dict[str, Any]]:
    """The active tuple's rows for these documents, in selection order."""
    if not documents:
        return []
    rows = conn.execute(
        "SELECT document_hash, status, k, profile, flags FROM extractions"
        " WHERE prompt_version=%s AND schema_version=%s AND validator_version=%s"
        "   AND document_hash = ANY(%s)",
        (*TUPLE, list(documents)),
    ).fetchall()
    order = {dh: i for i, dh in enumerate(documents)}
    return sorted(rows, key=lambda r: order.get(r["document_hash"], len(order)))


def gate(
    settings: Any,
    conn: Conn,
    store: Any,
    *,
    engine: Any,
    docs: int,
    dry_run: bool,
) -> GateResult:
    """Select, drain, check. The runner owns the lock, the archive and the writes."""
    from jobhunter.l2.runner import run

    summary = run(settings, conn, store, engine=engine, max_docs=docs, max_usd=0.0,
                  dry_run=dry_run, bundle=ACTIVE)
    selected = list(summary.queued)
    payload = {"extraction": summary.to_dict()}
    # a dry run takes the writer lock too, so it knows the same thing the real
    # one does and must say it: "would drain 0 fresh documents" reads like an
    # empty corpus when what it means is that a drain is already running
    stopped = drained_check(selected, payload, docs=docs)
    plan = (
        f"would drain {len(selected)} fresh document(s) under "
        f"{'/'.join(TUPLE)} through {engine.name} at max-usd 0"
        + ("" if stopped.ok else f" -- WARNING: {stopped.detail}")
    )
    if dry_run:
        return GateResult(selected, [], plan, {"selected": selected, **payload})
    conn.commit()
    checks = gate_checks(selected, settled_rows(conn, selected),
                         audit_mod=settings.l2_audit_mod, docs=docs, summary=payload)
    return GateResult(
        selected, checks, plan,
        {
            "tuple": "/".join(TUPLE),
            "documents": len(selected),
            "verdict": verdict(checks),
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
            **payload,
        },
    )


def render(result: GateResult) -> str:
    lines = [result.plan] if not result.checks else []
    lines += [f"{'ok  ' if c.ok else 'FAIL'}  {c.name}: {c.detail}" for c in result.checks]
    if result.checks:
        lines.append(verdict(result.checks))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from jobhunter.archive import open_store
    from jobhunter.config import Settings
    from jobhunter.l2.engines import CodexCli
    from jobhunter.store import db

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=int, default=25, help="how many fresh documents to drain")
    parser.add_argument("--dry-run", action="store_true",
                        help="select the documents and print the plan; call no engine")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)

    settings = Settings.load()
    if settings.l2_engine != "codex-cli":
        print(f"the v20 live gate runs on codex-cli; JOB_HUNTER_L2_ENGINE is "
              f"{settings.l2_engine!r}")
        return 3
    conn = db.connect(settings.require_database_url())
    try:
        engine = CodexCli(
            reasoning_effort=settings.l2_reasoning_effort,
            trust_requested_model=settings.l2_trust_requested_model,
            strict=settings.l2_schema_strict,
        )
        result = gate(settings, conn, store=open_store(settings.archive_url), engine=engine,
                      docs=args.docs, dry_run=args.dry_run)
        print(json.dumps(result.summary, indent=2) if args.json else render(result))
        return 0 if args.dry_run or verdict(result.checks) == "PASS" else 1
    finally:
        with contextlib.suppress(Exception):
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
