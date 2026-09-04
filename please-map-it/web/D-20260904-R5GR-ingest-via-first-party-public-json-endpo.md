---
id: D-20260904-R5GR
type: decision
status: proposed
provenance:
  session: session_01P5FqD7NihBxkuFjggEDWck
  captured_at: 2026-09-04T19:35:40Z
  source: chat
  source_ref: transcript:session_01P5FqD7NihBxkuFjggEDWck#workday-how
  actor: human
---

# Ingest via first-party public JSON endpoints; policy amended

The 'official ATS APIs, no scraping' founding rule is relaxed to: structured JSON endpoints only — official APIs first, else the endpoint the company's own careers page calls. Still no HTML scraping, no auth, no challenge bypass; honest UA, budgets, backoff. One-way door: public-repo posture change, amended in the ingestion spec. Design detail: docs/superpowers/specs/2026-09-04-multi-ats-expansion-design.md §2.

responds-to:: [[Q-20260904-KMBE]]

## Provenance

> "we need to find some way to include nvidia in our job hunt data set. all the large companies, google, nvidia, meta, amazon, etc should all be included. how to get the workday data?" — transcript:session_01P5FqD7NihBxkuFjggEDWck#workday-how
