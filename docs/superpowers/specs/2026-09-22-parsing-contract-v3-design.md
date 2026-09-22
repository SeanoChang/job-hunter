# Parsing contract v3 — the parser stops issuing verdicts

Status: **approved 2026-09-22** (Sean). Amends
`2026-09-07-parsing-contract-v2-design.md`; every section of that spec not
named here stands. Implementation plan:
`docs/superpowers/plans/2026-09-22-v20-list-everything.md`.

## 1. Principle

Small models extract; big models judge. The runner emits only what can be
verified against the posting's own bytes — statements, skills, numbers,
relations, each with a verbatim quote — plus objective context signals. Every
judgment (is this required, is this a dealbreaker, does this candidate fit)
belongs to the agent reading the record through the CLI/MCP, which has the
candidate's profile and the full source; the extractor never does.

Evidence base: the 2026-09-22 analysis of the v19 review queue (300-doc
sample, `scratchpad/review_splits.out`): 294/300 review docs failed on label
variance over identical text — adjacent taxonomy kinds (172 pairs
compensation vs employer_context), the same number under different scope
tags (1,399 of 1,536 value splits), `gps` vs `global positioning systems
(gps)`. Importance labels disagreed across runs of one model and more across
models. A verdict three readings disagree on is noise, not information.

## 2. Contract deltas (schema 3, prompt `demand-profile/v11`)

### 2.1 Statements lose their verdicts

Removed from the emit and record shapes: `importance`, `importance_evidence`,
`proficiency`, `proficiency_evidence`. Both were labels the model assigned
from descriptor text ("Demonstrable expertise" → required; "Advanced" →
expert) and neither is checkable against the source.

Added, in their place:

| field | owner | definition |
| --- | --- | --- |
| `section_heading` | **code** (assemble) | the text of the nearest preceding heading block, or null. A heading block is a source block matching `^#{1,6}\s+\S` (ATX) or `^\*\*[^*\n]{1,80}\*\*:?\s*$` (a bold-only line). Derived from the statement's first evidence span; never emitted by the model. |
| `modality_evidence` | model, **bound** | zero or one reference quoting the posting's own modal phrase for this statement, verbatim ("must", "required", "preferred", "ideally", "a plus", "nice to have", "minimum"); null when the text carries none. Bound and validated exactly like `evidence`; the vocabulary is open — the validator checks binding, not wording. The model never infers modality from a heading or from the clause's own descriptor. |

`kind` stays: the listing must separate what the candidate needs from what the
employer describes. It is structural, never gated (§3).

### 2.2 Everything else in the record is unchanged

Mentions, facts (with code-derived values), relations, block accounting,
source assessment, the quality block — byte-for-byte the schema-2 shapes.

## 3. Settlement (validator `20`)

The agreement gate keeps two checks. Everything else it computes today is
reported in `agreement` as a metric and never fails a document.

| check | rule | why it stays |
| --- | --- | --- |
| **negation** | aligned claims must agree on polarity | "no sponsorship" read as "sponsorship available" is the one extraction error that actively harms the user; it is rare and cheap |
| **numeric conflict** | two samples that both parsed a number from the same span must agree on dimension, bounds and unit | 144 vs 12 months is a misread, not variance |

Demoted to metrics: `f1`, `importance` (gone with the field), `kind`,
`scoped_values` (scope/family tags), `alternatives`, `entity_links`,
`polarity_target`. Dispute sets and adjudication (validator/18) still
compute against the metrics for the audit's scoping; they no longer decide
publication.

Statuses: a document with at least one assembled-and-verified candidate is
`validated`. `needs_review` is reserved for the two gate failures above and
for human parking; `quarantined` is unchanged (no candidate survived
attribution).

## 4. Serving — every verified extraction serves

`profile_mentions` and the profile blob are written for every `validated`
row, and — the delta — for every `needs_review` row that carries a chosen
candidate. The blob's `quality` block gains `sample_notes`: the metric
splits the cohort showed (which dimensions, how many aligned pairs), so a
reading agent sees what the samples disagreed on. `search_eligible` keeps
its name and its audit-derived meaning, re-scoped by §6.

## 5. Sampling — monitoring, not adjudication

k-sampling runs only on the deterministic slot (`JOB_HUNTER_L2_AUDIT_MOD`,
5%). A reprompted first pass no longer triggers extra samples: the retry
contract (v10) already makes a reprompt an edit of the prior candidate, and
the 373-of-1,000 "incomplete cohort" review class was nothing but exhausted
sample budgets. Expected effect: 2–3× fewer engine calls per document.

## 6. Audit `semantic-audit/v4` and repair `semantic-repair/v2`

The auditor certifies **extraction fidelity** — nothing the source says was
missed (`omission`, `bad_exclusion`), nothing was invented
(`unsupported_statement`, `mention_linkage`), polarity and numbers read as
written (`polarity_subject`, `numeric_scope_unit`), relations as written
(`relationship`). The `importance` code is removed with the field. Eligibility
(`search_eligible`) means "this record is a faithful extraction", never "these
claims are the employer's true requirements". Repair operations follow the
schema-3 statement shape; policy (one round, one re-audit, old-hash guards)
is unchanged.

## 7. Versions and migration

Frozen identifiers bump together: prompt `demand-profile/v11`, schema `3`,
validator `20`, `semantic-audit/v4`, `semantic-repair/v2`. Nothing under the
v10/2/19 tuple is edited.

Migration is offline and complete. A schema-2 archived record derives its
schema-3 record without a model call: `section_heading` from the block
structure and the statement's first evidence span; `modality_evidence` from
the archived `importance_evidence` **only when that quote matches the
code-owned modal lexicon** (`must|required|require|preferred|prefer|
ideally|plus|nice to have|bonus|minimum|at least|strongly`) — a descriptor
quote ("Demonstrable expertise") derives null, never a modality it is not.
`proficiency` is dropped. Replay (`extract rebuild`) re-derives and
re-settles the whole corpus under 20 with zero engine calls; new documents
extract under v11 from the first call.

## 8. Agent surface

Unchanged verbs, richer answer: `q profile <doc>` returns the schema-3 record
(statements with heading + modal quote, skills, facts, relations);
`q document <doc>` the source. The intended front page for the hunt is the
skills list plus the hard facts a posting can state — work authorization,
location/on-site, level, dates — each with its quote. Judging the rest is the
reader's job, with the evidence to do it.

## 9. Non-goals

No matcher, no tracker, no concept linker in this increment. No change to
collection, the archive, or the lifecycle store. No calibration of any
label — labels that needed calibrating were removed instead.
