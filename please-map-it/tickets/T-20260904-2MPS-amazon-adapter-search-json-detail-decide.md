---
id: T-20260904-2MPS
type: feature
state: open
plan: P-20260904-KWVF
milestone: M-20260904-45P2
classification: bounded
appetite: M
backbone_index: 13
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

# Amazon adapter (search.json, detail decided by fixture)

List verified; fixture capture decides whether search results carry full descriptions or a per-job detail call is needed. 10k display cap accepted for now (Q-20260904-9S3R). Spec §4.3.

depends-on:: [[T-20260904-8J7V]] (prerequisite) — third implementation of the TwoPhaseSource protocol

## Acceptance criteria

- [ ] ac-1 | search.json fixture parses rows with stable uids and pagination against `hits` | predicate: `uv run pytest tests/sources/test_amazonjobs.py -k "list" -v` ^ac-1
- [ ] ac-2 | the fixture-decided description path (embedded or per-job detail) yields a PostingVersion whose description contains the fixture posting's qualifications text verbatim | predicate: `uv run pytest tests/sources/test_amazonjobs.py -k "description" -v` ^ac-2
- [ ] ac-3 | the ticket records which path the fixture decided, in Interfaces below | predicate: `bash -c '! grep -q "decided-by-fixture: pending" please-map-it/tickets/T-20260904-2MPS-*.md'` ^ac-3

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- 10k display cap accepted for this ticket ([[Q-20260904-9S3R]] stays open for facet sweeps).

## Known-bad approaches

None known — checked the web.

## Interfaces

- Consumes: `TwoPhaseSource` from [[T-20260904-8J7V]] plus the embedded-detail
  driver mode from [[T-20260906-7PTV]].
- Produces: `sources/amazonjobs.py` in `get_two_phase("amazonjobs")` (embedded
  mode; the one-shot `get_source` guess couldn't survive the 100-row
  result_limit cap); single fixed board `amazonjobs:amazon`.
- decided-by-fixture: embedded — search.json rows carry `description`,
  `basic_qualifications`, `preferred_qualifications` in full (probe
  2026-09-06, id_icims 10530730); the per-job `.json` path serves HTML, so no
  detail endpoint exists. Pagination: offset/result_limit, max 100/page,
  `hits` capped at 10,000.

depends-on:: [[T-20260906-7PTV]] (prerequisite) — versions must come from list rows; without the embedded driver mode every row would sit pending_detail forever

## Touch paths

```paths
src/jobhunter/sources/amazonjobs.py
src/jobhunter/sources/__init__.py
tests/sources/test_amazonjobs.py
tests/sources/fixtures/amazonjobs_*.json
companies.toml
```

## Non-goals

```paths
src/jobhunter/fetch.py
```
