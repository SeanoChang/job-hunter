# v20 — List Everything, Gate Only What's Dangerous — Implementation Plan

> **For agentic workers:** implemented via a Workflow (implementation agents +
> adversarial verification), task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The parser stops issuing verdicts. Statements carry objective
context (code-derived section heading, quoted modal phrase) instead of
importance/proficiency labels; every verified extraction serves; the gate
keeps only negation and numeric conflict; sampling monitors instead of
adjudicating. The existing corpus migrates by replay with zero engine calls.

**Architecture:** One coordinated frozen-identifier bump — prompt
`demand-profile/v11`, emit/record schema `3`, validator `20`,
`semantic-audit/v4`, `semantic-repair/v2` — with the v10/2/19 tuple
untouched. Assembly gains a code-owned heading derivation; the agreement
module keeps computing every dimension but only two fail a document; the
store refills the aggregate for any row with a chosen candidate; the runner's
sampling trigger drops the reprompt branch; rebuild derives schema-3 records
from schema-2 archives offline.

**Spec:** `docs/superpowers/specs/2026-09-22-parsing-contract-v3-design.md`
(approved 2026-09-22). Evidence: the 2026-09-22 review-queue analysis
(300-doc sample; `scratchpad/review_splits.out`, `review_calibrate2.out`).

## Global constraints

- Frozen identifiers bump, never edit in place: v11 / 3 / 20 / audit v4 /
  repair v2, each with its history line; 19 and everything below untouched.
- Null-over-guess: `modality_evidence` is quoted or null — never inferred
  from a heading or descriptor; `section_heading` is code-owned, never
  emitted.
- Evidence binding unchanged: every new reference field binds through
  `source.resolve` and is checked by `verify` like `evidence`.
- Migration is offline and total: no engine call may be needed to bring a
  schema-2 record to schema 3.
- `mypy --strict`, ruff 100; tests offline/deterministic; TDD — every
  behavior change lands with a test watched failing first.
- Store-touching tests run against an isolated database
  (`JOB_HUNTER_TEST_DATABASE_URL`) while a drain holds the writer lock.

## Policy (normative — settlement under validator/20)

| gate | rule | outcome on failure |
| --- | --- | --- |
| negation | aligned claims disagree on polarity | `needs_review` |
| numeric conflict | both samples parsed a number from the same span; dimension, bounds or unit differ | `needs_review` |
| everything else (`f1`, `kind`, `scoped_values` tags, `alternatives`, `entity_links`, `polarity_target`) | computed, reported under `agreement`, surfaced in `quality.sample_notes` | none — the document is `validated` |

A document with one assembled-and-verified candidate and no gate failure is
`validated`. `quarantined` is unchanged. Human dispositions stay senior.

### Task 1: schema 3 + assembly — headings in, verdicts out

**Files:** `src/jobhunter/l2/schemas_data/3/{emit,record}.schema.json`
(new), `src/jobhunter/l2/v2/assemble.py`, `src/jobhunter/l2/v2/verify.py`,
`src/jobhunter/l2/v2/source.py` (heading predicate), `src/jobhunter/l2/schemas.py`
(loader knows "3"), `tests/l2/v2/test_assemble.py`, `tests/l2/v2/test_verify.py`,
`tests/l2/v2/test_source.py`, `tests/l2/test_schemas.py`.

**Interfaces:** `source.heading_of(blocks, block_id) -> str | None` — nearest
preceding heading block's text under the spec §2.1 predicate.
Statement (schema 3): drop `importance`, `importance_evidence`,
`proficiency`, `proficiency_evidence`; add `section_heading: str | null`
(record only, code-owned) and `modality_evidence: [reference] | null`
(emit + record, bound). `assemble` derives `section_heading` from the first
bound `evidence` span; `verify` treats `modality_evidence` as a bound ref
family (attribution + occurrence checks) and rejects any emitted
`section_heading` (`schema:unexpected_field`).

- [ ] Step 1: failing tests — heading predicate (ATX and bold-only lines
  are headings; a bullet is not; nearest-preceding wins; none → null);
  assemble writes `section_heading` per statement from block structure;
  an emit carrying `importance` or `section_heading` fails schema 3;
  `modality_evidence` binds and mis-cites like `evidence`.
- [ ] Step 2: write schema 3 (copy of 2 with the statement delta), heading
  predicate, assembly derivation, verify wiring.
- [ ] Step 3: bump `VALIDATOR_VERSION` to "20" with its history line.
  Full gate green. Commit.

### Task 2: prompt `demand-profile/v11`

**Files:** `src/jobhunter/l2/v2/prompt.py`, `tests/l2/v2/test_prompt.py`.

- [ ] Step 1: failing template pins — no importance/proficiency
  instructions remain; the modality rule is present ("quote the posting's
  own modal phrase if one applies to this statement; otherwise null; never
  infer from a heading or the clause itself"); both worked examples emit
  schema-3 statements and validate under schema 3; retry-candidate block
  unchanged.
- [ ] Step 2: rewrite; `PROMPT_VERSION` v11 with history line; `bundles.py`
  v2 registration moves to (v11, "3", "20") — the v10/2/19 tuple stays
  reachable through `get_bundle_for_tuple` for replay.
- [ ] Step 3: full gate green. Commit.

### Task 3: validator/20 settlement + sampling

**Files:** `src/jobhunter/l2/agreement.py`, `src/jobhunter/l2/state.py`,
`src/jobhunter/l2/v2/serve.py` (quality `sample_notes`),
`src/jobhunter/l2/runner.py` (sampling trigger), `tests/l2/test_agreement.py`,
`tests/l2/test_state.py`, `tests/l2/test_runner_v2.py`.

**Interfaces:** `agreement.agree` reports every dimension as today but
`passed` is `negation == 0 and numeric_conflicts == 0`; `report["metrics"]`
carries the demoted dimensions; `serve.profile_of` writes
`quality.sample_notes` from the report. Runner: `audit or reprompted` becomes
`audit` only.

- [ ] Step 1: failing tests — the 2026-09-22 refutation shapes as fixtures:
  a cohort split only on kind/scope-tag/entity spelling/relation
  thoroughness settles `validated` with `sample_notes` naming the splits; a
  negation split still settles `needs_review`; 144-vs-12-months still
  settles `needs_review`; same number with different scope tags does not;
  a reprompted first pass takes no extra samples; the 5% slot still does.
- [ ] Step 2: implement; append the "20" policy to the validator history
  line (Task 1's) — no new identifier.
- [ ] Step 3: full gate green. Commit.

### Task 4: serving — every verified extraction serves

**Files:** `src/jobhunter/store/extraction.py`, `src/jobhunter/views.py`,
`src/jobhunter/pulse.py` (only if a status filter hides needs_review),
`tests/store/test_extraction.py`, `tests/test_views.py`.

- [ ] Step 1: failing tests — `upsert_state` refills `profile_mentions` for
  a `needs_review` row with a chosen candidate and still clears it for
  `quarantined`/pending; `q profile`/`q claims` return needs_review rows'
  records and skills; the profile carries `sample_notes`.
- [ ] Step 2: implement (the 2026-08-26 ruling amended: aggregates carry
  what the corpus *extracted*, with its quality note).
- [ ] Step 3: full gate green. Commit.

### Task 5: `semantic-audit/v4` + `semantic-repair/v2`

**Files:** `src/jobhunter/l2/v2/audit.py`, `src/jobhunter/l2/v2/repair.py`,
`tests/l2/v2/test_audit.py`, `tests/l2/v2/test_repair.py`.

- [ ] Step 1: failing tests — `importance` is no longer a valid finding
  code; the auditor prompt shows schema-3 candidates (heading + modality,
  no importance); triage and the boilerplate/capture rules unchanged;
  repair ops target schema-3 statement fields and refuse `importance`.
- [ ] Step 2: bump both versions with history lines; audit artifact keys
  spell `.a4`. Full gate green. Commit.

### Task 6: migration replay + live gate

**Files:** `src/jobhunter/l2/rebuild.py`, `scripts/`, `tests/l2/test_rebuild.py`.

- [ ] Step 1: failing test — rebuild derives a schema-3 record from a
  schema-2 archived attempt with zero engine calls: `section_heading`
  computed, `modality_evidence` = archived `importance_evidence` only when
  the quote matches the modal lexicon (descriptor quotes derive null),
  `proficiency` dropped; the derived record verifies clean under 20 and the
  candidate hash is deterministic.
- [ ] Step 2: implement; run `extract rebuild` over the corpus (operator,
  zero engine calls) and report: rows per status before/after, docs now
  serving skills, review share.
- [ ] Step 3 (completion criterion): live codex-cli drain, 25 fresh docs
  under v11/3/20 — every doc terminal, no machinery audit errors, k-samples
  only on the 5% slot, records carry headings and quoted modality, no
  importance anywhere; standard gate green.

## Non-goals

- No matcher, tracker, digest or concept linker in this plan.
- No change to collection, the archive layout, or the lifecycle store's
  single-writer discipline.
- No calibration of any retained label; no F1 threshold work.
