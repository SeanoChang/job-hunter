# Research memo: how industry structures job & temporal data for LLM/MCP usage

*2026-08-09. Four parallel research agents (job platforms / MCP patterns /
temporal data / LLMs-over-SQL), each followed by a citation-checking agent that
fetched every URL before it entered the reference list. Verification caveats are
noted inline where they change a claim.*

## What this changes for job-hunter (synthesis)

1. **Our architecture is the industry pattern, independently converged.** Raw
   ATS snapshots as ground truth + LLM structured extraction into a filterable
   schema is exactly hiring.cafe's play (§1); hybrid keyword + vector retrieval
   is how LinkedIn serves jobs (§1); "raw immutable drops + queryable index" is
   GDELT/GH Archive (§3).
2. **Recompute only on real change.** LinkedIn's JUDE cut LLM inference ~3× by
   embedding only on smart change detection — direct validation of our
   content-hash diff design: re-embed/re-extract only when `content_hash_changed`.
3. **Go bi-temporal now.** Zep/Graphiti and XTDB converge on two time axes:
   when a fact was true (`valid_from/valid_to`) vs when we observed it
   (`observed_at`). Invalidate, never delete — a closed posting is a fact whose
   validity interval ended. Cheap columns now, impossible to retrofit.
4. **Serve digests, never raw event logs.** Every system that feeds history to
   LLMs (Graphiti, event-sourced agent papers) compiles the log into projections
   — fact strings with date ranges, per-entity change digests. Our MCP layer
   should expose three temporal verbs: current state (default), `as_of(ts)`,
   and `diff(from, to)` returning field-level before/after rows.
5. **MCP surface: few workflow tools + a guarded SQL escape hatch + a semantic
   doc.** Consolidated tools beat endpoint-mirroring (Block went 30+ → 2;
   Harness 130+ tools ate 26% of context, redesigned to 11 at 1.6%). But
   tools-only fails analytics — Datadog watched agents sample-and-guess trends
   until they added a query tool (~40% cheaper runs). And the single biggest
   accuracy lever is a small hand-written semantic doc: +17–23 points across
   frontier models, more than any model upgrade. So: typed hot-path tools,
   one `query_readonly(sql)` on a `PRAGMA query_only` connection, and a ~4KB
   markdown semantic layer shipped as an MCP resource.
6. **Response discipline is quantified.** ~25k-token cap (Claude Code default),
   CSV over JSON (~29–50% token savings on tabular data), cell budgets with
   explicit "showing 100 of 2,340" notes, thin list → get detail. Our
   progressive-disclosure plan matches; adopt the numbers.
7. **Hybrid search fusion has a named answer**: Reciprocal Rank Fusion over
   FTS5 + sqlite-vec (BM25 and cosine scores aren't comparable; merge by rank).
   Two CTEs + a join — no vector DB warranted at our scale.
8. **Taxonomy check**: Lightcast's Open Skills (34k+ skills, free tier) is a
   viable open alternative to ESCO — evaluate both before committing (earlier
   deferral applied to Lightcast's commercial data products, not this taxonomy).
9. **Dedup key confirmed**: Revelio normalizes (title, company, location) as a
   blocking key, then pairwise similarity over *temporally overlapping*
   postings — the same shape we adopted from the lifecycle memo.

---

## 1. How large job-data companies structure postings for LLM/AI usage

**Whole-posting LLM embeddings are replacing handcrafted features.**
LinkedIn's JUDE platform (2025) is the clearest published architecture: the
entire posting text goes into a fine-tuned 7B+ LLM (LoRA, two-tower — one
shared base model with different prompt templates per entity type: job
description vs member profile vs resume), trained on human relevance labels +
engagement labels. Serving is streaming: change-data-capture streams → nearline
pipelines → GPU pods → embeddings in a KV store, p95 under 300ms. Two verified
numbers: smart change detection cut LLM inference cost ~3× vs naive DB-change
tracking, and swapping embeddings in for standardized features produced +2.07%
qualified applications — their largest single-model win [1.1]. LinkedIn's KDD
2025 STAR system layers GNNs on top of LLM text embeddings to fix cold-start
and filter-bubble problems [1.2], and their retrieval paper describes serving
as a hybrid: embedding KNN plus term matching in one index [1.3] — the
industrial precedent for FTS5 + sqlite-vec.

**Structured extraction and taxonomies are still load-bearing.** LinkedIn's
Job2Skills (KDD 2020) extracts skills with "salience and market-aware"
weighting rather than mention counts, deployed across 20M postings: +1.92% job
applications, −37% employer rejection of skill suggestions [1.4]. Lightcast
maintains the Open Skills taxonomy — 34,000+ skills, continuously updated,
with library/extractor tooling and free-tier access [1.5]. Revelio Labs
publishes the most complete pipeline documentation: dedup via normalized
(title, company, location) blocking then pairwise similarity over temporally
overlapping postings; ~17k titles hierarchically clustered; 35k skills
clustered into levels; postings parsed into sections and enriched with
predicted salary (trained on visa filings + self-reports), seniority, and
remote suitability [1.6].

**The hiring.cafe pattern — closest to us.** HiringCafe (1.3M+ MAU) scrapes
employer career pages/ATS boards directly, treats the employer's own posting
as ground truth, and — the founder's stated inflection point — used LLM
structured-output schemas to extract all metadata from the description text
itself, powering unusually granular filters (benefits, clearance, funding
stage) [1.7][1.8]. Raw snapshot + LLM extraction into a filterable schema is a
proven, monetizable representation.

**ATS vendors.** Ashby credits its "clean data architecture" as what lets it
ship AI features — structured-first storage is the enabler, not the model
[1.9]. Indeed pairs its matching models with a fine-tuned GPT generating the
"why you matched" explanation: +20% started applications, +13%
interviews/hires [1.10] — a feature our agent can replicate locally from the
fit-score + fact base.

### References (all fetched and verified)

1. [JUDE: LLM-based representation learning for LinkedIn job recommendations](https://www.linkedin.com/blog/engineering/ai/jude-llm-based-representation-learning-for-linkedin-job-recommendations) — LinkedIn Engineering, 2025. Production whole-posting embedding architecture. *(Verifier: page says ~3× inference savings from change detection.)*
2. [A Scalable and Efficient Signal Integration System for Job Matching (STAR)](https://arxiv.org/abs/2507.09797) — arXiv / KDD 2025 (LinkedIn). Text embeddings alone insufficient; GNN signals added for cold-start/bias.
3. [Learning to Retrieve for Job Matching](https://arxiv.org/abs/2402.13435) — arXiv (LinkedIn), 2024. Hybrid KNN + term-matching retrieval.
4. [Salience and Market-aware Skill Extraction for Job Targeting (Job2Skills)](https://arxiv.org/abs/2005.13094) — arXiv / KDD 2020 (LinkedIn). Weighted skill extraction at 20M-posting scale.
5. [Lightcast Open Skills Taxonomy](https://lightcast.io/open-skills) — Lightcast. 34k+ skill open taxonomy + extractor tooling.
6. [Revelio Labs Data Dictionary: Methodologies](https://www.data-dictionary.reveliolabs.com/methodology.html) — Revelio Labs. Dedup, title/skill clustering, enrichment models.
7. [Scaling HiringCafe from 0 to 1M+ users](https://blog.hiring.cafe/p/scaling-hiringcafe-from-0-to-1m-users) — HiringCafe blog (Ali Mir), 2025. Structured-output extraction as the unlock.
8. [Hiring Cafe is ready for this moment](https://therevive.substack.com/p/hiringcafe-is-ready-for-this-moment) — The Revive, 2026. Confirms metadata extracted from the description itself.
9. [AI-Powered Features In Ashby](https://www.ashbyhq.com/product-updates/ai-features-in-ashby) — Ashby, 2023. "Clean data architecture" quote.
10. [How Indeed Uses AI to Provide Better Job-Matching Context](https://www.indeed.com/lead/how-indeed-uses-ai-to-provide-better-matching-context-for-job-seekers) — Indeed, 2024. GPT match explanations; +20% applications.

---

## 2. MCP server design over structured datasets

**Tool granularity: workflow tools, not endpoint wrappers.** Anthropic's
guidance is explicit — consolidate (`schedule_event` over
`list_users`+`list_events`+`create_event`), let one tool run multiple queries
under the hood [2.1]. Block's Linear MCP went through three generations: v1 =
30+ endpoint-mirroring tools; v2 = consolidated with category params; v3 = two
tools (`execute_readonly_query` / `execute_mutation_query`) taking the query
language directly [2.3]. Harness has the hardest numbers: 130+ tools consumed
~26% of a 200k context before the user typed anything; their registry-dispatch
redesign covers 125+ resource types with 11 tools at ~1.6% [2.6]. Cloudflare
shipped thirteen small permission-scoped servers instead of one monolith, with
per-server evals of tool selection [2.10].

**Response-size discipline.** Claude Code caps tool responses at 25,000 tokens
by default; Anthropic recommends pagination, filtering, and a
`response_format: concise|detailed` enum [2.1]. Axiom's writeup is the best
tabular playbook (wide observability events ≈ our postings table): CSV instead
of JSON saved 29% tokens; a global "cell budget" per response with explicit
"showing 100 of 2,340 rows" notes; scoring columns by fill-rate/diversity to
pick top-N on wide schemas. Mantra: start small, expand on demand [2.4].

**Progressive disclosure.** GitHub's official server groups hundreds of tools
into 19 toolsets behind an allowlist, with a `--read-only` mode and both local
stdio and remote HTTP [2.8]. Anthropic's code-execution post generalizes:
present servers as a filesystem of typed APIs, filter data in the sandbox
before it touches context — one workflow dropped 150k → 2k tokens (98.7%)
[2.2]. Notion rewrote its content layer as "Notion-flavored Markdown" instead
of block JSON purely for token density [2.5].

**IDs.** Replace arbitrary UUIDs with semantically meaningful IDs (measurably
fewer hallucinated handles) [2.1] — our `gh:ramp:123` / `F014` scheme is
already the recommended shape.

**Resources vs tools in practice.** The spec makes resources
application-driven (host/user decides inclusion) vs model-controlled tools
[2.11]; in practice, dataset servers ship almost everything as tools because
agent clients invoke tools reliably. Resources fit user-attached context —
for us: the semantic-layer doc and workspace markdown.

**Raw SQL to agents — the live debate.** Pro: one query tool covers the long
tail (Block ended there [2.3]; Axiom exposes its query language [2.4]). Con:
Anthropic's reference Postgres server was archived in 2025 after a
SQL-injection bypass of its read-only protection; Postgres MCP Pro's answer is
layered defense — SQL parsing to reject embedded COMMIT/ROLLBACK, read-only
transactions, execution timeouts, and a restricted DB role as the real
boundary [2.9]. Sentry took a third path: natural-language tool inputs with a
server-side LLM translating to their query syntax [2.7]. For a local
single-user SQLite store the risk calculus is mild — the pragmatic consensus:
curated workflow tools for hot paths + a read-only query escape hatch enforced
at the connection level, writes only through purpose-built tools.

### References (all fetched and verified)

1. [Writing effective tools for AI agents — using AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents) — Anthropic Engineering, 2025. Consolidation, 25k cap, response_format, semantic IDs.
2. [Code execution with MCP: building more efficient agents](https://www.anthropic.com/engineering/code-execution-with-mcp) — Anthropic Engineering, 2025. Progressive disclosure; 150k→2k case study.
3. [Block's Playbook for Designing MCP Servers](https://engineering.block.xyz/blog/blocks-playbook-for-designing-mcp-servers) — Block Engineering, 2025. Linear MCP v1→v3; 400kB hard limits; one-risk-level-per-tool.
4. [Designing MCP servers for wide schemas and large result sets](https://axiom.co/blog/designing-mcp-servers-for-wide-events) — Axiom, 2025. CSV savings, cell budgets, column scoring.
5. [Notion's hosted MCP server: an inside look](https://www.notion.com/blog/notions-hosted-mcp-server-an-inside-look) — Notion Engineering, 2025. Markdown over block JSON for token density.
6. [Architecting MCP for AI Agents: Lessons from Our Redesign](https://www.harness.io/blog/harness-mcp-server-redesign) — Harness, 2026. 130+ tools/26% context → 11 tools/1.6%.
7. [getsentry/sentry-mcp](https://github.com/getsentry/sentry-mcp) — GitHub/Sentry. Embedded-agent NL→query translation pattern.
8. [github/github-mcp-server](https://github.com/github/github-mcp-server) — GitHub. Toolset allowlists, `--read-only`, stdio vs HTTP.
9. [crystaldba/postgres-mcp (Postgres MCP Pro)](https://github.com/crystaldba/postgres-mcp) — GitHub, 2025. Layered read-only SQL enforcement.
10. [Thirteen new MCP servers from Cloudflare](https://blog.cloudflare.com/thirteen-new-mcp-servers-from-cloudflare/) — Cloudflare, 2025. Many small scoped servers; per-server evals.
11. [Resources — MCP Specification (2025-06-18)](https://modelcontextprotocol.io/specification/2025-06-18/server/resources) — MCP spec. Resources-vs-tools contract.

---

## 3. Serving historical/temporal data to LLMs

**Temporal knowledge graphs: validity intervals + invalidation, not deletion.**
Zep's Graphiti engine is the strongest precedent: every fact carries a
bi-temporal pair — valid time (`valid_at`/`invalid_at`: when true in the
world) and transaction time (`created_at`/`expired_at`: when the system
learned it). Contradicting information writes an `invalid_at` timestamp rather
than deleting, so the graph answers both "what is true now" and "what was
believed when" [3.1][3.2]. Crucially, the LLM never sees raw events — it gets
**fact strings with attached date ranges**, current-valid by default,
historical on request. MemGPT/Letta is the complementary pattern: the model
pages external history in and out of context via function calls [3.3].

**Bitemporal databases: a query surface, not a payload.** XTDB is the
canonical schema — `_system_from/to` + `_valid_from/to` per row, queried with
SQL:2011 `FOR VALID_TIME AS OF` / `FOR SYSTEM_TIME AS OF`, and the key design
point: **the database appears atemporal by default**; temporal machinery
activates only when invoked [3.4]. Dolt does the diff side: `dolt_diff(from,
to)` exposes row-level changes (before/after values) as ordinary SQL relations
[3.5]. TDBench validates this for LLMs specifically: models answer
time-sensitive questions best by generating temporal SQL over explicitly
timestamped schemas, beating reasoning over semi-structured timelines [3.6].

**Feature stores: point-in-time correctness.** Feast's
`get_historical_features` does an as-of join — latest value at-or-before each
row's event timestamp — explicitly to stop models "seeing the future" [3.7];
practitioner writeups put leakage inflation at 5–20% of offline metrics
[3.8]. For us: any fit-score or embedding must be pinned to the snapshot it
was computed from.

**Event sourcing for agents: log as truth, projections as context.** Two 2026
papers converge: keep the append-only log as ground truth but never hand it to
the model. "The Log is the Agent" makes the working state a deterministic
projection of the log, giving replay and forking [3.9]; ESAA projects an agent
event log into deterministic markdown read models (`handoff.md`, `state.md`)
that are what agents actually read [3.10] — a direct blueprint for compiling
our posting_events into per-posting "history digests" in the files tier.

**Snapshot/diff corpora at scale.** GDELT publishes append-only 15-minute
drops with a BigQuery index [3.11]; GH Archive records hourly immutable
archives of GitHub events, also BigQuery-queryable [3.12]. Both prove the
"raw immutable drops + queryable index" split we already have.

### References (all fetched and verified)

1. [Zep: A Temporal Knowledge Graph Architecture for Agent Memory](https://arxiv.org/abs/2501.13956) — arXiv, 2025. Graphiti; 94.8% DMR vs MemGPT 93.4%.
2. [Graphiti: Knowledge Graph Memory for an Agentic World](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/) — Neo4j Developer Blog, 2025. Bi-temporal edge model detail. *(Direct fetch bot-blocked; confirmed via search; [Medium mirror](https://medium.com/neo4j/graphiti-knowledge-graph-memory-for-a-post-rag-agentic-world-0fd2366ba27d).)*
3. [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560) — arXiv (Berkeley/Letta), 2023. Paged external memory via function calls.
4. [Time in XTDB](https://docs.xtdb.com/about/time-in-xtdb.html) — XTDB docs, 2024. Bitemporal columns + SQL:2011 surface.
5. [Querying History (AS OF, dolt_diff)](https://www.dolthub.com/docs/sql-reference/version-control/querying-history) — Dolt docs, 2024. Diffs as SQL relations.
6. [TDBench: Temporal Databases for Time-Sensitive QA in LLMs](https://arxiv.org/abs/2508.02045) — arXiv / Microsoft Research, 2025. Temporal SQL beats timeline reasoning.
7. [Feature Retrieval — Point-in-Time Joins](https://docs.feast.dev/getting-started/concepts/feature-retrieval) — Feast docs. The as-of join.
8. [Point in Time Correctness and Time Travel](https://www.systemoverflow.com/learn/ml-feature-stores/feature-store-architecture/point-in-time-correctness-and-time-travel) — System Overflow, 2025. 5–20% leakage inflation figure.
9. [The Log is the Agent: Event-Sourced Reactive Graphs](https://arxiv.org/abs/2605.21997) — arXiv, 2026. Log as truth; deterministic projections; fork-at-any-event.
10. [ESAA: An Event-Sourced Memory Layer for LLM Coding Agents](https://arxiv.org/html/2606.23752) — arXiv, 2026. Event log → markdown read models.
11. [GDELT 2.0: Our Global World in Realtime](https://blog.gdeltproject.org/gdelt-2-0-our-global-world-in-realtime/) — GDELT, 2015. 15-minute append-only drops.
12. [GH Archive](https://www.gharchive.org/) — gharchive.org. Hourly immutable event archives + BigQuery.

---

## 4. LLMs over relational data: raw SQL vs curated tools — the evidence

**Text-to-SQL is good on toy schemas, brittle on real ones.** On BIRD
(realistic SQLite databases) the best systems reach ~82% execution accuracy vs
a 92.96% human baseline — and every top method depends on curated external
knowledge, not schema alone [4.3]. On Spider 2.0 (ICLR 2025 oral; 632
enterprise workflow tasks), baseline frontier models originally solved ~6–10%
(vs 86.6% on the old Spider 1.0); by 2026 specialized *iterating agents with
live DB access* post 74–97%, while plain prompt-only setups still sit near 10%
[4.1][4.2]. Calibration caveat: a 2026 audit found 52.8% of BIRD Mini-Dev and
62.8% of Spider 2.0-Snow gold labels erroneous — treat leaderboard deltas as
noisy [4.4].

**Two findings that bind directly:**

- **Skip schema-linking entirely.** When the full schema fits in context,
  passing it whole and dropping the retrieval/linking step eliminated an error
  class and topped BIRD [4.5]. Our schema trivially fits — ship the full
  annotated schema as an MCP resource; build no retrieval over it.
- **Semantics beat model choice.** Snowflake's Cortex Analyst hits 90%+ on
  real-world evals only via a hand-authored semantic model (single-prompt
  GPT-4o: 51% on the same eval) [4.6]. A 2026 paired benchmark found a ~4KB
  hand-written markdown doc describing measures/conventions lifted accuracy
  +17–23 points across three frontier models, and semantic context explained
  essentially all variance — model choice none [4.7]. The cheapest,
  highest-leverage artifact we can ship is a short prose doc: what a posting
  is, how events append, what `first_seen`/`last_seen` mean, normalization
  gotchas.

**Industry practice: hybrid, not binary.** Arcade's security guidance: typed
parameterized tools where possible, enforcement in the database (read-only
role/connection) because "prompting tells the model what you want, not what
it's allowed to do" [4.8]. But Datadog documents the tools-only failure mode:
agents answering analytical questions through fixed filter-tools resorted to
pulling record samples and guessing trends; adding a SQL capability was the
turning point — ~40% cheaper runs, and CSV + field trimming + token-budget
pagination gave ~5× more records per token [4.9]. Google's production middle
ground, MCP Toolbox for Databases, is human-authored parameterized SQL in
declarative YAML — agents fill typed params only [4.11].

**Hybrid retrieval implementation.** The settled local-first pattern: FTS5 +
sqlite-vec fused with Reciprocal Rank Fusion — rank each list independently,
merge by position, because BM25 and cosine scores aren't comparable. Reference
implementation is two CTEs + a join [4.12]. SQLite FTS5+vec benchmarks at
sub-millisecond into hundreds of thousands of rows — far beyond our corpus;
no external vector DB warranted.

### References (all fetched and verified)

1. [Spider 2.0 (repo)](https://github.com/xlang-ai/Spider2) — xlang-ai / ICLR 2025 oral. Enterprise text-to-SQL benchmark.
2. [Spider 2.0 Leaderboard](https://spider2-sql.github.io/) — XLang Lab, 2026. Agents 74–97%; prompt-only ~10%.
3. [BIRD-SQL Leaderboard](https://bird-bench.github.io/) — BIRD, 2026. Top 81.95% vs human 92.96%.
4. [Pervasive Annotation Errors Break Text-to-SQL Benchmarks](https://arxiv.org/abs/2601.08778) — arXiv, 2026. 52.8%/62.8% gold-label error rates.
5. [The Death of Schema Linking?](https://arxiv.org/abs/2408.07702) — arXiv (Distyl), 2024. Full-schema-in-context wins.
6. [Cortex Analyst: Evaluating Text-to-SQL Accuracy](https://www.snowflake.com/en/blog/engineering/cortex-analyst-text-to-sql-accuracy-bi/) — Snowflake Engineering, 2025. 90%+ with semantic model vs 51% without.
7. [Semantic Layers for Reliable LLM-Powered Data Analytics](https://arxiv.org/abs/2604.25149) — arXiv (Rumiantsau & Fokeev), 2026. +17–23 pts from a 4KB semantic doc.
8. [How to Build SQL Tools for AI Agents](https://www.arcade.dev/blog/sql-tools-ai-agents-security/) — Arcade.dev, 2025. DB-level least privilege.
9. [Designing MCP tools for agents: Lessons from Datadog's MCP server](https://www.datadoghq.com/blog/engineering/mcp-server-agent-tools/) — Datadog Engineering, 2026. SQL escape hatch; ~40% cheaper; 5× records/token.
10. [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents) — Anthropic Engineering, 2025. (Shared with §2.)
11. [MCP Toolbox for Databases](https://github.com/googleapis/mcp-toolbox) — Google, 2026. YAML-declared parameterized SQL tools.
12. [Hybrid full-text and vector search with SQLite](https://simonwillison.net/2024/Oct/4/hybrid-full-text-search-and-vector-search-with-sqlite/) — Simon Willison on Alex Garcia's sqlite-vec, 2024. RRF fusion reference implementation.
