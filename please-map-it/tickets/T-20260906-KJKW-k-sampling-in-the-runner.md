---
id: T-20260906-KJKW
type: feature
state: open
plan: P-20260906-PK6N
classification: bounded
appetite: M
backbone_index: 2
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
  source_ref: docs/superpowers/plans/2026-09-06-m3-quality-loop.md#Task 2
  actor: agent
---

# k-sampling in the runner

Slot rule: `int(document_hash[:8], 16) % JOB_HUNTER_L2_AUDIT_MOD == 0` (default 20) → k=3; a slot-1 pass that needed a reprompt escalates to k=3; everyone else k=1. Extra samples are ordinary archived attempts under their `sample_slot`. Agreement gate on completion: pass → validated with the medoid as `chosen_attempt` and the report in `agreement`; fail → `needs_review` with the report. Caps count all samples.

depends-on:: [[T-20260906-VCWN]] (prerequisite) — the gate calls agree(); without it k-samples have no verdict

## Acceptance criteria

- [ ] ac-1 | audit-slot and reprompt-escalation documents run k=3, others k=1, all samples archived under their slot | predicate: `uv run pytest tests/l2/test_runner.py -k "sampling" -v` ^ac-1
- [ ] ac-2 | agreement failure demotes to needs_review with the report stored; pass validates the medoid as chosen_attempt | predicate: `uv run pytest tests/l2/test_runner.py -k "agreement" -v` ^ac-2

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- Samples never merged; chosen_attempt always one archived response.
- Caps count every sample's observed usage.

## Known-bad approaches

None known.

## Interfaces

- Consumes: `agree()` from the agreement ticket; slot rule int(document_hash[:8],16) % JOB_HUNTER_L2_AUDIT_MOD == 0, default 20.

## Touch paths

```paths
src/jobhunter/l2/runner.py
src/jobhunter/config.py
tests/l2/test_runner.py
```

## Non-goals

```paths
src/jobhunter/l2/agreement.py
src/jobhunter/store/schema.sql
```
