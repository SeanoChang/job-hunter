---
id: A-20260904-H87J
type: argument
status: proposed
provenance:
  session: session_01P5FqD7NihBxkuFjggEDWck
  captured_at: 2026-09-04T19:36:21Z
  source: spec
  source_ref: docs/superpowers/specs/2026-09-04-multi-ats-expansion-design.md#5
  actor: agent
---

# Hourly CI fetch budget forces detail budgets

The sync step has a 45-minute wall and fetch shares it with extraction. ~1,100 extra list requests/hour plus 40 details/board/run keeps fetch at an estimated 6-9 minutes. The budget mechanism is what makes two-phase sources compatible with the hourly cron at all.

argues:: [[D-20260904-EQ2W]] (+)

## Provenance

> docs/superpowers/specs/2026-09-04-multi-ats-expansion-design.md#5
