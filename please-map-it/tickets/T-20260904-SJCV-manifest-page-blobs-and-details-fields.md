---
id: T-20260904-SJCV
type: feature
state: open
plan: P-20260904-KWVF
milestone: M-20260904-FPX5
classification: bounded
appetite: S
backbone_index: 3
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

# Manifest: page_blobs and details fields

Two-phase boards archive N list pages and per-posting detail blobs under one attempt manifest; both fields optional so old manifests stay valid and is_healthy/replay treat absence exactly as today. Spec §3.3.

## Acceptance criteria

- [ ] ac-1 | a manifest with page_blobs and details round-trips through serialize/parse; one without them parses exactly as today | predicate: `uv run pytest tests/archive/test_manifests.py -v` ^ac-1
- [ ] ac-2 | is_healthy treats a two-phase manifest (blob_sha256 null, page_blobs present) as healthy when transport is ok | predicate: `uv run pytest tests/test_fetch.py -k "healthy" -v` ^ac-2
- [ ] ac-3 | full suite green (old manifests in fixtures unaffected) | predicate: `uv run pytest -q` ^ac-3

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- Manifests stay write-once; both new fields optional; absent fields behave exactly as before this ticket.
- Every detail blob is content-addressed and archived before any store write (archive-first, [[D-20260904-EQ2W]]).

## Known-bad approaches

None known — checked the web.

## Interfaces

- Produces: `AttemptManifest.page_blobs: tuple[str, ...] | None`, `AttemptManifest.details: tuple[DetailAttempt, ...] | None` (uid, blob_sha256, http_status, error), consumed by [[T-20260904-8J7V]] and [[T-20260904-PZZQ]].

## Touch paths

```paths
src/jobhunter/archive/manifests.py
src/jobhunter/models.py
tests/archive/test_manifests.py
```

## Non-goals

```paths
src/jobhunter/store/**
src/jobhunter/fetch.py
```
