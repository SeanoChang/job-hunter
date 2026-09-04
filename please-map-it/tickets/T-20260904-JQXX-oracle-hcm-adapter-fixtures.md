---
id: T-20260904-JQXX
type: feature
state: open
plan: P-20260904-KWVF
milestone: M-20260904-QYDM
classification: bounded
appetite: M
backbone_index: 11
owner: human
priority: P2
severity: normal
model: sonnet
effort: xhigh
review_mode: ask
provenance:
  session: -
  captured_at: 2026-09-04T19:38:09Z
  source: pmi ticket new
  source_ref: plan:P-20260904-KWVF
  actor: agent
---

# Oracle HCM adapter + fixtures

recruitingCEJobRequisitions list (limit 200) + detail finder from the probe; fixtures from JPMC. Spec §4.2.

depends-on:: [[T-20260904-8J7V]] (prerequisite) — second implementation of the TwoPhaseSource protocol
depends-on:: [[T-20260904-3MJK]] (uncertain) — blocked only if the detail finder differs from the spec guess; probe:T-20260904-3MJK

## Acceptance criteria

- [ ] ac-1 | list fixture (real JPMC page) parses rows and TotalJobsCount; pagination math covers limit=200 windows | predicate: `uv run pytest tests/sources/test_oraclehcm.py -k "list" -v` ^ac-1
- [ ] ac-2 | detail fixture (shape pinned by the probe) normalizes to a PostingVersion with full description_html | predicate: `uv run pytest tests/sources/test_oraclehcm.py -k "detail" -v` ^ac-2
- [ ] ac-3 | non-CE response shapes raise EnvelopeError | predicate: `uv run pytest tests/sources/test_oraclehcm.py -k "envelope" -v` ^ac-3

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- No I/O in the adapter; fixtures are recorded real responses.
- Detail request shape comes from the probe's Evidence node, never from the spec guess.

## Known-bad approaches

- Building on the unverified ById finder guess — flagged in spec §7, the probe [[T-20260904-3MJK]] exists to kill it.

## Interfaces

- Consumes: `TwoPhaseSource` from [[T-20260904-8J7V]]; `Board.extra["base"|"site"]` from [[T-20260904-VTZ6]]; the probe's pinned request.
- Produces: `sources/oraclehcm.py` in `get_source("oraclehcm")`, consumed by [[T-20260904-797G]].

## Touch paths

```paths
src/jobhunter/sources/oraclehcm.py
src/jobhunter/sources/__init__.py
tests/sources/test_oraclehcm.py
tests/sources/fixtures/oraclehcm_*.json
```

## Non-goals

```paths
src/jobhunter/fetch.py
companies.toml
```
