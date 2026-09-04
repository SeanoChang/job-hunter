---
id: T-20260904-PZZQ
type: feature
state: open
plan: P-20260904-KWVF
milestone: M-20260904-FPX5
classification: bounded
appetite: M
backbone_index: 5
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

# Ingest: pending_detail presence, version-on-detail

List rows create postings with current_version_hash NULL and presence parse_status pending_detail; a fetched detail creates the version/document and flips it. L2 queue unchanged (documents only). Spec §3.4.

depends-on:: [[T-20260904-SJCV]] (prerequisite) — ingest reads page_blobs and details from the manifest shape T3 defines

## Acceptance criteria

- [ ] ac-1 | a list-only manifest creates a posting with current_version_hash NULL and presence parse_status pending_detail | predicate: `uv run pytest tests/store/test_lifecycle.py -k "pending_detail" -v` ^ac-1
- [ ] ac-2 | ingesting a detail blob creates the version+document and flips the posting to it; re-ingesting the same detail is a no-op | predicate: `uv run pytest tests/store/test_lifecycle.py -k "version_on_detail" -v` ^ac-2
- [ ] ac-3 | a uid absent from the next list snapshot closes exactly like today's boards; rebuild replays both paths from the archive | predicate: `uv run pytest tests/store/ -k "two_phase" -v` ^ac-3

### Manual

Nothing here — the contract is fully commandable (store tests need local Postgres).

## Invariants

- All writes through store/lifecycle.py under the advisory lock; readers untouched.
- The L2 queue keys on documents only — a pending_detail posting never enters it.
- The store stays rebuildable from the archive alone ([[D-20260904-EQ2W]]).

## Known-bad approaches

None known — checked the web.

## Interfaces

- Consumes: manifest `page_blobs`/`details` from [[T-20260904-SJCV]] as driven by [[T-20260904-8J7V]].
- Produces: `parse_status="pending_detail"` semantics observed by [[T-20260904-AA7Z]]'s verification.

## Touch paths

```paths
src/jobhunter/ingest.py
src/jobhunter/store/lifecycle.py
src/jobhunter/rebuild.py
tests/store/test_lifecycle.py
```

## Non-goals

```paths
src/jobhunter/l2/**
src/jobhunter/store/queries.py
```
