# Context: designing the job-data ingestion pipeline (stage 1)

Briefing for design discussion. State as of 2026-08-08. **Historical** — the
product was reframed on 2026-08-08 (bring-your-own-agent kit) and parsing was
redesigned on 2026-08-17; two corrections to the reconcile step recorded there
(reconcile on observed source ids, fix the empty-board guard) apply to the
pipeline sketched below. Current design: `2026-08-17-parsing-direction.md`.

## The project

**job-hunter** — an open-source, self-hosted, agentic-first job research tool. Runs on
the user's own hardware; personal data never leaves the machine. Ethos constraints:
ingest only from official ATS APIs (no LinkedIn/Indeed scraping), no auto-apply, no
redistribution of the corpus. The repo currently contains only a README; planning was
reset on 2026-08-08 and this pipeline is the first thing to be built.

## Decisions already made

**Roadmap is a scale progression** (smaller → larger → universal):

1. **Stage 1 (now):** personal-scale automated ingestion pipeline.
2. **Stage 2a:** batch LLM harness — scheduled agent stages doing structure extraction,
   profile scoring, and a daily ranked shortlist over the store.
3. **Stage 2b:** interactive agent with query tools, plus grounded résumé/outreach
   tailoring checked by an adversarial verifier.
4. **Stage 3 (universal):** research arm publishing job-market / economic-trend analysis
   from the accumulated corpus; open-source hardening for other self-hosters; warm
   referrals (collaboration-path discovery).

**Build style: deep foundation.** Each stage built properly in order — not a tracer
bullet. One deliberate early investment: **temporal tracking from day one**, because
trend research needs history that cannot be backfilled.

**A previous prototype exists but is reference-only.** An earlier package ("datacore")
implemented ingestion + a temporal store (DuckDB) and shipped with ~44 green tests. The
decision is to rebuild clean inside job-hunter and carry over its *lessons* as
requirements, not its code:

- Per-record failure isolation — one malformed posting logs an error, never aborts a run.
- Total-failure reconcile guard — a fetch that wholly failed closes nothing, so a broken
  feed can't mass-close jobs.
- Content-hash dedup — unchanged content bumps `last_seen` only.
- Packaging gotcha: DuckDB needs `pytz` installed to return timezone-aware datetimes
  from `TIMESTAMPTZ` columns.

## Stage 1 shape as approved (high level)

Python 3.12+ managed with uv. Bounded modules:

- `sources/` — one adapter per ATS (Greenhouse, Lever, Ashby; Workday deferred), each
  implementing the same small protocol: given a board identifier, yield raw postings.
- `registry` — loads and validates a hand-curated `companies.toml`
  (company → ATS type + board token). The only file edited to grow the corpus.
- `normalize` — raw ATS payload → one common typed `Posting` model; raw JSON kept
  alongside so later stages can revisit the source.
- `store/` — temporal store behind a small interface; DuckDB implementation first.
  Sketched tables: `postings` (normalized fields + raw payload + content_hash +
  first_seen / last_seen / closed_at), `posting_versions` (append-only history when
  content changes), `runs` (per-run counts and errors).
- `pipeline` — orchestrates a run: fetch → normalize → upsert → reconcile (postings
  absent from a healthy fetch get `closed_at`).
- `report` / `cli` — `job-hunter ingest` and `job-hunter report` (new / changed / closed
  since last run, simple facet filters).

Runs are idempotent so scheduling stays external (manual first, then launchd/cron).
TDD with recorded API fixtures — no live calls in tests.

**Non-goals for stage 1:** no LLM calls, no embeddings, no Workday, no
discovery/crawling, no UI, no multi-user.

## Open design questions — the actual work

1. **Storage truth model** (biggest structural fork): mutable state table + versions
   table, a pure append-only event log with derived views, or a hybrid (state table for
   queries, event log as ground truth). Decides how stage-3 research reads history.
2. **Posting identity & lifecycle:** identity is presumably (source, board, job_id) —
   but when a closed posting reappears, does the same row reopen, or does a new
   lifecycle row link to the old one? Affects repost/duration metrics later.
3. **Content-hash scope:** hash the full raw payload (false churn from embedded
   timestamps) or a chosen set of normalized fields (title, description, location,
   compensation)?
4. **Description handling:** ATS payloads carry HTML; store raw only, or also extract
   plain text at ingest time for stage 2 to consume?
5. **Smaller details:** registry schema/validation, run observability (what `runs`
   records), schema-migration approach as the store evolves.

## Downstream consumers to design for

- **Stage 2 agents** read this store: raw + structured representations (embeddings
  added later), provenance from every derived fact back to its source posting.
- **Stage 3 research** reads the temporal history: posting durations, repost rates,
  salary/requirement drift over time.

These two customers are what make the storage decisions matter.
