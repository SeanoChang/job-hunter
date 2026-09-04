---
id: T-20260904-3MJK
type: probe
state: open
plan: P-20260904-KWVF
milestone: M-20260904-QYDM
classification: spike
appetite: S
backbone_index: 10
owner: human
priority: P2
severity: normal
model: sonnet
effort: medium
review_mode: ask
provenance:
  session: -
  captured_at: 2026-09-04T19:38:09Z
  source: pmi ticket new
  source_ref: plan:P-20260904-KWVF
  actor: agent
---

# Probe: pin Oracle CE detail-finder syntax

The list endpoint is live-verified; the per-requisition detail finder (ById, expand=all) is the one unverified link in the Oracle family. Timeboxed curl session against JPMC/Oracle; deliverable is the exact request shape, code reverted. Spec §4.2, §7.

## Acceptance criteria

- [ ] ac-1 | an Evidence node linked from this ticket records the exact curl (URL, finder, params) that returns one JPMC requisition's full description, plus its response field names | predicate: `bash -c 'grep -rl "T-20260904-3MJK" please-map-it/web | xargs grep -ql recruitingCEJobRequisitions'` ^ac-1

### Manual

- [ ] ac-2 | the recorded curl reproduces when pasted fresh (probe code reverted; only the finding remains) | owner: human ^ac-2

## Invariants

- Timeboxed to one working session; anything written for the probe is reverted — the deliverable is the finding, not code.
- Read-only requests against public endpoints; policy [[D-20260904-R5GR]] applies.

## Known-bad approaches

- Guessing the finder from the spec and building the adapter on the guess — the reason this probe exists (spec §7).

## Interfaces

- Produces: the pinned detail-request shape consumed by [[T-20260904-JQXX]] (its uncertain edge resolves when this lands).

## Touch paths

```paths
please-map-it/web/*.md
```

## Non-goals

```paths
src/**
tests/**
```
