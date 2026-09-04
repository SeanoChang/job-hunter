---
id: T-20260904-797G
type: chore
state: open
plan: P-20260904-KWVF
milestone: M-20260904-QYDM
classification: bounded
appetite: S
backbone_index: 12
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

# O: add the 6 Oracle boards, JPMC first

JPMC (7,317 postings) is the largest single unlock in the plan; then Oracle, TI, Uber, Amex, Akamai. Spec §6 O.

depends-on:: [[T-20260904-JQXX]] (prerequisite) — no oraclehcm boards without the adapter

## Acceptance criteria

- [ ] ac-1 | registry validates with the six oraclehcm boards; each verified live at add time (TotalJobsCount > 0, real title) | predicate: `uv run job-hunter registry check` ^ac-1
- [ ] ac-2 | JPMC postings queryable after two cycles | predicate: `uv run job-hunter q postings --board oraclehcm:jpmc -o json | jq -e '.meta.count > 0'` ^ac-2

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- JPMC lands first and alone for one cycle; the other five follow only when it is green.

## Known-bad approaches

- Adding boards on token-name trust — [[E-20260904-RDGJ]].

## Interfaces

- Consumes: the adapter from [[T-20260904-JQXX]]; endpoints from [[E-20260904-PNDB]].

## Touch paths

```paths
companies.toml
```

## Non-goals

```paths
src/jobhunter/**
```
