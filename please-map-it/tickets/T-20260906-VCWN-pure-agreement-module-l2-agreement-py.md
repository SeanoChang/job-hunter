---
id: T-20260906-VCWN
type: feature
state: open
plan: P-20260906-PK6N
classification: bounded
appetite: M
backbone_index: 1
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
  source_ref: docs/superpowers/plans/2026-09-06-m3-quality-loop.md#Task 1
  actor: agent
---

# pure agreement module (`l2/agreement.py`)

Claim alignment across samples by span-overlap Jaccard ≥ 0.5 (greedy, one-to-one); mean pairwise claim-set F1 ≥ 0.80; importance agreement on required claims ≥ 0.90; zero negation disagreements (any split escalates unconditionally). Medoid sample selection (whole record, max mean pairwise F1, deterministic tie-break by sample slot). Disagreement report dict for `extractions.agreement`. Pure functions over profile dicts; exhaustive tests.

## Acceptance criteria

- [ ] ac-1 | alignment, F1, importance and negation gates behave per spec on synthetic sample sets | predicate: `uv run pytest tests/l2/test_agreement.py -v` ^ac-1
- [ ] ac-2 | the medoid is chosen whole and deterministically (tie-break by sample slot) | predicate: `uv run pytest tests/l2/test_agreement.py -k "medoid" -v` ^ac-2

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- Pure: no I/O, no LLM, no store import.
- Thresholds are module constants surfaced for VALIDATOR_VERSION coupling.

## Known-bad approaches

None known — first implementation of spec §4.5.

## Interfaces

- Produces: `agree(samples: list[profile dict]) -> AgreementResult` (passed, report dict, medoid index); consumed by the runner ticket.

## Touch paths

```paths
src/jobhunter/l2/agreement.py
tests/l2/test_agreement.py
```

## Non-goals

```paths
src/jobhunter/l2/runner.py
src/jobhunter/l2/transforms.py
```
