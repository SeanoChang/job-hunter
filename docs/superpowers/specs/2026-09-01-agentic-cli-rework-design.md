# Agentic CLI rework — design

Date: 2026-09-01. Status: approved design, pre-implementation.
Supersedes the *shape* of the `q` verb table in
`docs/2026-08-26-l2-extraction-harness.md` §11 while keeping its rulings
(read-only role, paged with hard cap 500, no dump verb, owner-internal).
Motivating failure: a hands-on session found the CLI unusable as a consumer —
operator verbs only, no way to retrieve a posting, env-only config with
one-line errors (`store/queries.py` proves the read model stops at counts and
events).

## 1. Goal and primary consumer

The CLI's primary consumer is **an AI agent**, specifically an hourly Claude
schedule that must answer "what changed in Sean's job market since my last
run?" in one cheap call, compose a human update, and drill down only when
something interesting appeared. Humans remain secondary consumers via the same
verbs (TTY rendering).

Design authority for agent-facing conventions, from the 2026-09-01 research
pass (clig.dev, clispec.dev, gh CLI engineering posts, Anthropic
"writing effective tools for agents", Hugging Face `hf` agent CLI):

1. Structured output by default when piped; human tables only on a TTY.
2. Data on stdout, diagnostics on stderr, never interleaved.
3. No interactive prompt anywhere; destructive verbs take `--yes` and fail
   fast naming that flag.
4. Typed exit codes, one class per code, declared in a machine catalog.
5. Errors teach: kind + message + hint + enumerated valid values.
6. Every list is bounded, truncation is always marked, continuation is a
   cursor — silent clipping is the worst failure mode.
7. Field selection (`--fields`) to control token cost.
8. One noun–verb grammar so an agent extrapolates unseen commands.
9. A machine introspection surface (`schema`) so no agent parses `--help`.
10. Next-step hints with real ids pre-filled in output.
11. Ship agent guidance with the tool (skill file, printable).

## 2. Contract spine (applies to every verb)

**Envelope.** Piped stdout emits exactly one JSON object:

```json
{"ok": true,
 "data": …,
 "meta": {"count": 12, "truncated": false, "next_cursor": null,
          "hint": "q profile --doc 3f2a91ab04c1 for the full profile"}}
```

Errors (any verb):

```json
{"ok": false,
 "error": {"kind": "not_found",
           "message": "no document matches '3f2a'",
           "hint": "prefixes must be at least 6 hex chars; list ids with: q postings",
           "valid": null}}
```

`--output json|table` (`-o`) overrides TTY detection. The old `--json` flag is
removed. On a TTY, `data` renders as a table/text; `meta.hint` still prints.

**Exit codes** (declared in `schema`, tested as a table):

| code | class |
| --- | --- |
| 0 | success |
| 1 | `verify` findings failed (unchanged) |
| 2 | usage / validation error |
| 3 | config missing or invalid |
| 4 | not found / ambiguous identifier |
| 5 | backend unavailable (DB, archive, network) |
| 6 | systemic (today's exit 2 moves here) |

**Bounds.** `--limit` default 50, hard cap 500 (standing ruling).
`meta.truncated` is always present on list verbs; continuation is
`--after <cursor>` where the cursor is opaque. No verb dumps the corpus.

**Identifiers.** Every id the CLI prints, it accepts back — short hash
prefixes resolve exactly as `verify` already does (`cli.py` prefix
resolution); ambiguity is exit 4 with a hint to lengthen the prefix.

**Prompts.** None, ever. `rebuild` requires `--yes` off-TTY.

## 3. Porcelain: `pulse`

`job-hunter pulse [--cursor NAME | --since TS] [--boards s:b,…] [--peek] [--limit N]`

One envelope containing everything the hourly routine needs:

- `window` — `{from, to}` actually covered.
- `events[]` — lifecycle events in the window
  (`posting_events` joined to current version): `kind`
  (`opened|changed|closed|reopened`), `uid`, `board`, `title`, `company`,
  `url`, `at`; closes carry `closed_lower_at`/`closed_upper_at` (the honest
  interval, never a fake point).
- Inline `profile` on opened/changed events whose current document has a
  **validated** extraction under the active engine glob: required/preferred
  area names, top mentions, compensation/experience/deadline facts. Summary
  only — no spans, no quotes; `q profile` has the rest.
- `attention` — unhealthy boards (`board_health`), extraction backlog and
  review-queue depth, `needs_review`/`quarantined` counts, spend today
  (all existing queries in `queries.py`).
- `meta.hint` — drill-down commands with real ids.

**Cursor semantics.** Named cursors are client state in
`$XDG_STATE_HOME/job-hunter/cursors.json` (default
`~/.local/state/job-hunter/`, override `JOB_HUNTER_STATE_DIR`). The stored
value is the watermark `{at, event_ids_at}` — the timestamp of the newest
reported event plus the ids seen at that exact instant for tie-breaking.
The timestamp is authoritative because `rebuild` regenerates `event_id`s but
reproduces `at`; after a rebuild the worst case is re-reporting one instant,
never skipping. If `--limit` truncates, the cursor advances only to the last
*emitted* event and `meta.truncated` tells the agent to call again. Advance happens only after the envelope is
fully flushed to stdout; a crash mid-run re-reports rather than skips.
`--peek` never advances. No cursor yet → last 24 h and `meta.first_run: true`.
Personal state stays on the client (2026-08-18 ruling); the shared store is
never written by `pulse`.

## 4. Plumbing: the `q` namespace

| verb | returns |
| --- | --- |
| `q postings [--board] [--status open\|closed] [--since] [--search TEXT] [--fields] [--limit] [--after]` | posting rows: uid, board, title, company, url, status, first/last seen, version_count. `--search` is ILIKE over title+company (FTS is out of scope, §9). |
| `q posting <uid>` | one posting: lifecycle fields, close interval, version history, current `document_hash`, its events. |
| `q events [--since TS] [--kind] [--board] [--uid] [--limit] [--after]` | raw lifecycle events (what `pulse` consumes; stateless flavor). |
| `q document <hash-prefix> [--slice S:E]` | canonical markdown for one document (slice in codepoints, matching quote spans). One document per call (ruling). |
| `q profile --doc <prefix> [--full]` | the demand profile rendered agent-first: areas → claims (importance/level, quote text), facts. `--full` emits the raw record JSON verbatim. |
| `q claims --mention X [--importance] [--board] [--since] [--limit]` | claim rows across the corpus via `profile_mentions` (§6). |
| `q boards [--unhealthy]` | health, open counts, panel membership. |

Read-only: `q` and `pulse` require only a read Postgres role plus archive
read; `doctor` warns when a writer DSN is used for them.

## 5. Operator surface

- **`sync`** (new) — drain-ingest pending manifests → fetch → extract within
  budget → summary envelope. Exactly the choreography of today's CI steps,
  same advisory locks, flags `--no-extract`, `--extract-max-docs`. The fetch
  workflow becomes one `sync` step in the same PR.
- **Removed:** `report` (superseded by `pulse` / `q events`), the `--json`
  flag everywhere.
- **Kept, re-skinned onto the contract spine:** `fetch`, `ingest`,
  `rebuild --yes`, `extract run|review|rebuild`, `verify`, `status`,
  `db init|version`, `archive ls`, `registry check|list`.
- `extract review`'s dossier flow keeps its file-based pipeline but its
  listing/output adopt the envelope.

## 6. Store change (exactly one)

Schema v3 adds one **derived, rebuildable** table:

```sql
profile_mentions (
  document_hash, model, prompt_version, schema_version, validator_version,
  mention text, area_kind text, importance text,
  PRIMARY KEY (document_hash, model, prompt_version, schema_version,
               validator_version, mention, area_kind, importance)
)
```

Populated from `extractions.profile` on every derived-state write
(`store/extraction.py`) and by `extract rebuild`. Indexed on
`(mention, importance)`. Homogeneous in engine tuple like every aggregate
(2026-08-26 ruling). Nothing else in the schema changes.

## 7. Onboarding and introspection

- **`doctor`** — per-variable config check (all six: archive URL, R2
  endpoint/key/secret/region, DSN), live connectivity probes to archive and
  DB, role detection (read-only vs writer), each failure with the exact fix.
  Exit 0 healthy / 3 config / 5 backend.
- **Config loading** — precedence: process env > `./.env` >
  `~/.config/job-hunter/env`. `config.py` stays the only env reader.
- **`schema`** — machine catalog: every command, flags, enums, exit-code
  table, envelope JSON Schema, active versions (`NORMALIZER_VERSION`,
  validator, prompt). Generated from the typer app, not hand-written, so it
  cannot drift.
- **`skill`** — prints the shipped agent guide (also committed as
  `skills/job-hunter-cli/SKILL.md`): the pulse→drill-down loop, error
  recovery per exit code, token-economy tips (`--fields`, summaries before
  `--full`).

## 8. The hourly Claude-schedule blueprint

Routine prompt (runs locally first — the machine with creds and cursor
state; cloud later once creds are provisioned there):

1. `job-hunter pulse --cursor hourly -o json`
2. Empty `events` + clean `attention` → end as a quiet no-op.
3. Otherwise compose Sean's update: **New** (title/company/comp/required
   areas, matched against the interest sketch embedded in the routine
   prompt — personal data never enters the shared store), **Changed**,
   **Closed** (with close intervals), **Attention** (unhealthy boards,
   review backlog, spend). Drill down with `q profile` / `q document` only
   for postings worth expanding.
4. Deliver as a push notification/message. Cursor already advanced
   atomically with output; the data survives in the run transcript even if
   delivery fails.

## 9. Out of scope

FTS/semantic search (L3 territory), embeddings, personal application
tracking, the MCP wrapper (unchanged ruling: after the verbs are exercised),
public serving, cross-tuple aggregates.

## 10. Testing

- Golden envelope snapshots per verb (JSON contract is a tested artifact).
- TTY-vs-pipe rendering tests; stderr purity test (stdout parses as one JSON
  object under every verb and error path).
- Cursor semantics: advance-after-flush, `--peek`, crash replay, event-id
  watermark across a `rebuild`.
- Exit-code table test driving each error class.
- `schema` output validated against the live typer app.
- Integration: extend `tests/integration/test_three_days.py` to assert
  `pulse` deltas across the three days, including an interval-censored close.
- CI: fetch workflow switched to `sync`; the run must stay green in the same
  PR.

## 11. Breaking changes (deliberate, single PR)

`report` deleted; `--json` deleted in favor of `-o json`; exit code 2 → 6
for systemic; every `--json` consumer (the fetch workflow, README examples,
runbook) updated in the same change. CI and Sean are the only consumers
today; the coordinated break was approved 2026-09-01.
