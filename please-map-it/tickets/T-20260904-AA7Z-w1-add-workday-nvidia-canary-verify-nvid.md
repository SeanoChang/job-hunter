---
id: T-20260904-AA7Z
type: feature
state: open
plan: P-20260904-KWVF
milestone: M-20260904-FPX5
classification: bounded
appetite: S
backbone_index: 7
owner: human
priority: P2
severity: normal
model: sonnet
effort: medium
review_mode: ask
provenance:
  session: -
  captured_at: 2026-09-04T19:37:49Z
  source: pmi ticket new
  source_ref: plan:P-20260904-KWVF
  actor: agent
---

# W1: add workday:nvidia, canary, verify NVIDIA profiles

NVIDIA is the sole proving board for the whole chassis: registry add, fetch canary, presence and versions verified, then the minimum/preferred-qualifications L2 parse check Sean asked for. Spec §6 W1.

depends-on:: [[T-20260904-PZZQ]] (prerequisite) — postings cannot appear without pending_detail ingest
depends-on:: [[T-20260904-VJKA]] (prerequisite) — no workday source without the adapter

## Acceptance criteria

- [ ] ac-1 | registry validates with workday:nvidia added | predicate: `uv run job-hunter registry check` ^ac-1
- [ ] ac-2 | after two hourly cycles, NVIDIA postings exist with document-backed versions | predicate: `uv run job-hunter q postings --board workday:nvidia -o json | jq -e '.meta.count > 0'` ^ac-2
- [ ] ac-3 | the sync canary run stays inside the step budget with NVIDIA aboard | predicate: `gh run view --repo SeanoChang/job-hunter $(gh run list --repo SeanoChang/job-hunter --workflow fetch.yml --limit 1 --json databaseId -q '.[0].databaseId') --json conclusion -q .conclusion | grep -x success` ^ac-3

### Manual

- [ ] ac-4 | an NVIDIA demand profile renders the posting's minimum vs preferred qualifications as required vs preferred claims — Sean judges the parse | owner: human ^ac-4

## Invariants

- Verification runs against the deployed schedule (real Neon + R2), not a local stub.
- One board only in this ticket — the wave ([[T-20260904-KXNR]]) waits for this proof.
- Cost caps and detail budgets stay at spec defaults during the canary.

## Known-bad approaches

- Trusting token names without reading real job titles — [[E-20260904-RDGJ]].

## Interfaces

- Consumes: everything W0 built; produces the go/no-go evidence for [[T-20260904-KXNR]] and answers [[Q-20260904-76DR]] with telemetry.

## Touch paths

```paths
companies.toml
```

## Non-goals

```paths
src/jobhunter/**
```
