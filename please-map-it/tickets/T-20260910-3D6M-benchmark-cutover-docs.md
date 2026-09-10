---
id: T-20260910-3D6M
type: feature
state: open
plan: P-20260910-Q3CC
classification: bounded
appetite: M
backbone_index: 6
owner: human
priority: P2
severity: normal
model: opus
effort: xhigh
review_mode: ask
provenance:
  session: 14889510-6774-451b-9df1-285789f60029
  captured_at: 2026-09-10T13:44:13Z
  source: pmi capture planwrite
  source_ref: docs/superpowers/plans/2026-09-10-v2-cutover-local.md#Task 6
  actor: agent
---

# benchmark, cutover, docs

The proof and the switch: fixture benchmark green, a live 20-document codex A/B against the v1 baseline with an explicit gate (v2 quarantine ≤ half of v1's), then the .env bundle flip and drain-loop restart, then docs. A failed gate halts cutover and reopens analysis — the switch never happens on vibes.
depends-on:: [[T-20260910-JD35]] (prerequisite) — the served surface must understand v2 before v2 rows exist
depends-on:: [[T-20260910-QAW7]] (prerequisite) — cutover re-extracts 30k docs; the [A1] fix must land before that volume

## Acceptance criteria

- [x] ac-1 | fixture benchmark: the 12 case contracts plus the v2 runner loop pass | predicate: `uv run pytest tests/l2/test_runner_v2.py tests/l2/v2/ -q` ^ac-1
- [ ] ac-2 | live A/B recorded in this ticket: 20 docs under bundle v2, quarantine rate ≤ half of the v1 baseline on the same sample | predicate: `JOB_HUNTER_L2_BUNDLE=v2 uv run job-hunter extract run --max-docs 20 --max-usd 0 -o json` ^ac-2
- [x] ac-3 | docs updated: CLAUDE.md bundles section, runbook bundle env line, README increment-2 slice marked shipped with auditor/repair explicitly open | predicate: `grep -q "JOB_HUNTER_L2_BUNDLE" docs/runbooks/2026-09-09-local-codex-drain.md && grep -q "bundles" src/jobhunter/CLAUDE.md && echo ok` ^ac-3
- [x] ac-4 | final full gate | predicate: `uv run pytest tests/ -q && uv run ruff check . && uv run mypy` ^ac-4

### Manual

- [ ] ac-5 | the A/B numbers justify cutover (or the halt is recorded with the failure analysis) and the .env flip + loop restart happened | owner: human ^ac-5

## Invariants

- The drain loop is paused during the A/B so the sample is clean.
- v1 rows keep serving until a v2 row supersedes them (profile_row's current-tuple-first order) — no serving gap during re-extraction.
- No GitHub activity anywhere in this ticket.

## Known-bad approaches

- Cutting over on the fixture benchmark alone: fixtures are the 12 curated cases; the live A/B on fresh queue docs is the gate that reflects the actual corpus.
- Running the A/B while the v1 loop drains: two writers contend for the lock and the sample gets skewed by lock_held skips.

## Interfaces

## Touch paths

```paths
docs/README.md
src/jobhunter/CLAUDE.md
docs/runbooks/2026-09-09-local-codex-drain.md
please-map-it/tickets/T-20260910-3D6M-benchmark-cutover-docs.md
```

## Non-goals

```paths
src/jobhunter/l2/**
tests/**
```

## Record — 2026-09-10

### Step 1 — fixture benchmark (ac-1): PASS

`uv run pytest tests/l2/test_runner_v2.py tests/l2/v2/ -q` → exit 0, 187 tests
(the twelve case contracts, the v2 module suites, and the v2 runner loop).

### Step 2 — live A/B (ac-2): NOT RUN BY THIS TICKET; the run in flight is failing

The predicate calls codex; this agent is barred from invoking any model, so it
is **deferred** as a command. A larger live run of the same shape was already in
flight on this machine while this ticket executed — the session's
`v2_quarantine_experiment.py`, which feeds every v1-quarantined document
through the v2 bundle with the real runner (`JOB_HUNTER_L2_BUNDLE=v2`,
concurrency 8, `codex-cli` / `gpt-5.6-luna`). Its numbers, read out of the
store read-only at 13:40 local, with the run 9/113 documents in:

| | v1 baseline (tuple `demand-profile/v5` / 1 / 12, whole corpus) | v2 run so far (`demand-profile/v6` / 2 / 10) |
|---|---|---|
| validated | 123 | 0 |
| needs_review | 83 | 0 |
| quarantined | 61 | 9 |
| documents settled | 267 | 9 of 113 targeted |

Sample definition: the 113 targets are exactly the documents v1 quarantined, so
v1's quarantine rate on this sample is 100% and the gate is "v2 quarantine
≤ 50%". At 9 of 9 settled documents quarantined, the run is failing the gate,
not passing it. **Cutover is therefore halted** pending the analysis below and
a re-run.

Failure classes over the 39 v6 attempts recorded so far — 223 errors, parsed
from `extraction_attempts.error_detail`. Every attempt is `attribution_failed`
and none is `schema_invalid`, so the emits are schema-valid JSON and it is the
v2 verifier and reference binder rejecting them:

| errors | docs | finding |
|---|---|---|
| 76 | 12 | `statements:importance_unexpected` |
| 73 | 10 | `statements:evidence_missing` |
| 33 | 13 | reference binding — `not a literal substring` |
| 20 | 6 | `statements:importance_missing` |
| 9 | 2 | `references:unknown_reference` |
| 5 | 3 | `facts:fact_family_shape` |
| 3 | 2 | `accounting:exclusion_reason_missing` |
| 3 | 2 | `facts:presence_mismatch` |
| 1 | 1 | `accounting:refs_missing` |

Reading: 169 of 223 errors (76%) are one prompt/verifier disagreement about
**statement importance and per-aspect evidence** — the model attaches
importance where the contract forbids it, omits it where the contract requires
it, or omits the evidence spans the verifier demands. Reference binding, the
class v2 exists to kill, is down to 42 errors including unknown ids. The twelve
fixture cases cannot catch the first class because their emits are curated. The
next move is `demand-profile/v6`'s importance/evidence rules, not another
binding mechanism — analysis owner is the plan, not this ticket.

### Step 3 — cutover (ac-5): NOT PERFORMED

`.env` is unchanged (`JOB_HUNTER_L2_BUNDLE` still unset → `v1`) and no drain
loop was restarted. Two independent reasons:

1. The gate above is failing.
2. **The read surface does not follow the bundle** (found while writing the
   docs, verified in code). `views.profile_row`, `views.claims_view` and
   `pulse` each compute "the engine tuple in force" from the v1 module
   constants — `l2.prompt.PROMPT_VERSION`, `l2.runner.SCHEMA_VERSION`,
   `l2.transforms.VALIDATOR_VERSION` — never from `JOB_HUNTER_L2_BUNDLE`.
   Consequences after a flip: `q profile`/MCP `q_profile` keep serving the v1
   row for any document that has one (current-tuple rows sort first, and v1 is
   "current" to that query), a document whose only row is v2 is labelled
   `historical: true`, `q claims` cannot see v2 `profile_mentions` rows, and
   `pulse` shows no v2 profiles. This ticket's invariant "v1 rows keep serving
   until a v2 row supersedes them" holds only in its first half — supersession
   never happens. Fixing those three call sites is a prerequisite of the flip
   and belongs to a new ticket (`views.py` is a non-goal here).

### Step 4 — docs (ac-3): DONE

`grep -q "JOB_HUNTER_L2_BUNDLE" docs/runbooks/2026-09-09-local-codex-drain.md
&& grep -q "bundles" src/jobhunter/CLAUDE.md && echo ok` → `ok`.
`src/jobhunter/CLAUDE.md` gains the bundles entry and drops "not wired into the
runner, CLI, or MCP yet" from `l2/v2/`; the runbook gains the bundle env
section, the read-path gap, and the A/B gate with its pass/fail branches;
`docs/README.md` indexes the cutover plan, marks increment 2's harness slice
shipped with `semantic-audit/v1`/`semantic-repair/v1` explicitly open, adds
`demand-profile/v6` to the frozen identifiers, and records the 2026-09-10
standing ruling.

### Step 5 — final gate (ac-4): PASS, on an isolated database

`uv run pytest tests/ -q && uv run ruff check . && uv run mypy` → 1075 tests
exit 0, "All checks passed!", "Success: no issues found in 72 source files".

Against the default DSN the same command fails 76 tests, all with
`lock_held=True`: the live A/B above holds the extract advisory lock for the
whole run, and that lock is database-wide (T-20260908-YFHC). Environmental, and
outside this ticket's docs-only fence. The green run above used
`JOB_HUNTER_TEST_DATABASE_URL=postgresql://jobhunter:jobhunter@localhost:5432/jobhunter_gate_t6`,
an empty scratch database created for it; drop it with
`psql -d postgres -c 'DROP DATABASE jobhunter_gate_t6'` whenever the clutter
bothers anyone.
