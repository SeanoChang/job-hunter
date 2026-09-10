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

## Architecture reviews

- `2026-09-06-l2-data-quality-audit.md` — **current analysis, advisory**:
  read-only audit of all stored L2 states, live MCP source/profile comparisons,
  demonstrated semantic and completeness defects, and prioritized quality gates.
  Raw snapshot data stays local under `data/l2-audit-2026-09-06/`.
- `2026-09-06-architecture-review.md` — **current analysis, advisory**: reframes
  the user problem, evaluates implementation at `72f1a1b`, reproduces evidence
  and recovery failures, and prioritizes improvements. Includes runnable
  probes in `review-evidence/architecture-probes.py`; does not supersede the
  normative designs below.

## Design documents

- `superpowers/specs/2026-09-07-parsing-contract-v2-design.md` — **approved,
  normative for schema-v2 semantics** (amended [A1]–[A4] after independent
  verification, 2026-09-07): typed statements, scoped facts, claim-linked
  mentions, source completeness, audit/repair prompts, v1 coexistence, and
  twelve regression contracts.
  `superpowers/plans/2026-09-07-parsing-v2-offline-contract.md` increment 1
  (offline contract) is **shipped**: `l2/v2/` pure modules, schema `2`,
  `blocks/1` source annotation, `validator/10`, the v1 floor-grammar repair
  (`validator/9`), and the twelve case contracts plus synthetic minimal
  pairs — all no model calls, no database/archive I/O.
  `superpowers/plans/2026-09-10-v2-cutover-local.md` ships increment 2's
  **harness slice**: the `Bundle` engine tuple, prompt `demand-profile/v6`
  over annotated blocks, the v2 bundle end to end (emit → record → verify →
  stored profile + statement-derived `profile_mentions`), shape-aware
  `pulse`/`extract show`, and archive reads moved out of the write
  transaction. Still **open** from increment 2: the `semantic-audit/v1` and
  `semantic-repair/v1` prompts and the audit/repair loop — the deterministic
  verifier plus the quality gate carry correctness until they land. Note what
  that gate costs while they are open: `assemble` leaves `semantics` and
  `completeness` at `not_checked`, so no offline v2 record is
  `search_eligible` and the `profile_mentions` projection yields **no rows at
  all**. The v2 profile blob is populated; the v2 mention aggregate is empty by
  construction until the auditor lands, which makes `semantic-audit/v1` a
  prerequisite of cutover rather than a fast follow.
  Increment 3 (persistence + explicit reads: the additive migration, the
  richer per-claim table, v2 views in `cli_q`/`mcp`) is still design only;
  storage is unmigrated and the hosted MCP is untouched. Frozen identifiers:
  `blocks/1`, schema `2`, `validator/9` (v1) / `validator/10` (v2),
  `demand-profile/v6`, `parsing-rules/2`, `aliases/1`; any further change to
  these bumps rather than edits in place.
- `superpowers/specs/2026-09-02-hosted-mcp-design.md` — **current, normative
  for the hosted read surface**: the MCP wrapper over `views.py`, static-bearer
  auth, server-side pulse cursors (schema v4 `mcp_cursors`), Cloud Run deploy
  declared in `infra/`. Operated by `runbooks/2026-09-02-deploy-mcp.md`.
- `superpowers/specs/2026-09-01-agentic-cli-rework-design.md` — **current,
  normative for the CLI surface**: the JSON envelope, typed exit codes, teaching
  errors, `pulse` cursors, the `q` namespace, `sync`/`doctor`/`schema`/`skill`,
  and `profile_mentions`. Supersedes the CLI sections of the ingestion spec and
  the `q` verb table in the L2 harness doc (whose rulings it keeps).
- `runbooks/2026-09-09-local-codex-drain.md` — **current**: the queue-dump →
  local codex-cli drain → encrypted-outbox ingest loop that extracts on the
  owner's machine and uploads through CI (no store credentials locally);
  includes the single-writer and validator-replay invariants.
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
  next-step of the parsing-direction doc. Amended 2026-09-04 (see below).
- `superpowers/specs/2026-09-04-multi-ats-expansion-design.md` — **current,
  approved.** §2 is the in-force policy amendment: ingestion widens from
  official ATS APIs only to official ATS APIs and other first-party structured
  JSON endpoints (Workday CXS, Oracle Recruiting Cloud, amazon.jobs,
  SmartRecruiters, Eightfold); still no HTML scraping, no authentication, no
  bypassing bot challenges. The rest of the design (two-phase source
  architecture, the five adapters) is being built as plan P-20260904-KWVF.
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
- `superpowers/plans/2026-09-01-agentic-cli-rework.md` — the agent-first CLI:
  the output contract (`cli_output.py`), config file layering, the `q`
  namespace, `pulse` with client-side cursors, schema v3 `profile_mentions`,
  `sync`, `doctor`, `schema`, `skill`. Deletes `report` and the `--json` flag
  (both listed above as shipped by earlier increments).
- `superpowers/plans/2026-09-02-hosted-mcp.md` — the hosted MCP server: payload
  assembly extracted to `views.py`, schema v4 `mcp_cursors` + `store/mcp_state.py`,
  the FastMCP app with bearer auth and eight tools, packaging, and the Terraform
  config in `infra/`.
- `superpowers/plans/2026-09-07-parsing-v2-offline-contract.md` — parsing v2
  increment 1 (shipped, offline only): the `l2/v2/` pure modules — source
  block annotation (`blocks/1`), typed derivation grammars (`validator/10`),
  emit→record assembly, the `(record, markdown)` verifier, quality
  dimensions, mention/statement projection — schema `2`, plus the v1
  floor-grammar repair (`validator/9`) and the twelve case contracts with
  seven synthetic minimal pairs. Frozen identifiers: `blocks/1`, schema `2`,
  `validator/9`/`validator/10`, `parsing-rules/2`, `aliases/1`. Zero model
  calls, zero database/archive I/O; the runner, CLI, and MCP are untouched.
- `superpowers/plans/2026-09-10-v2-cutover-local.md` — parsing v2 increment 2,
  harness slice (shipped, local): `l2/bundles.py` — the engine tuple (prompt,
  schema, validator, assembly, verifier, both storage projections) as one
  selectable `Bundle`, with `get_bundle_for_tuple` so a mixed archive replays
  under the bundle that judged each attempt; `l2/v2/prompt.py`
  (`demand-profile/v6` over a numbered `blocks/1` listing); `l2/v2/serve.py`
  (stored slice with its `"schema": "2"` marker, `profile_mentions` rows whose
  importance comes from the linked statement, a `summary()` matching
  `pulse.profile_summary`'s keys); shape-aware `pulse` and `extract show`; and
  the [A1] fix — archive GETs never hold an open write transaction. The bundle
  is selected by `JOB_HUNTER_L2_BUNDLE` (`v1` default); the live A/B gate and
  the local flip to `v2` are the operator procedure in
  `runbooks/2026-09-09-local-codex-drain.md`. Two read-path prerequisites of
  any real flip are NOT shipped with it: (1) the read path still takes "the
  tuple in force" from v1's module constants (`views.profile_row`,
  `views.claims_view`, `pulse`), so serving does not follow the selected
  bundle; (2) a v2 run writes zero `profile_mentions` rows, because the quality
  gate holds every unaudited record ineligible — so fixing (1) alone points
  `q claims` at an empty tuple and it returns nothing corpus-wide until
  `semantic-audit/v1` ships. No schema migration, no MCP or CI change, no
  GitHub activity. The 2026-09-10 live A/B failed its gate and cutover is
  halted (`please-map-it/tickets/T-20260910-3D6M-benchmark-cutover-docs.md`).

Not built yet: M3 alerting (attention digests via generic webhook), the
concept linker (L3), and the workspace/tracker faces.

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
- 2026-09-02 — a drain outlives its database connection: the runner reconnects
  and re-takes the extract lock mid-run (aborting as `lock_held` with
  `aborted: "lock_lost"` if another writer has it — the counters and the spend
  it already made stay on the summary, so that run is never described as
  "nothing done"), and cleanup on a dead connection never replaces the failure
  that killed the run — including a LockLost raised while recording an
  `engine_fatal` attempt, which re-raises the engine failure instead.
  A reconnecting run re-applies its OWN uncommitted
  writes from a per-run journal, because the catch-up scan cannot: its
  watermark is `max(started_at)` over committed rows, so an attempt rolled
  back alongside a later one that committed sits behind the watermark forever.
  A provider refusing the request (401/402/403) is the
  new `engine_fatal` attempt outcome — recorded once, never retried or
  laddered, and reported as an engine error carrying the status and the
  provider's message.
- 2026-09-02 — hosted MCP: the server reuses the package rather than
  reimplementing the read surface (`views.py` is the shared payload assembly,
  parity is a test, not a promise); pulse watermarks live in the store for the
  hosted server only — a deliberate bend of "personal state stays on the
  client", because the server is owner infrastructure and the state is one
  timestamp; auth is a static bearer, Google IAM stays open because an MCP
  client cannot send a Google identity token; deploy is Cloud Run declared in
  Terraform (`infra/`), never hand-run gcloud, and secret *values* never enter
  tf state; Cloudflare Containers rejected at a $5/mo baseline against Cloud
  Run's free tier.
- 2026-09-10 — the extraction engine tuple is a named bundle
  (`JOB_HUNTER_L2_BUNDLE`, `l2/bundles.py`), and the tuple keys the corpus
  partition: switching bundles re-extracts rather than resumes, both tuples
  coexist in `extractions`, and rollback is selecting the
  previous bundle — never deleting or relabelling rows (spec §10). A cutover
  is gated on a live A/B against the bundle in force (v2's quarantine rate at
  most half of v1's on the same sample), run with the drain loop paused so one
  writer holds the lock; the fixture suite alone never authorizes a flip. That
  A/B baseline is always computed under the v1 tuple in force, never as an
  unfiltered `status='quarantined'` scan, which sweeps in retired tuples and
  inflates the rate. In v2, importance belongs to a statement, never to the
  presentation area a mention sits in — `profile_mentions` takes it from the
  linked statement, and only for a `search_eligible` record: the aggregate is a
  corpus-wide assertion, so an unaudited record populates the profile blob and
  contributes nothing to the claim index.
- 2026-09-04 — ingestion policy amended: official ATS APIs only widens to
  official ATS APIs and, absent one, the first-party structured JSON endpoint
  the company's own careers page calls (Workday CXS, Oracle Recruiting Cloud,
  amazon.jobs search.json, SmartRecruiters, Eightfold). Never HTML scraping,
  never authentication, never bypassing a bot challenge or rate limit; every
  request carries an honest User-Agent, per-host request spacing, a per-board
  detail budget, and backoff on errors. A blocked or challenged board is
  marked `blocked`, never retried around.
