# Correctness-First Review Pipeline (validator/19, audit/v3, prompt v10) Implementation Plan

> **For agentic workers:** implemented via a Workflow (implementation agents +
> adversarial verification), task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the silent-correctness holes before optimizing queue
clearance: wrong values that pass verification, coverage claims without
evidence, retries that regenerate instead of edit, an audit that suppresses
real omissions and cannot recover from its own failures, and an agreement
gate blind to v2's semantic fields. Completion is gated on a **live codex-cli
drain passing end-to-end**, not on fixtures.

**Architecture:** The source → candidate → validation → audit/repair →
publication boundaries stay exactly as they are; this increment makes the
implementations enforce their stated contracts. Frozen identifiers bump,
never edit in place: validator 18→19 (derivation, accounting, agreement
comparator), `semantic-audit/v2`→`v3` (omission triage, no hash echo),
prompt v9→v10 (retry carries the prior candidate; content rules). Audit
artifacts become version-keyed so an audit bump can actually re-audit.
Evaluation precedes any backfill: archived emits replay offline under the
corrected validator against both the failed set and a validated control,
tracking false acceptance alongside clearance.

**Spec:** `docs/superpowers/specs/2026-09-07-parsing-contract-v2-design.md`
(§3 importance, §4 auditor/repair, §5 flow, §6 comparator). Evidence base:
`docs/2026-09-14-review-quarantine-failure-analysis.md` plus the 2026-09-15
external adversarial review (six confirmed findings, all re-verified against
this tree; probes: unit `12+`+`years` → count≥12 passes verify; accounting
accepts a travel requirement assigned to an unrelated sales statement;
omission downgraded when an English requirement shares a block with EEO
text; `all_of` vs `any_of` scores F1=1.0).

## Global constraints

- Frozen identifiers: `VALIDATOR_VERSION = "19"`, `AUDIT_VERSION =
  "semantic-audit/v3"`, `PROMPT_VERSION` v10, each with its history-comment
  line appended, no earlier line edited. v1 and validator ≤18 artifacts
  untouched and still readable.
- Null-over-guess governs every derivation change: an unrecognized unit
  anchor yields `present_unparsed`, never a guessed dimension.
- Human dispositions outrank every automated phase, including re-audits
  under a new audit version. No publication mid-phase. Archive-first,
  write-once; a new audit version writes NEW keys, never overwrites.
- `assemble` and `verify` derivation mappings stay frozen together: the
  existing family-by-family agreement test must cover the unit path.
- `mypy --strict`, ruff 100; tests offline/deterministic; TDD — every
  behavior change lands with a test watched failing first.
- No bulk backfill, no bulk review-retry campaign, and no corpus migration
  in this plan: Task 6 measures; migration is a separate decision on its
  numbers.

## Fix order (normative)

1. Incorrect passes first (Tasks 1–2): unit derivation in both paths,
   evidence-backed accounting, omission severity.
2. Repair and audit recovery (Tasks 3–4): versioned audit attempts with
   selection/replay rules; retries that carry and edit the prior candidate.
3. Contract alignment (Task 5): prompt importance/proficiency/relations
   rules; agreement comparator over v2 semantic fields.
4. Evaluate before backfilling (Task 6): offline replay with false-acceptance
   tracking, then the live codex-cli gate.

### Task 1: validator/19 — unit derivation in both paths + evidence-backed accounting

**Files:** `src/jobhunter/l2/v2/facts.py`, `src/jobhunter/l2/v2/assemble.py`,
`src/jobhunter/l2/v2/verify.py`, `tests/l2/v2/test_facts.py`,
`tests/l2/v2/test_assemble.py`, `tests/l2/v2/test_verify.py`.

**Interfaces:** `derive_quantity(value_text, comparison_text,
unit_text: str | None = None)` — `unit_text` (the cited unit anchor, joined
like `_cited`/`_texts` do) is consulted only when the value span itself
carries no `_UNIT` match; an unrecognized `unit_text` word forces `None`
(present_unparsed), never a count. Both call sites forward it:
`assemble._derive` (the experience/quantity branch, mirroring what the
compensation branch already does with currency/period) and
`verify._rederive`.

- [ ] Step 1: failing tests — the external probe verbatim: emit citing
  value `12+` with unit anchor `years` must derive
  `{"dimension":"duration","unit":"month","min_value":144}` through assemble
  AND be re-derived identically by verify (today both return a dimensionless
  count and verification passes). Add: unit anchor `pounds` (unknown word) →
  `present_unparsed`; unit in value span still wins; family agreement test
  extended to the unit argument.
- [ ] Step 2: implement `derive_quantity` unit parameter + both forwards.
- [ ] Step 3: failing tests — accounting: a `statements`-disposition row
  whose ref'd objects carry zero evidence refs with that row's `block_id`
  is a new content error `accounting:coverage_unevidenced` (the probe: a
  travel-requirement block assigned to an unrelated sales statement must
  fail). Same rule for `fact_entry` refs. A row whose ref'd statement DOES
  cite the block stays valid — partial-clause omissions remain the audit's
  question, not this check's.
- [ ] Step 4: implement in `_check_accounting`; widen the
  requirement-language tripwire to `disposition == "context"` blocks as a
  warning (`context_requirement_language`), pointed at the auditor exactly
  like the excluded-block one.
- [ ] Step 5: bump `VALIDATOR_VERSION` to "19" with its history line;
  register in `bundles.py` (v2 keeps `compat_validators` for fold
  continuity: 17 and 18 attempts assemble byte-identically only where the
  emit cited no separate unit anchor — document in the history comment that
  19 re-derives, so replayed 17/18 attempts fold under 19 by RE-JUDGING, as
  `rebuild.py` already does across 17→18).
- [ ] Step 6: full gate green: `uv run pytest -q && uv run ruff check . &&
  uv run mypy`. Commit.

### Task 2: semantic-audit/v3 — omission triage that proves capture, no hash echo

**Files:** `src/jobhunter/l2/v2/audit.py`, `tests/l2/v2/test_audit.py`.

**Interfaces:** `AUDIT_VERSION = "semantic-audit/v3"`. `_severity` gains the
candidate record (or a prebuilt object→cited-blocks index) so triage can
check evidence, with rules:

- Downgrade an omission to warning **only** when at least one target is a
  candidate object whose own evidence cites the finding's block (the auditor
  located that block's content in the candidate — genuine granularity).
  A target merely existing in the candidate proves nothing and no longer
  downgrades.
- Boilerplate downgrade only when the cited block matches
  `_BOILERPLATE_MARKERS` **and** `_REQUIREMENT_LANGUAGE` does NOT match it:
  a block mixing EEO text with an English-proficiency requirement stays
  blocking (the external probe).
- Triage still only lowers; no other code's severity changes.
- The emit schema and prompt drop the `candidate_hash` echo entirely;
  `judge()` drops the comparison — the binding is code-owned (the caller
  already passes `candidate_hash` for artifact keying only).

- [ ] Step 1: failing tests — both external probes as fixtures (unrelated
  target stays blocking; EEO+requirement mixed block stays blocking), plus
  the v2 behaviors that must survive: true granularity (target cites the
  block) still warns, pure-boilerplate block still warns, non-omission codes
  never lowered. A hash-echo-free emit validates; judge no longer raises on
  a garbled hash field (field gone from schema).
- [ ] Step 2: implement; bump `AUDIT_VERSION` with a history comment naming
  both defects (suppression rules, model-transcribed hash bookkeeping).
- [ ] Step 3: full gate green. Commit.

### Task 3: version-keyed audit attempts + selection/replay rules

**Files:** `src/jobhunter/archive/keys.py`, `src/jobhunter/l2/runner.py`,
`tests/archive/test_keys.py`, `tests/l2/test_runner_v2.py`.

**Interfaces:** `keys.x_audit_key(attempt_key, audit_version)` — the version
becomes a key segment (e.g. `…-s1a2.a3.json.gz` for v3); the bare legacy key
is, by definition, a `semantic-audit/v2` artifact and stays readable.
Selection rule everywhere the runner probes audits (`_audit_view`, the
re-audit queue, `_repair_audit_key`, settle's audit read): an artifact
satisfies the active tuple only if its audit version equals
`bundle.audit_version`; an older-version artifact means "audit owed", not
"audit done". Replay/campaign rule: a doc whose only audit is older-version
enters the re-audit queue exactly like `audit_error` does today — bounded by
the same per-pass budget, never a blanket sweep. Human dispositions
(`reviewed_by` set) are senior: re-audit may write artifacts but never
reopens a human-settled row.

- [ ] Step 1: failing tests — a doc with only a v2 audit artifact under a
  v3 bundle re-audits (engine called once, new `.a3` key written beside the
  old, old artifact untouched); a doc with a v3 artifact is skipped; a
  human-dispositioned row is never re-opened by the pass; `_repair_audit_key`
  composes with the version segment.
- [ ] Step 2: implement keys + selection; thread `bundle.audit_version`
  through every audit probe in the runner (the `store.exists` skip today at
  the audit phase and both repair-side probes).
- [ ] Step 3: full gate green. Commit. (This is what makes Task 2
  recoverable for the 61 machinery-blocked docs: restored audits mean they
  can be ASSESSED — validated is not presumed.)

### Task 4: retries carry the prior candidate and validate the edit

**Files:** `src/jobhunter/l2/v2/prompt.py`, `src/jobhunter/l2/runner.py`,
`tests/l2/v2/test_prompt.py`, `tests/l2/test_runner_v2.py`.

**Interfaces:** `render(markdown, prior_errors, prior_emit: str | None =
None)` (bundle `render` signature widens for v2; v1 untouched). When
`prior_emit` is parseable JSON, the retry block includes it verbatim after
the source fence (same injection-order discipline as `prior_errors`) with
the instruction: return the SAME JSON, minimally edited to fix ONLY the
listed errors; do not remove or rewrite anything the errors do not name.
When it is not usable (JSON/schema failure), today's behavior stands. The
runner keeps the failed attempt's `raw_response` and passes it to the next
render; after a retry, a code-owned delta check flags top-level objects
(statements, relations groups/conditions/example_sets, facts entries,
mentions) that were valid in the prior emit, are absent in the retry, and
are named by no fed error — `retry:unexplained_deletion`, a content error
fed to the next rung like any other.

- [ ] Step 1: failing tests — retry prompt bytes contain the prior emit and
  the preservation instruction; unparseable prior emit falls back cleanly;
  the delta check fires on a scripted engine that deletes populated
  `relations` on retry (the confirmed retry-collapse shape) and stays silent
  when the retry only edits the erroring paths.
- [ ] Step 2: implement prompt block + runner threading + delta check.
  `PROMPT_VERSION` bumps to v10 here (bytes change) — coordinate with
  Task 5, which edits the same template: land both in one version bump,
  Task 4 first on a shared branch if implemented separately.
- [ ] Step 3: full gate green. Commit.

### Task 5: prompt v10 content + agreement over v2 semantic fields

**Files:** `src/jobhunter/l2/v2/prompt.py`, `src/jobhunter/l2/v2/serve.py`,
`src/jobhunter/l2/agreement.py`, `tests/l2/v2/test_prompt.py`,
`tests/l2/test_agreement.py`.

**Prompt (same v10 bump as Task 4):**
- Transcribe spec §3's importance disambiguation verbatim (heading-strength
  rule; when neither modal keyword nor heading strength exists, `ambiguous`
  is the correct label — never a guessed binary; `importance_evidence` must
  quote modality wording or the heading, never the clause's own descriptor).
- Conservative proficiency: the enum only from genuine taxonomy wording
  ("fluency", "expert-level"); strength adjectives ("strong", "advanced",
  "extensive") leave proficiency null.
- A populated relations/example_sets worked few-shot: one enumerated
  alternatives list ("Qt, Cocoa, React, Angular, or similar") linking EVERY
  named item, one `any_of` group, one condition — plus the negative example
  (same-topic statements at different tiers are NOT a group).
- A `block_accounting` example showing legal ref types and naming the
  illegal ones (mention/area ids) — the single largest historical error
  bucket (1,659 instances).

**Agreement (validator/19, same bump as Task 1):** the claim surface grows
the spec §6 fields — statement kind, polarity target, scoped fact values,
alternatives/operator, entity links — carried through
`serve.profile_of`'s claim index and compared in `agreement` as dimension
checks alongside importance/negation (not folded into F1; thresholds
unchanged, no recalibration in this plan). The external refutation case is
the fixture: two records identical except `all_of` vs `any_of` on aligned
claims must NOT pass.

- [ ] Step 1: failing tests — template pins for each new prompt rule;
  agreement fixtures: the all_of/any_of pair fails, kind flip fails,
  scoped-value disagreement (144 vs 12 months) fails, identical records
  still pass at F1 1.0.
- [ ] Step 2: implement; `PROMPT_VERSION` v10 history line notes both
  tasks' changes; serve/agreement changes ride validator "19"'s line.
- [ ] Step 3: full gate green. Commit.

### Task 6: evaluate before backfilling — offline replay, then the live codex-cli gate

**Files:** `scripts/` (evaluation script), no `src/` changes expected.
Operator-run with workflow support; nothing lands in the store from the
offline half.

- [ ] Step 1 (offline replay, zero engine calls): re-judge archived raw
  emits under validator 19 for (a) the 475-doc failed set and (b) a 200-doc
  validated control sample. Report against exact denominators:
  **false acceptance** (control records that passed 18 and fail a 19 check —
  each is a previously silent wrong value or unevidenced coverage claim, i.e.
  a retained obligation caught), clearance movement on the failed set, and
  per-check deltas (`coverage_unevidenced`, unit derivation flips,
  comparator dimension failures). No DB writes; numbers reviewed before any
  migration decision.
- [ ] Step 2 (live gate — **the completion criterion**): a real codex-cli
  drain must pass end-to-end on fresh documents:
  `JOB_HUNTER_L2_BUNDLE=v2 JOB_HUNTER_L2_CONCURRENCY=10 uv run job-hunter
  extract run --max-docs 25 --max-usd 0` (local engine, $0), asserting from
  the run summary and artifacts: (1) zero `audit_error` from machinery
  (no hash failures — the field no longer exists; any residual binding
  errors get diagnosed individually, not papered over), (2) at least one
  document exercised the retry path and its retry attempt preserved the
  prior emit's unrelated valid objects (artifact diff), (3) audits landed
  under version-keyed `.a3` artifacts, (4) the batch settles with every doc
  in a terminal or scheduled state and the standard gate stays green
  (`uv run pytest -q && uv run ruff check . && uv run mypy`).
- [ ] Step 3 (human, Sean): spot-check ≥5 records that validator 19 judges
  differently than 18 (both directions), and the review/quarantine split of
  the live batch against the current 21.0%/2.4% baseline before authorizing
  any backfill or bulk retry campaign.

## Non-goals

- No bulk backfill/migration of the existing corpus and no bulk review-retry
  campaign — Task 6's numbers decide those separately.
- No F1 threshold recalibration; no new agreement thresholds.
- No CI extraction (collection-only stays); v1 bundle untouched.
- The residual audit-binding normalization cases (lenient resolver already
  exists at `audit.py:493`) are investigated per-case in Task 6's live gate,
  not preemptively "fixed".
