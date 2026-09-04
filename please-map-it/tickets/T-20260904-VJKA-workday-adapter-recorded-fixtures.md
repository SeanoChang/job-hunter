---
id: T-20260904-VJKA
type: feature
state: open
plan: P-20260904-KWVF
milestone: M-20260904-FPX5
classification: bounded
appetite: M
backbone_index: 6
owner: human
priority: P2
severity: normal
model: sonnet
effort: xhigh
review_mode: ask
provenance:
  session: -
  captured_at: 2026-09-04T19:37:49Z
  source: pmi ticket new
  source_ref: plan:P-20260904-KWVF
  actor: agent
---

# Workday adapter + recorded fixtures

CXS list POST (20/page, uid=bulletFields[0], externalPath fallback) and detail GET (jobPostingInfo.jobDescription to description_html). Fixtures from nvidia.wd5/NVIDIAExternalCareerSite. Spec §4.1.

depends-on:: [[T-20260904-8J7V]] (prerequisite) — implements the TwoPhaseSource protocol that ticket defines
depends-on:: [[T-20260904-VTZ6]] (prerequisite) — reads host and site from the registry extra keys

## Acceptance criteria

- [ ] ac-1 | list fixture (real nvidia.wd5 page) parses to rows with uid from bulletFields[0], externalPath fallback exercised, and the correct total | predicate: `uv run pytest tests/sources/test_workday.py -k "list" -v` ^ac-1
- [ ] ac-2 | detail fixture normalizes jobPostingInfo into a PostingVersion whose description_html contains the salary-range sentence verbatim | predicate: `uv run pytest tests/sources/test_workday.py -k "detail" -v` ^ac-2
- [ ] ac-3 | an envelope that is not CXS shape raises EnvelopeError, not a crash | predicate: `uv run pytest tests/sources/test_workday.py -k "envelope" -v` ^ac-3

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- No I/O in the adapter; fixtures are recorded real responses, like every `tests/sources/` neighbor.
- Normalization helpers come from sources/base.py; identity/hashing only via hashing.py.

## Known-bad approaches

- Assuming board identity from token/tenant names — [[E-20260904-RDGJ]]: fixtures must come from the verified NVIDIA endpoint.

## Interfaces

- Consumes: `TwoPhaseSource` protocol from [[T-20260904-8J7V]]; `Board.extra["host"|"site"]` from [[T-20260904-VTZ6]].
- Produces: `sources/workday.py` registered in `get_source("workday")`, consumed by [[T-20260904-AA7Z]].

## Touch paths

```paths
src/jobhunter/sources/workday.py
src/jobhunter/sources/__init__.py
tests/sources/test_workday.py
tests/sources/fixtures/workday_*.json
```

## Non-goals

```paths
src/jobhunter/fetch.py
companies.toml
```
