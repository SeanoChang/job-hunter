---
id: T-20260904-KXNR
type: chore
state: open
plan: P-20260904-KWVF
milestone: M-20260904-M938
classification: bounded
appetite: S
backbone_index: 9
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

# W2: Workday wave — 25 tenants in two registry batches

Majors first, then the rest; Dell site slug and Booking's 22-posting board re-verified at add time; every board identity checked with real job titles (the linkedin-fake lesson, E-20260904-RDGJ). Neon Launch upgrade is a precondition (Q-20260904-EHWB). Spec §6 W2.

depends-on:: [[T-20260904-AA7Z]] (prerequisite) — the chassis is proven on NVIDIA before 25 boards ride it

## Acceptance criteria

- [ ] ac-1 | every added tenant verified at add time: live CXS total > 0 and a real job title matching the company | predicate: `uv run python scripts/live_smoke.py --source workday` ^ac-1
- [ ] ac-2 | registry validates and the next scheduled runs stay green with the wave aboard | predicate: `uv run job-hunter registry check` ^ac-2
- [ ] ac-3 | all 26 workday boards report healthy | predicate: `uv run job-hunter q boards -o json | jq -e '[.data[] | select(.board | startswith("workday:"))] | length == 26'` ^ac-3

### Manual

- [ ] ac-4 | Neon Launch upgrade confirmed before the first batch lands ([[Q-20260904-EHWB]]) | owner: human ^ac-4

## Invariants

- Two batches, majors first; a batch that turns a board red is rolled back by removing the board, never by loosening health checks.
- Dell's site slug and Booking's 22-posting board re-verified, not copied from the probe.

## Known-bad approaches

- Adding boards on token-name trust — [[E-20260904-RDGJ]] (the linkedin fake, the Qualcomm/Grab dead tenants).

## Interfaces

- Consumes: the chassis proven by [[T-20260904-AA7Z]]; tenant list from [[E-20260904-PNDB]].

## Touch paths

```paths
companies.toml
scripts/live_smoke.py
```

## Non-goals

```paths
src/jobhunter/**
```
