---
id: T-20260904-9GQ8
type: feature
state: open
plan: P-20260904-KWVF
milestone: M-20260904-45P2
classification: bounded
appetite: S
backbone_index: 14
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

# SmartRecruiters adapter + 4 boards

Official documented public API: postings list + posting detail (jobAd.sections). Boards: Grab, Canva, Wix, Snap (Snap count suspiciously low — re-verify at add). Spec §4.4.

depends-on:: [[T-20260904-8J7V]] (prerequisite) — runs on the two-phase driver

## Acceptance criteria

- [ ] ac-1 | postings-list fixture parses rows and totalFound pagination | predicate: `uv run pytest tests/sources/test_smartrecruiters.py -k "list" -v` ^ac-1
- [ ] ac-2 | posting-detail fixture normalizes jobAd.sections into description_html | predicate: `uv run pytest tests/sources/test_smartrecruiters.py -k "detail" -v` ^ac-2
- [ ] ac-3 | registry validates with Grab, Canva, Wix, Snap added — Snap's low count (20) re-verified and noted at add time | predicate: `uv run job-hunter registry check` ^ac-3

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- Official documented API — the one family here with a stability promise; keep the adapter free of undocumented params.

## Known-bad approaches

None known — checked the web.

## Interfaces

- Consumes: `TwoPhaseSource` from [[T-20260904-8J7V]].
- Produces: `sources/smartrecruiters.py` in `get_source("smartrecruiters")`; boards grab, canva, Wix2, SNAPInc1.

## Touch paths

```paths
src/jobhunter/sources/smartrecruiters.py
src/jobhunter/sources/__init__.py
tests/sources/test_smartrecruiters.py
tests/sources/fixtures/smartrecruiters_*.json
companies.toml
```

## Non-goals

```paths
src/jobhunter/fetch.py
```
