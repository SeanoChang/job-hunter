---
id: T-20260906-EMHP
type: feature
state: open
plan: P-20260906-PK6N
classification: bounded
appetite: M
backbone_index: 4
owner: human
priority: P2
severity: normal
model: sonnet
effort: medium
review_mode: ask
provenance:
  session: 7309c626-e091-47fa-889c-466cb4b0249a
  captured_at: 2026-09-06T14:38:57Z
  source: pmi capture planwrite
  source_ref: docs/superpowers/plans/2026-09-06-m3-quality-loop.md#Task 4
  actor: agent
---

# weekly consolidation

`extract consolidate`: reads validated rows only; emits append-only artifacts to the archive — drift report (agreement series, quarantine rates by source/board, validator-version mix), the human audit queue (needs_review oldest-first with reasons), and refuter summary. A weekly cron step in the existing workflow family (no new scheduler); exit 0 on nothing-to-do.

## Acceptance criteria

- [ ] ac-1 | consolidation emits the drift report and audit queue as append-only archive artifacts from validated rows only | predicate: `uv run pytest tests/l2/test_consolidate.py -v` ^ac-1
- [ ] ac-2 | the weekly workflow step exits 0 with nothing to do and never writes outside the archive's consolidation prefix | predicate: `uv run pytest tests/l2/test_consolidate.py -k "noop or prefix" -v` ^ac-2

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- Validated-only inputs; append-only outputs; no second scheduler (a step in the existing workflow family).

## Known-bad approaches

None known.

## Interfaces

- Produces: `extract consolidate` verb; artifacts under consolidation/ in the archive.

## Touch paths

```paths
src/jobhunter/l2/consolidate.py
src/jobhunter/cli.py
.github/workflows/fetch.yml
tests/l2/test_consolidate.py
```

## Non-goals

```paths
src/jobhunter/store/schema.sql
```
