---
id: T-20260904-WKVF
type: feature
state: open
plan: P-20260904-KWVF
milestone: M-20260904-FPX5
classification: bounded
appetite: S
backbone_index: 1
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

# http.py: POST bodies and same-host spacing

Paged sources need POST with JSON bodies (Workday CXS) and polite request spacing per host; http.py is GET-only today. Spec §3.2.

## Acceptance criteria

- [ ] ac-1 | fetch() accepts method="POST" with a JSON body and sends Content-Type application/json | predicate: `uv run pytest tests/test_http.py -k "post" -v` ^ac-1
- [ ] ac-2 | consecutive requests to the same host are spaced ≥ the configured spacing_ms; different hosts are not delayed | predicate: `uv run pytest tests/test_http.py -k "spacing" -v` ^ac-2
- [ ] ac-3 | existing GET behavior is byte-identical (full suite green, no call-site changes) | predicate: `uv run pytest -q` ^ac-3

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- Timeouts, bounded retries, size cap, and honest transport verdicts are unchanged for every existing caller.
- One HTTP client module: no second client appears elsewhere.

## Known-bad approaches

None known — checked the web; no rejected option touches the HTTP layer.

## Interfaces

- Produces: `Fetcher.fetch(url, *, method="GET", json_body=None)` and a `spacing_ms` constructor knob, consumed by [[T-20260904-8J7V]].

## Touch paths

```paths
src/jobhunter/http.py
tests/test_http.py
```

## Non-goals

```paths
src/jobhunter/fetch.py
src/jobhunter/sources/**
```
