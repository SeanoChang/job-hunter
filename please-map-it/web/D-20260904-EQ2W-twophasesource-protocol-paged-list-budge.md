---
id: D-20260904-EQ2W
type: decision
status: proposed
provenance:
  session: session_01P5FqD7NihBxkuFjggEDWck
  captured_at: 2026-09-04T19:36:04Z
  source: spec
  source_ref: docs/superpowers/specs/2026-09-04-multi-ats-expansion-design.md#3
  actor: agent
---

# TwoPhaseSource protocol: paged list + budgeted per-posting detail

These ATSes list 20 rows/page without descriptions; each description costs one request. New protocol keeps adapters pure, adds page_blobs+details to manifests so archive stays truth, and a 40/run detail budget with 7-day re-checks fits 2,000-posting boards into the hourly cron. Presence exact; edits up to 7 days late. Spec §3.

responds-to:: [[Q-20260904-KMBE]]

## Provenance

> docs/superpowers/specs/2026-09-04-multi-ats-expansion-design.md#3
