---
id: T-20260904-VTZ6
type: feature
state: open
plan: P-20260904-KWVF
milestone: M-20260904-FPX5
classification: bounded
appetite: S
backbone_index: 2
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

# Registry: per-source extra keys (host/site/base/domain)

workday/oraclehcm/eightfold boards carry keys beyond company/source/board; Board gains a frozen extra mapping, registry check validates the per-source required set. Spec §3.5.

## Acceptance criteria

- [ ] ac-1 | a workday board without host/site (and oraclehcm without base/site, eightfold without base/domain) fails `registry check` with a teaching error naming the missing key | predicate: `uv run pytest tests/test_registry.py -k "extra" -v` ^ac-1
- [ ] ac-2 | unknown keys on any board remain an error, and every existing board in companies.toml still validates | predicate: `uv run job-hunter registry check` ^ac-2
- [ ] ac-3 | full suite green | predicate: `uv run pytest -q` ^ac-3

### Manual

Nothing here — the contract is fully commandable.

## Invariants

- `Board` stays frozen; `extra` is an immutable mapping.
- Registry revision hashing covers the new keys (a changed site is a changed revision).

## Known-bad approaches

None known — checked the web; no rejected option touches the registry.

## Interfaces

- Produces: `Board.extra["host"|"site"|"base"|"domain"]`, consumed by [[T-20260904-VJKA]], [[T-20260904-JQXX]], [[T-20260904-YZPA]]. Shapes per [[D-20260904-EQ2W]] and spec §3.5.

## Touch paths

```paths
src/jobhunter/registry.py
src/jobhunter/models.py
tests/test_registry.py
```

## Non-goals

```paths
companies.toml
src/jobhunter/sources/**
```
