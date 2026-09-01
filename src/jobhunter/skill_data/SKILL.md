---
name: job-hunter-cli
description: Query Sean's job-posting corpus and compose update digests via the job-hunter CLI
---

# job-hunter CLI

`job-hunter` reads a local-first corpus of job postings collected from official
ATS APIs: an immutable archive plus a temporal store that tracks each posting
from first seen to closed, and a demand profile (what a posting actually asks
for) extracted per document. You are its primary consumer.

Every verb prints exactly one JSON envelope on stdout when stdout is piped;
diagnostics go to stderr. `-o json` / `-o table` forces either mode. (`skill` is
the one exception: this file is its payload, so it prints markdown unless you
ask for `-o json`.)

```json
{"ok": true, "data": …, "meta": {"count": 12, "truncated": false,
                                 "next_cursor": null, "hint": "q posting … "}}
{"ok": false, "error": {"kind": "not_found", "message": "…",
                        "hint": "lengthen the prefix", "valid": null}}
```

`meta.hint` names the next command with real ids filled in — follow it instead
of guessing. Run `job-hunter schema -o json` for the machine catalog: every
command, its flags, the exit-code table, the envelope JSON Schema and the
active versions. Never parse `--help`.

## The hourly loop

1. `job-hunter pulse --cursor hourly -o json` — everything that changed since
   your last run in one call: `data.events[]` (opened / changed / closed /
   reopened, with title, company, board, url), an inline `profile` summary on
   opened and changed events whose document has a validated extraction, and
   `data.attention` (unhealthy boards, extraction backlog, spend today).
2. No events and nothing in `attention` → end the run as a quiet no-op. Silence
   is a valid report.
3. Otherwise compose the update: **New**, **Changed**, **Closed**,
   **Attention**. A close carries `closed_between: [lower, upper]` — the
   posting disappeared somewhere in that interval. Report the interval; never
   invent a point in time.
4. Drill down only for the postings worth expanding:
   - `job-hunter q profile --doc <hash-prefix>` — areas, top mentions,
     compensation / experience / deadline facts.
   - `job-hunter q profile --doc <hash-prefix> --full` — the whole record:
     claims with their quotes and spans.
   - `job-hunter q document <hash-prefix> --slice 0:1500` — the canonical
     markdown the spans index into.
   - `job-hunter q posting <uid>` — lifecycle, version history, its events.
5. `meta.truncated: true` means the page stopped at `--limit`. The cursor
   advanced only as far as the events you were shown, so calling `pulse` again
   continues exactly where it stopped.

`--peek` reports without advancing the cursor. `--since 24h` or
`--since 2026-09-01T00:00:00Z` ignores the cursor entirely (nothing is
recorded). Cursors are client-side state in `JOB_HUNTER_STATE_DIR` (default
`~/.local/state/job-hunter/cursors.json`); give each consumer its own name.

## Reading the corpus

| command | returns |
| --- | --- |
| `q postings [--board s:b] [--status open\|closed] [--since 7d] [--search TEXT]` | posting rows, newest first |
| `q posting <uid>` | one posting: lifecycle, close interval, versions, events, current document |
| `q events [--since] [--kind opened,closed] [--board] [--uid]` | raw lifecycle events, oldest first |
| `q document <hash-prefix> [--slice S:E]` | canonical markdown of one document |
| `q profile --doc <hash-prefix> [--full]` | the demand profile of one document |
| `q claims --mention Python [--importance required] [--board]` | who demands one mention, across the corpus |
| `q boards [--unhealthy]` | per-board health and open counts |

Every id the CLI prints, it accepts back: a 12-hex document prefix from any
listing is a valid `--doc`. `q` and `pulse` only read — they run on a read-only
Postgres role.

## Exit codes

| code | meaning | what to do |
| --- | --- | --- |
| 0 | success | continue |
| 1 | `verify` ran, findings failed | report the findings; do not retry |
| 2 | usage / validation error | read `error.valid`, fix the flag, retry once |
| 3 | config missing or invalid | run `job-hunter doctor`, relay the failing check and its hint, stop |
| 4 | not found or ambiguous id | lengthen the hash prefix, or re-list with `q postings` / `q events`; never guess an id |
| 5 | backend unavailable | the database or archive is down: say so and wait for the next scheduled run, never loop |
| 6 | systemic | an operator must act (ingest gaps, breaker tripped, schema mismatch); relay `error.message`, stop |

Retrying the same failing command more than once is never the answer: 2 and 4
need a different command, 3 needs a human, 5 and 6 need time or an operator.

## Token economy

- `--fields uid,title,company` on any list verb drops every other key; ask for
  what you will print, not for everything.
- Summaries before `--full`: `q profile --doc X` is a digest, `--full` carries
  every claim, quote and span. Fetch the digest first, expand only what you
  will actually quote.
- Keep `--limit` at what you will read (default 50, hard cap 500). Page with
  `--after <meta.next_cursor>` rather than raising the limit.
- Slice documents (`--slice 0:1500`); a posting's markdown can run long, and
  one document per call is the deliberate design — there is no dump verb.

## Operator verbs (not yours)

`sync` (ingest → fetch → extract), `extract run|review|rebuild`, `rebuild`,
`db init` and `fetch` write to the shared store and cost money. Leave them to
the operator; if the corpus looks stale, say so in the update and name the
verb rather than running it.

## Install

```bash
job-hunter skill > ~/.claude/skills/job-hunter-cli/SKILL.md
```
