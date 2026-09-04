---
id: T-20260904-8J7V
type: feature
state: open
plan: P-20260904-KWVF
milestone: M-20260904-FPX5
classification: architectural
appetite: M
backbone_index: 4
owner: human
priority: P2
severity: normal
model: opus
effort: xhigh
review_mode: ask
provenance:
  session: -
  captured_at: 2026-09-04T19:37:49Z
  source: pmi ticket new
  source_ref: plan:P-20260904-KWVF
  actor: agent
---

# TwoPhaseSource protocol + fetch.py driver with detail budget

The architectural core: pure list_url/parse_list/detail_url/normalize_detail protocol, fetch.py pages the list, budgets details (new-first then staleness sweep), archives everything before store writes. Ships dark. Spec §3.2, §3.4.

depends-on:: [[T-20260904-WKVF]] (prerequisite) — driver issues POST list requests and spaced detail GETs through http.py

## Acceptance criteria

- [ ] ac-1 | the driver pages a fake list to `total`, stops at the page cap, and archives every page blob before ingest sees anything | predicate: `uv run pytest tests/test_fetch.py -k "two_phase and (pages or cap)" -v` ^ac-1
- [ ] ac-2 | detail budget spends new-uids first, then details older than redetail_days, and never exceeds the per-board budget | predicate: `uv run pytest tests/test_fetch.py -k "two_phase and budget" -v` ^ac-2
- [ ] ac-3 | a 403 or challenge response marks the board blocked in the manifest and skips it without retry | predicate: `uv run pytest tests/test_fetch.py -k "two_phase and blocked" -v` ^ac-3
- [ ] ac-4 | ships dark: with no two-phase boards registered, the full suite is green and untouched | predicate: `uv run pytest -q` ^ac-4

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- Adapters stay pure: all I/O in fetch.py; a TwoPhaseSource never touches the network.
- Archive-first ordering per [[D-20260904-EQ2W]]; one manifest per board per run, write-once.
- A full detail sweep per run is forbidden (rejected: [[O-20260904-X6EC]]).

## Known-bad approaches

- Full hourly detail sweep — [[O-20260904-X6EC]]: up to 2,000 requests/board/run, outside the CI budget ([[A-20260904-H87J]]).

## Interfaces

- Consumes: `Fetcher.fetch(..., method, json_body)` from [[T-20260904-WKVF]]; manifest fields from [[T-20260904-SJCV]].
- Produces: `TwoPhaseSource` protocol (`list_url(board, offset)`, `parse_list(body) -> ListPage`, `detail_url(board, row)`, `normalize_detail(body, row, board) -> PostingVersion`) implemented by [[T-20260904-VJKA]], [[T-20260904-JQXX]], [[T-20260904-2MPS]], [[T-20260904-9GQ8]], [[T-20260904-YZPA]].

## Touch paths

```paths
src/jobhunter/sources/base.py
src/jobhunter/fetch.py
tests/test_fetch.py
```

## Non-goals

```paths
src/jobhunter/store/**
src/jobhunter/sources/workday.py
```
