---
id: T-20260904-YZPA
type: feature
state: open
plan: P-20260904-KWVF
milestone: M-20260904-45P2
classification: bounded
appetite: S
backbone_index: 15
owner: human
priority: P2
severity: normal
model: sonnet
effort: xhigh
review_mode: ask
provenance:
  session: -
  captured_at: 2026-09-04T19:38:10Z
  source: pmi ticket new
  source_ref: plan:P-20260904-KWVF
  actor: agent
---

# Eightfold adapter + Netflix

List verified on explore.jobs.netflix.net; fixture capture decides list-only vs detail calls (positions may embed job_description). Spec §4.5.

depends-on:: [[T-20260904-8J7V]] (prerequisite) — runs on the two-phase driver

## Acceptance criteria

- [ ] ac-1 | jobs-list fixture (explore.jobs.netflix.net) parses positions and count with start/num pagination | predicate: `uv run pytest tests/sources/test_eightfold.py -k "list" -v` ^ac-1
- [ ] ac-2 | the fixture-decided description path (embedded job_description or per-position call) yields full description_html | predicate: `uv run pytest tests/sources/test_eightfold.py -k "description" -v` ^ac-2
- [ ] ac-3 | registry validates with eightfold:netflix added | predicate: `uv run job-hunter registry check` ^ac-3

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- Netflix only: Microsoft and Qualcomm are Eightfold but gated anonymously ([[E-20260904-PNDB]]) — they stay out until an open endpoint is verified.

## Known-bad approaches

- Reusing the Netflix domain pattern for gated tenants and retrying around 403s — policy [[D-20260904-R5GR]] forbids it.

## Interfaces

- Consumes: `TwoPhaseSource` from [[T-20260904-8J7V]]; `Board.extra["base"|"domain"]` from [[T-20260904-VTZ6]].
- Produces: `sources/eightfold.py` in `get_source("eightfold")`.

## Touch paths

```paths
src/jobhunter/sources/eightfold.py
src/jobhunter/sources/__init__.py
tests/sources/test_eightfold.py
tests/sources/fixtures/eightfold_*.json
companies.toml
```

## Non-goals

```paths
src/jobhunter/fetch.py
```
