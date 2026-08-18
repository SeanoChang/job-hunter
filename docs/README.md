# job-hunter docs — index and status

Last consolidated 2026-08-18. One line per document: what it is, and whether it
is the current statement, partially superseded (with a status note inside), or
historical. When two documents disagree, the one marked **current** wins.

## The design as of 2026-08-17, in one paragraph

job-hunter is a local-first, bring-your-own-agent job-hunting kit (CLI + MCP +
skills), experimental and for personal use first. Postings are fetched from
official ATS APIs into an immutable raw archive; each version is normalised to
**Markdown as the only canonical text**; closed-vocabulary facts (money, dates,
durations) are anchored by code; an LLM extracts an **evidence-first demand
profile** once per version — areas of atomic verbatim claims with their own
importance/level/threshold, a recursive structure over claims, and mentions; a
generated description is an optional projection labelled with what produced it.
Mentions are linked afterwards to a concept registry that grows from the corpus.
The LLM is the labeler from day one and each extraction sub-task is designed to
be replaced by a small model as labels accumulate. Matching is
description-vs-description by a judge over the shortlist, never a keyword score;
per-requirement verdicts carry evidence and distinguish `not_demonstrated` from
`contradicted`. Regex is retired as a vocabulary source. Details and
dispositions of the 2026-08-17 external review:
`2026-08-17-parsing-direction.md`.

## Design documents

- `2026-08-18-ingestion-layer-spec.md` — **current, normative.** Ingestion
  layer: R2 archive, artifact identities, SQLite store schema, lifecycle
  algorithm with drop guard and interval-censored closes, CLI, deployment,
  testing. Resolves the first next-step of the parsing-direction doc.
- `2026-08-17-parsing-direction.md` — **current, canonical.** Parsing model,
  unified record, engine choice, external-review dispositions.
- `2026-08-17-parsing-vs-other-tools.md` — current. How 16 tools ingest and
  parse postings; where ours is better or not.
- `2026-08-16-parsing-prototype-report.md` — superseded (its addendum says so).
  Rule-parser prototype results on two postings; historical evidence.
- `2026-08-15-data-model.html` — partially superseded (status callout inside).
  Posting record, derived layer, failure modes, testing strategy.
- `2026-08-09-data-exploration.md` — partially superseded (note inside). Store,
  MCP shape, open decisions; identities now resolved.
- `2026-08-08-stage1-ingestion-context.md` — historical (note inside). Original
  stage-1 ingestion briefing.
- `sources/README.md` and `sources/*.md` — current (revision note inside). Real
  ATS payloads compared; unified posting record draft.

## Research memos (`research/`)

All still current as research; none define the design.

- `2026-08-16-resume-matching-landscape.md` — documented failure modes of
  matchers; judge verdicts as distributions; input gates.
- `2026-08-15-asia-sources.md` — fetchable JP/TW/SG/HK sources; CJK plumbing.
- `2026-08-14-benchmark-precedents.md` — reusable extraction datasets and eval
  harnesses.
- `2026-08-14-ats-coverage.md` — share of postings reachable via big-3 APIs and
  JSON-LD.
- `2026-08-11-competitor-weaknesses.md` — what users complain about in
  career-ops, ai-job-search, Jobscan and others.
- `2026-08-11-market-research-demand.md` — demand evidence and positioning
  (temporal history, ghost jobs).
- `2026-08-09-industry-data-for-llm-mcp.md` — bi-temporal store, MCP temporal
  verbs, FTS/embedding choices.
- `2026-08-08-understanding-postings.md` — field-level source priority,
  taxonomies.
- `2026-08-08-posting-lifecycle-tracking.md` — change events, repost detection,
  ghost score.
- `2026-08-08-labor-market-analytics.md` — versioned company panel for credible
  analytics.

## Prototype code

`prototypes/parsing/` — the retired rule-based tier-1 parser, four fixtures, the
24-bullet regression gold, the `claude -p --json-schema` structured-call wiring,
and a dated judge run. Its README carries the superseded banner. The
demand-profile extractor (L2), the HTML→Markdown step, and the linker are not
built yet.

## Standing rulings (chronological, all still in force)

- 2026-08-08 — roadmap smaller→larger→universal; temporal tracking from day one;
  datacore is reference only; bring-your-own-agent kit, not an agent.
- 2026-08-15 — mainland China out of scope; JP/TW/SG/HK in.
- 2026-08-16 — experimental/personal use first; effort goes to parsing;
  extraction is not keyword ticking; TS is a language, React a framework
  (facets, typed edges); scoring is model-based; no 0–100 match score.
- 2026-08-17 — regex cannot be exhausted over 1M JDs; descriptions of skill, not
  levels; the demand description is the most important extraction; the LLM is
  not the only solution — record must be evidence-first, LLM as labeler, small
  models later; external review's eight findings adopted.
