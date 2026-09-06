---
id: T-20260906-TC43
type: feature
state: open
plan: P-20260906-PK6N
classification: bounded
appetite: M
backbone_index: 3
owner: human
priority: P2
severity: normal
model: sonnet
effort: medium
review_mode: ask
provenance:
  session: 7309c626-e091-47fa-889c-466cb4b0249a
  captured_at: 2026-09-06T14:38:57Z
  source: pmi capture planwrite
  source_ref: docs/superpowers/plans/2026-09-06-m3-quality-loop.md#Task 3
  actor: agent
---

# demote-only refuter

`extract refute` verb + runner hook: an engine pass re-reads a validated row's document and claims and returns refute/uphold with cited reasons; a refute verdict appends the review event to `reviews/` then demotes the row to `needs_review`. Uphold writes provenance only. Never promotes, never touches non-validated rows.

## Acceptance criteria

- [ ] ac-1 | a refute verdict appends the review event to the archive before the row demotes to needs_review | predicate: `uv run pytest tests/l2/test_refuter.py -v` ^ac-1
- [ ] ac-2 | uphold writes provenance only and the verb refuses non-validated rows | predicate: `uv run pytest tests/l2/test_refuter.py -k "uphold or refuses" -v` ^ac-2

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- Demote or annotate, never promote (spec §4.4).
- Runs under EXTRACT_LOCK_KEY like every review verb.

## Known-bad approaches

Automated promotion is the self-poisoning loop the spec forbids — not an option, not a fallback.

## Interfaces

- Produces: `extract refute` CLI verb + runner hook; verdict object archived under reviews/.

## Touch paths

```paths
src/jobhunter/l2/refuter.py
src/jobhunter/cli.py
tests/l2/test_refuter.py
```

## Non-goals

```paths
src/jobhunter/store/schema.sql
src/jobhunter/l2/agreement.py
```
