---
id: T-20260904-NFPW
type: chore
state: open
plan: P-20260904-KWVF
milestone: M-20260904-FPX5
classification: bounded
appetite: S
backbone_index: 8
owner: human
priority: P2
severity: normal
model: sonnet
effort: low
review_mode: ask
provenance:
  session: -
  captured_at: 2026-09-04T19:38:09Z
  source: pmi ticket new
  source_ref: plan:P-20260904-KWVF
  actor: agent
---

# Policy wording: first-party JSON endpoints amendment

README, root CLAUDE.md and the ingestion spec get the amended posture: structured JSON endpoints only, no HTML scraping, no auth, no challenge bypass, honest UA, budgets, backoff. Public-repo posture change Sean signed off in the spec review. Spec §2.

## Acceptance criteria

- [ ] ac-1 | README and root CLAUDE.md carry the amended posture (first-party JSON endpoints; no HTML scraping; no challenge bypass) | predicate: `grep -il "challenge" README.md CLAUDE.md | wc -l | grep -x 2` ^ac-1
- [ ] ac-2 | the ingestion spec's ruling list gains the dated amendment and docs/README.md indexes it | predicate: `grep -rq "first-party" docs/README.md` ^ac-2

### Manual

- [ ] ac-3 | the public-repo wording reads as a deliberate policy, not a loophole — Sean approves the phrasing | owner: human ^ac-3

## Invariants

- Bound by [[D-20260904-R5GR]] — wording restates nothing beyond the decided posture.

## Known-bad approaches

- Silently bending the old rule without a written amendment — the reason [[D-20260904-R5GR]] exists.

## Interfaces

Nothing here — docs only.

## Touch paths

```paths
README.md
CLAUDE.md
docs/README.md
docs/2026-08-18-ingestion-layer-spec.md
```

## Non-goals

```paths
src/**
```
