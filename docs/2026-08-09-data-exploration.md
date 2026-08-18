# Data exploration — current state of ideas

> [!NOTE] Revisions since 2026-08-09
>
> Content-hash scope (open decision 4) is resolved as four separate identities
> (raw capture / posting version / canonical document / extraction run); the
> store is "recomputable from raw + archives", not byte-for-byte rebuildable,
> once LLM outputs and curation exist; reconciliation must operate on observed
> source ids with a corrected empty-board guard; the derived layer is now an
> evidence-first demand profile over Markdown as canonical text. All in
> `2026-08-17-parsing-direction.md`.

_2026-08-09. Consolidates the design conversation, the live ATS fetches
([sources/](sources/README.md)), and the three research memos
([research/](research/)) into one picture of the data layer. These are working
notes, not a spec — open decisions are listed at the end._

## 1. What the data layer must do

From the product ("bring your own agent" toolkit — CLI + TUI + MCP + skills that
plug into Claude Code/Codex):

- **Local-first.** Corpus, notes, and personal data live on the user's machine.
  Zero external services required; the user's agent subscription supplies the
  LLM.
- **Embeddings** — semantic search and fit-scoring over postings.
- **Keyword tracking** — skills/requirements extracted per posting, trendable
  over time.
- **Graphical linking** — postings ↔ companies ↔ applications ↔ notes ↔ facts,
  traversable by the agent.
- **MCP-friendly shape** — the data must serve well through MCP tools/resources.

From the research memos, three additional hard requirements:

- **Provenance on every extracted field** — ATS choice is a confound (Ashby
  exposes structured salary, Greenhouse buries it in HTML), so analysis must be
  able to restrict by extraction source. Prefer `null`/`unstated` over guessing.
- **Temporal completeness** — the differentiated asset. Majors hard-expire at
  120 days and publish no edit history; we keep everything and diff everything.
- **Versioned company panel** — credible trend claims need a dated membership
  history of which companies were tracked when. Cheap now, impossible to
  backfill.

## 2. Architecture: two tiers, one invariant

```
  files-as-truth                          DB-as-derived-index
  ──────────────                          ───────────────────
  workspace/                              jobhunter.db (SQLite)
  ├── companies/<co>.md        ────►      registry, panel, edges
  ├── applications/<co-role>/  indexer    postings, events, runs
  ├── facts.md (F001…)         ────►      keywords, embeddings, FTS
  └── memory/                             (all rebuildable)

  data/raw/  (ground truth, append-only)
  └── source=greenhouse/board=ramp/<timestamp>.json.gz
```

- **Files are truth** for everything a human or agent authors: company notes,
  application folders, the fact base, memory. Markdown + frontmatter,
  `[[wiki-links]]` between them. Git-friendly, agent-native, portable.
- **The DB is a derived index.** Invariant: _everything in the database can be
  rebuilt from the raw archive + the files._ Schema migrations become "delete
  and re-index," never data surgery.
- **Raw snapshots are the ground truth for postings.** Verbatim API payloads,
  gzipped, Hive-style partitioned, never discarded. The core lesson from memo 2:
  every third-party provider baked irreversible judgment calls into collection
  (60-day dedup windows, snapshot-only records). We defer all classification to
  query time so heuristics can improve retroactively.

## 3. Engine: SQLite-first

One file covers all three access patterns; chosen over alternatives because this
is a _distributed toolkit_ — every user runs it, so zero-dependency wins.

| Need                                     | SQLite answer                                                        |
| ---------------------------------------- | -------------------------------------------------------------------- |
| Relational core (postings, events, runs) | plain tables                                                         |
| Keyword/full-text search                 | **FTS5** (built in)                                                  |
| Embeddings / vector search               | **sqlite-vec** extension (optional — degrade gracefully to FTS-only) |
| Graph linking                            | typed edge table + recursive CTEs                                    |

Considered and deferred: **DuckDB** (better analytics engine, but a second
runtime dep + the pytz gotcha from datacore; revisit for the stage-3 research
arm, reading the same raw archive), **KuzuDB** (real graph queries, but our
graph is small and shallow — recursive CTEs suffice), **Postgres** (server
process contradicts local-first).

## 4. Temporal store (the core tables)

- **Identity**: `uid = source:board:source_id` (e.g. `gh:ramp:5101378008`).
  Platform IDs are never trusted as identity across reposts — a separate
  `duplicate_cluster` table (with `match_method` + confidence) groups
  reposts/cross-posts. True duplicates share as little as 37% raw text, so
  clustering is multi-signal: normalized (company, title, location) blocking key
  → shingle/MinHash sketch → bounded time window.
- **`postings`**: current state per uid — normalized fields (the unified format
  in [sources/README.md](sources/README.md)), `content_hash` (over normalized
  fields only, so formatting churn doesn't fire changes), `first_seen`,
  `last_seen`, `closed_at`.
- **`posting_events`** (append-only, the analytical asset):
  - `opened`, `reopened` (feeds repost detection)
  - `field_changed` — per field, old/new, diff magnitude
  - `refreshed_unchanged` vs `content_hash_changed` — distinguishes
    recency-gaming (Greenhouse ships a "refresh" button) from real edits
  - `closed` with `closure_signal` (delisted vs explicit status vs 404) +
    confidence — "closed" never pretends to mean "filled"
- **Bi-temporal from day one** (industry adoption — Graphiti/XTDB pattern):
  distinguish _when a fact was true_ (`valid_from`/`valid_to` on posting-field
  facts) from _when we observed it_ (`observed_at` on events). Invalidate, never
  delete — a closed posting is a fact whose validity interval ended. Enables
  "what did this req look like when I applied"; impossible to retrofit.
- **`runs`**: every fetch, with per-board outcome. Per-record failure isolation
  and a total-failure reconcile guard (a board returning `[]` or erroring must
  never mass-close postings — Lever returns HTTP 200 `[]` for dead boards).
- **`panel`**: `(company, added_at, removed_at)` — the versioned registry
  membership history required for credible trend metrics.

Only Greenhouse provides `updated_at`; Lever and Ashby provide nothing. Change
detection is therefore _our_ job via snapshot content-hash diff — which is also
why the event stream is data nobody else has.

## 5. Extraction: source-priority chains with provenance

Every enriched field gets a value _and_ a source column, filled by the first
rung that answers:

```
native ATS field  →  deterministic regex  →  small local model  →  LLM (agent)
     "native"            "regex"                "model"              "llm"
```

- **Salary** is increasingly a field-read, not NLP: pay-transparency laws (~18
  states) + Ashby's structured `compensation` block. Regex catches most of the
  Greenhouse prose ranges. `comp_source` records which rung answered.
- **Skills/keywords** anchor to an open taxonomy — **ESCO** (~14k skills, open
  license) is the default; **Lightcast Open Skills** (34k+ skills, free tier) is
  a live alternative to evaluate — with an alias layer on top; Nesta
  `ojd_daps_skills` or SkillNER as the v1 extractor — local, CPU-only, adoptable
  rather than buildable. Tables:
  `keywords(posting_uid, skill_id, surface_form, extractor)`.
- **Embeddings**: one vector per posting over title + normalized description,
  stored in sqlite-vec; model choice open (see §10). Recompute only on
  `content_hash_changed` — LinkedIn's JUDE cut embedding-inference cost ~3× with
  exactly this change-detection discipline.
- The LLM rung is _optional and last_ — the toolkit must be useful before any
  agent has run, and every LLM-derived value is marked as such.

## 6. Graph linking

Nodes are things that already exist (postings in the DB, markdown files in the
workspace); edges are one typed table:

```sql
edges(src_type, src_id, edge_type, dst_type, dst_id, created_at)
-- e.g. ('application','ramp-swe','targets','posting','ashby:ramp:abc123')
--      ('note','companies/ramp','mentions','fact','F014')
```

File-side `[[wiki-links]]` and frontmatter are the authored form; the indexer
materializes them into `edges`. Traversal is recursive CTEs ("everything
connected to this application within 2 hops"). No graph database until the graph
earns one.

## 7. Shape for MCP

Patterns adopted for the MCP server over this store:

- **Stable ID handles** — every entity addressable by a short opaque-ish ID the
  model can repeat reliably: posting `gh:ramp:123`, fact `F014`, application
  `ramp-swe-2026-08`. IDs appear in every response so follow-up calls need no
  search.
- **Progressive disclosure** — list/search tools return thin summaries (id,
  title, company, score, age); `get_posting(uid)` returns depth. Keeps context
  cheap for the agent.
- **Tools for the DB tier, resources for the file tier** — queries and mutations
  are tools (`search_postings`, `score_fit`, `log_application`); workspace
  markdown is exposed as MCP resources the host can attach.
- **Structured output, named filters in** —
  `search_postings(query, location, min_salary, max_age_days, …)`. Response
  discipline from industry practice: cap responses well under the ~25k-token
  client default, prefer CSV/compact tabular over JSON for lists (~29–50% token
  savings), and state truncation explicitly ("showing 100 of 2,340").
- **Consolidated workflow tools, not endpoint wrappers** — the industry pattern
  is a handful of tools (Block's Linear server went 30+ → 2; Harness 130+ → 11);
  we stay under ~10.
- **Three temporal verbs** (XTDB/Dolt pattern): current state is the default and
  looks atemporal; `as_of(timestamp)` for snapshots; `diff(from, to)` returning
  field-level before/after rows — agents never consume the raw event log.
- **One guarded SQL escape hatch** — `query_readonly(sql)` on a
  `PRAGMA query_only` connection with a statement timeout. Evidence: fixed tools
  alone force agents to sample-and-guess on analytical questions (Datadog),
  while agents that iterate SQL against a live DB top the text-to-SQL
  benchmarks. Writes stay behind purpose-built tools.
- **A semantic-layer doc as an MCP resource** — a ~4KB hand-written markdown
  file explaining the schema's meaning (what a posting is, how events append,
  `first_seen`/`last_seen` semantics, normalization gotchas). Benchmarked as
  worth +17–23 accuracy points — more than any model choice. Ship the full
  annotated schema too; it fits in context, so no schema-retrieval machinery.
- **Hybrid search fusion via Reciprocal Rank Fusion** — FTS5 and sqlite-vec
  ranks merged by position (BM25 and cosine scores aren't comparable).

## 8. Derived metrics (computed at query time, never at collection)

- **`ghost_score`** — composite: age-without-edits + repost cycles + salary
  absence where disclosure is normal + evergreen-text reuse. (Ghost prevalence
  ~18–22% on Greenhouse per its own telemetry.)
- **Durations vs the company's own baseline** — not fixed cutoffs (evergreen
  tail: 25% of postings live >90 days).
- **Repost count / interval**, `days_to_first_salary_disclosure`, edit
  count/magnitude.
- **Posting age in triage** — Ashby's 13M-application study: the first week of a
  posting sees ~2× the application rate of any later week. Age belongs in the
  fit score the daily `/triage-postings` skill surfaces.

## 9. Scale (grounded in the live fetches)

|                         | Personal (~100 companies) | Universal (stage 3)       |
| ----------------------- | ------------------------- | ------------------------- |
| Open postings           | ~20–30k                   | few million               |
| Raw snapshot            | ~30–45MB gzipped          | ~40–50GB/day uncompressed |
| With content-hash dedup | **~1GB/year**             | few GB/day compressed     |
| Event stream            | trivial                   | single-digit GB/year      |

History is cheap; there is no storage excuse to skip it.

## 10. Open decisions

1. **Confirm SQLite-first** (vs DuckDB-first) — recommendation stands: SQLite.
2. **Confirm the files-as-truth / DB-as-index split** — recommendation stands.
3. **Embedding model** — local (e.g. a small sentence-transformers model, CPU)
   vs API-based via the user's agent; leaning local-optional. 3b. **Skill
   taxonomy** — ESCO (default) vs Lightcast Open Skills; evaluate both
   extractors on real postings before committing.
4. **Content-hash field scope** — exactly which normalized fields participate
   (description text yes; URLs and ordering no); needs a written list.
5. **Workspace directory layout** — final naming for `companies/`,
   `applications/<co-role>/`, fact base format.
6. **When to write the spec** — this doc + sources + research memos are the
   inputs; the spec is the single remaining unwritten artifact.

## 11. Source documents

- [sources/README.md](sources/README.md) — real ATS payloads compared, unified
  format, scale analysis (live fetches 2026-08-08)
- [research/2026-08-08-understanding-postings.md](research/2026-08-08-understanding-postings.md)
  — skill extraction, taxonomies, source-priority chains
- [research/2026-08-08-posting-lifecycle-tracking.md](research/2026-08-08-posting-lifecycle-tracking.md)
  — dedup, reposts, ghost jobs, event vocabulary (§7 adopted)
- [research/2026-08-08-labor-market-analytics.md](research/2026-08-08-labor-market-analytics.md)
  — credibility lane, versioned panel, ATS-subset bias
- [research/2026-08-09-industry-data-for-llm-mcp.md](research/2026-08-09-industry-data-for-llm-mcp.md)
  — how LinkedIn/Revelio/hiring.cafe structure postings for LLMs, MCP server
  patterns (Anthropic/Block/Harness/Datadog), bi-temporal + event-sourcing
  precedents, text-to-SQL evidence; 45 verified references
- [2026-08-08-stage1-ingestion-context.md](2026-08-08-stage1-ingestion-context.md)
  — stage-1 briefing (predates the product reframe; still valid on ingestion)
