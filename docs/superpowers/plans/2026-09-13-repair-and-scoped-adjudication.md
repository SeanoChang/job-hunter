# Repair + Scoped Adjudication (validator/18) Implementation Plan

> **For agentic workers:** implemented via a Workflow (implementation agents +
> adversarial verification), task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Take the review+quarantine share from ~26% to <5%: scoped
adjudication and incomplete-cohort adjudication clear the disagreements whose
audits are clean where it matters, and `semantic-repair/v1` fixes the
~75% of review docs whose candidates are genuinely defective.

**Architecture:** Validator 17→18 (settlement policy changes again — bump,
never edit in place). The dispute set becomes CODE: medoid-namespaced
unaligned statements plus sibling-cited blocks the medoid never cites — the
2026-09-13 adversarial verifier's method, which refuted naive statement-id
overlap. Repair is a new pure module mirroring audit.py (typed operations,
old-hash guarded, one round + one re-audit per spec §5), driven by a runner
phase between audit and settlement. A bundle-aware re-settle path replays
validator-17 archives under 18 with zero extraction calls, carrying audit
artifacts forward by candidate hash.

**Spec:** `docs/superpowers/specs/2026-09-07-parsing-contract-v2-design.md`
(§4 Repair, §5 flow: "one semantic repair round and one subsequent audit",
§6). Evidence base: the 2026-09-13 investigation (161 disagreement docs: 121
blocking-on-disputed, ~17–39 scoped-clearable; 43 incomplete: all
attribution-budget; 26 quarantined: no typo-fold fixes exist).

## Global constraints

- Frozen identifiers: validator "18"; `REPAIR_VERSION = "semantic-repair/v1"`;
  `AUDIT_VERSION` stays `semantic-audit/v2` (unchanged bytes). v1 untouched.
- Pure modules in `l2/v2/`: no I/O, no model calls, no env.
- Spec §5/§6: max one repair round + one re-audit; a failed repair never
  erases the base candidate; no publication mid-phase; audit output is never
  an extraction sample; human dispositions outrank every automated phase.
- `mypy --strict`, ruff 100; tests offline/deterministic; TDD (watch fail).

## Policy (normative, validator/18 — deltas over 17)

Dispute set for a disagreeing cohort (code-owned, from the verified method):
1. Align each non-medoid sample's claims to the medoid's (`agreement`
   alignment); every medoid statement left unaligned in ANY medoid-involving
   pair is disputed, **in the medoid's id namespace** (ids are per-sample and
   never comparable across samples).
2. Every block id cited by a sibling sample's statement evidence but cited by
   ZERO medoid statements is a disputed block (the omission side of an f1
   statement-set disagreement).
3. Gate-dimension map: `importance` gate ⇔ `importance` findings; `negation`
   gate ⇔ `polarity_subject`; `f1` gate ⇔ `omission`/`unsupported_statement`/
   `relationship`.

A blocking audit finding **touches the dispute** iff any of its targets is a
disputed statement id or disputed block id, OR its code maps to a failed
gate's dimension per rule 3. Scoped adjudication: disagreeing cohort +
completed audit + zero blocking findings touching the dispute ⇒ `validated`,
`sampling: "adjudicated"` (off-dispute blocking findings still gate
`search_eligible`, exactly as for agreeing cohorts). Incomplete cohort
(`sample_failed`) + completed audit with zero blocking findings anywhere ⇒
`validated`, `sampling: "adjudicated"` (conservative: whole-record clean).
Audit `audit_error` ⇒ re-audit once on a later pass before the doc settles
terminal; two consecutive audit errors ⇒ needs_review as today. Human
dispositions unchanged and always senior.

Repair trigger (before terminal settlement only, spec §6): a cohort that
would settle `needs_review` from disagreement whose audit has blocking
findings touching the dispute, OR a `validated` record with blocking findings
(eligibility repair). One repair round: render repair prompt (source +
candidate + validated findings), apply typed ops, re-verify, re-audit;
repaired candidate settles under the same rules with `parent_candidate_hash`
set. Repair failure (invalid ops, stale hash, worse audit) ⇒ base candidate
settles as it would have.

---

### Task 1: dispute sets + validator/18 settlement policy

**Files:** Modify `src/jobhunter/l2/agreement.py` (alignment provenance),
`src/jobhunter/l2/state.py`, `src/jobhunter/l2/v2/facts.py` (17→18 +
history line), `src/jobhunter/l2/bundles.py`. Tests: `tests/l2/test_state.py`,
`tests/l2/test_agreement.py`.

**Interfaces (produces):**
```python
# agreement.py
def dispute_set(medoid_record, sibling_records) -> Dispute
    # Dispute(statement_ids: frozenset[str], block_ids: frozenset[str])
    # rule 1 + rule 2 above; pure, no thresholds of its own
# state.py
GATE_DIMENSION_CODES = {"importance": ("importance",),
    "negation": ("polarity_subject",),
    "f1": ("omission", "unsupported_statement", "relationship")}
def audit_touches_dispute(findings, dispute, failed_gates) -> bool
# AuditView gains: findings (list with code/severity/targets); derive_state
# consults dispute via a records hook it already has access to (cohort_hook
# loads the records; thread the loaded records to the dispute computation —
# do NOT reload from the archive twice).
```

- [ ] Failing tests first: dispute_set on hand-built records (unaligned
      medoid statement ids; sibling-only block; ids NOT compared across
      namespaces — regression for the verifier's 7b354fd4 class);
      audit_touches_dispute per rule 3 (each gate); the five verifier
      refutation shapes as fixtures (0df0f921 target-overlap, 9d59cb88
      block-id omission, 7b354fd4 namespace, 27c9a9af dimension-gate,
      7f4303c2 omission-restates-dispute); policy rows: scoped clear ⇒
      adjudicated; touching ⇒ needs_review; incomplete + clean-everywhere ⇒
      adjudicated; incomplete + any blocking ⇒ needs_review; no-hook v1
      unchanged.
- [ ] Implement; targeted green; full gate green. Commit.

### Task 2: audit-error re-audit + repair trigger plumbing

**Files:** Modify `src/jobhunter/l2/runner.py` (audit retry pass; the
"needs repair" decision surfaced from the settle fold), tests
`tests/l2/test_runner_v2.py`.

- [ ] Failing tests: an `audit_error` artifact does not settle the doc
      terminal — the next run re-audits (new artifact beside the old) and
      then settles; two errors ⇒ needs_review; the repair trigger fires for
      blocking-on-dispute cohorts and for validated-with-blocking records,
      and NOT for human-touched docs.
- [ ] Implement; green; commit.

### Task 3: `semantic-repair/v1` — the pure repair contract

**Files:** Create `src/jobhunter/l2/v2/repair.py`,
`tests/l2/v2/test_repair.py`.

**Interfaces (produces):**
```python
REPAIR_VERSION = "semantic-repair/v1"
TEMPLATE: str  # spec §4 repair text verbatim + scaffold (source blocks,
               # candidate hash, candidate JSON, validated findings JSON)
def render(markdown, candidate_hash, record, findings) -> str
def emit_schema() -> dict   # typed ops, closed op/object kinds, explicit
                            # "type" everywhere (strict-mode contract)
class RepairJudgeError(Exception)  # all defects at once
def apply(emit, record, markdown, candidate_hash) -> dict
    # validates: base hash matches; op targets exist; old_object_hash matches
    # for replace/remove; no duplicate/conflicting ops; immutable fields
    # (document/extraction/provenance/quality) untouched; every op carries
    # finding_id + evidence + reason; removals cite evidence. Then rebuilds:
    # rebind all evidence (source.resolve), re-derive facts, reconcile
    # presence, recompute candidate_hash, set parent_candidate_hash.
    # Any violation raises RepairJudgeError — a failed repair never mutates.
```
Op kinds: add/replace/remove over statements, relations (groups/conditions/
example_sets), fact entries, mentions, areas; replace-only for presence
objects, source_assessment, accounting entries. Reuse assemble's binder and
derivation — do not duplicate grammar.

- [ ] Failing tests: template pins (spec sentences: "Difficulty parsing is
      not a reason to remove supported information.", "Do not change document
      identity, versions, provenance, or quality assessments."), each
      validity rule rejects, happy-path add/replace/remove rebind + re-derive
      + new hash + parent link, control-chars rejected (validator/17 rule
      reused), conflicting-ops reject, stale base rejects.
- [ ] Implement; green; commit.

### Task 4: runner repair phase + bundle-aware re-settle campaign

**Files:** Modify `src/jobhunter/l2/runner.py`, `src/jobhunter/l2/rebuild.py`
(bundle-awareness — the standing PFEE gap), `src/jobhunter/archive/keys.py`
(repair artifact key beside audit's). Tests: `tests/l2/test_runner_v2.py`,
`tests/l2/test_rebuild.py`.

Runner flow per doc (extends the audit phase): audit blocking-and-triggering
⇒ ONE repair engine call ⇒ archive repair artifact (raw ops + applied result
or the judge error) ⇒ `apply` ⇒ re-verify ⇒ ONE re-audit ⇒ settle whichever
candidate the policy picks (repaired if it verifies and audits no-worse,
else base). Archive-first throughout; repair output never counts as a sample.

Re-settle campaign: `rebuild`-path replay of validator-17 attempts under 18
with zero extraction calls — re-assemble each archived raw_response under the
v2 bundle (assembly unchanged 17→18, so candidates and hashes reproduce),
carry audit artifacts forward by **candidate hash** (scan the doc's audit
artifacts; attempt-key derivation misses because attempt keys are new), then
settle under 18. New repair/audit calls happen only where the policy asks.

- [ ] Failing tests: scripted-engine repair round (blocking finding ⇒ repair
      ⇒ clean re-audit ⇒ validated+eligible with parent hash); failed repair
      settles the base; one-round cap enforced; replay determinism (17
      attempts re-settled under 18 reproduce candidate hashes and reuse the
      carried audit — zero engine calls); catch-up folds repaired docs
      identically.
- [ ] Implement; green; full gate; commit.

### Task 5: live measurement (run by the operator, not the workflow)

- [ ] Re-settle the ~1,390 validator-17 docs under 18 (replay, no extraction
      cost); report the review/quarantine share movement with exact
      denominators.
- [ ] Run the repair phase over the remaining review docs and the
      validated-with-findings backlog; report eligibility movement and
      spot-check ≥5 repaired records' artifacts (human criterion).

## Non-goals

- Quarantine reduction (2.6%, already under target; no typo-fold fixes exist
  per the verified investigation).
- F1 threshold recalibration and containment-aware alignment (needs the
  adjudicated comparator this increment produces).
- The views.py served-tuple cutover (separate standing work).
