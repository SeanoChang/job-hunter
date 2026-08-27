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

- `2026-08-26-l2-extraction-harness.md` — **current, normative** for the L2
  layer: machine-verifiable evidence format + standalone verifier, extraction
  lifecycle state machine and runner, drift control + weekly consolidation
  checkpoint, agent access verbs and trust rings, threat model, engine
  choices (verified 2026-08-26). The record format itself stays ruled by the
  parsing-direction doc.
- `2026-08-25-durability-and-serving.md` — draft. Dead-man's switch, redundant
  schedulers, checkpointed rebuild, nightly snapshots; public serving ruled as
  snippets + attribution.
- `2026-08-18-ingestion-layer-spec.md` — **current, normative.** Ingestion
  layer: one hosted corpus, R2 archive as truth, artifact identities, Postgres
  (Neon) store with presence intervals, lifecycle algorithm with drop guard and
  interval-censored closes, CLI, deployment, testing. Resolves the first
  next-step of the parsing-direction doc.
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

## Code

`src/jobhunter/` — the ingestion layer, built to
`2026-08-18-ingestion-layer-spec.md` in two increments, both shipped:

- `superpowers/plans/2026-08-18-ingestion-increment-1-archive.md` — registry,
  the three ATS fetchers, the immutable archive (manifests + gzipped blobs on
  local FS or S3/R2), `fetch`, `status`, `archive ls`, `registry check`.
- `superpowers/plans/2026-08-18-ingestion-increment-2-store.md` — the Postgres
  store: `version_hash`, the HTML→Markdown converter (L0, `md/1`), the company
  panel, `Ingestor.ingest` with presence intervals, the drop guard and
  interval-censored closes, `ingest`, `rebuild`, `report`, `registry list`,
  `db init|version`.
- `superpowers/plans/2026-08-26-l2-increment-1-verifier.md` — L2 increment 1
  (shipped): `l2/` verifier — quote objects with codepoint spans, deterministic
  span resolution, versioned fact transforms, JSON schemas v1, the pure
  `verify()` check suite, and the file-based `verify` CLI. `validator/1` frozen.

- `superpowers/plans/2026-08-27-l2-increment-2-harness.md` — L2 increment 2
  (shipped): the extraction runner — engines (openai-compat + claude-cli,
  observed model ids), prompt `demand-profile/v1`, per-attempt archive
  objects, pure state derivation, store schema v2 (attempts/reviews/
  extractions + queue), ladder escalation with breaker and caps, catch-up
  scan, `extract run|review|rebuild`, store-addressed `verify`, `status`
  extraction block. Serial in M2; k-sampling and the quality loop are M3.

Not built yet: the M3 quality loop (k-sampling, refuter, consolidate,
alerts), M4 access verbs, and the concept linker (L3).

## Prototype code

`prototypes/parsing/` — the retired rule-based tier-1 parser, four fixtures, the
24-bullet regression gold, the `claude -p --json-schema` structured-call wiring,
and a dated judge run. Its README carries the superseded banner.

## Standing rulings (chronological, all still in force)

- 2026-08-08 — roadmap smaller→larger→universal; temporal tracking from day one;
  datacore is reference only; bring-your-own-agent kit, not an agent.
- 2026-08-15 — mainland China out of scope; JP/TW/SG/HK in.
- 2026-08-16 — experimental/personal use first; effort goes to parsing;
  extraction is not keyword ticking; TS is a language, React a framework
  (facets, typed edges); scoring is model-based; no 0–100 match score.
- 2026-08-18 — one hosted corpus, users are CLI/MCP clients (nobody self-hosts
  the DB); personal data stays on the client; R2 archive is truth; Postgres on
  Neon replaces SQLite; presence intervals, not per-sample observations.
- 2026-08-17 — regex cannot be exhausted over 1M JDs; descriptions of skill, not
  levels; the demand description is the most important extraction; the LLM is
  not the only solution — record must be evidence-first, LLM as labeler, small
  models later; external review's eight findings adopted.
- 2026-08-26 — L2 harness: evidence as quote objects (codepoint spans; the LLM
  never computes offsets; no fuzzy repair); one verifier, `validator_version`
  in the engine tuple; `model` observed from responses, never configured;
  extraction is stateless (no L2 input derived from any L2 output); aggregates
  are validated-only within one engine tuple; automated verdicts demote only —
  promotion is human; scheduled runs on free/cheap API engines, the
  subscription only in supervised local sessions (no OAuth token in CI);
  whole-extraction status in v1; monitoring stays minimal until a first trend
  is published. Second round: tiered escalation ladder (ordered candidates,
  intra-run, ladder hash keys series that span rungs); human review via CLI
  dossier pipeline with document-only gold labeling; attention digests via
  generic webhook (Slack-compatible), distinct from the liveness ping.
- 2026-08-27 — codex-cli is a supported extraction engine, but only fully
  isolated (--ignore-user-config kills its MCP servers and plugin skills;
  an agentic engine cannot be an extraction engine). It reports no model
  id, so recording the requested one is opt-in
  (JOB_HUNTER_L2_TRUST_REQUESTED_MODEL) and marked as asserted, not
  observed, provenance.
