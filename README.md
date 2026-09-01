# job-hunter

An open-source toolkit that turns the coding agent you already use — Claude Code,
Codex, or any MCP-capable agent — into your personal job-hunting agent. Everything
runs locally: the postings corpus, your notes, and your personal data stay on your
machine; your existing agent subscription supplies the intelligence.

## Goal

Replace job-board grinding with an agent working in a structured local workspace:

- **Corpus** — ingest postings directly from official ATS APIs (Greenhouse, Lever,
  Ashby) into a local temporal store that tracks every posting's lifecycle from
  first seen to closed. No LinkedIn/Indeed scraping, no auto-apply.
- **Workspace & tracker** — a structured home for the hunt: companies sorted and
  researched, one folder of organized notes per application, a status board
  (applied → screen → onsite → offer), your personal fact base, and local memory
  the agent maintains across sessions.
- **Toolkit faces** — a CLI for humans and agents to script, a TUI for browsing,
  and an MCP server for chat-style sessions over the same core.
- **Skills** — packaged workflows encoding how to job-hunt well: triage new
  postings against your profile, research a company, tailor a résumé grounded in
  your fact base so nothing is fabricated, log applications, prep interviews.

Later: a public shared corpus so nobody has to run their own ingestion, warm
referrals surfaced from public collaboration data, and labor-market research
(demand, posted wages, trends) published from the accumulated posting history.

## Status

Design in progress — see `docs/`. The ingestion pipeline is the first build; real
ATS payload analysis lives in `docs/sources/`.

## Quickstart

Both an archive URL and a database URL are required: every response is written
to an immutable archive first, then ingested into a Postgres store. A local
Postgres comes from `docker compose up -d postgres`.

```bash
uv tool install .                  # or: uv sync && uv run job-hunter ...
# config: process env > ./.env > ~/.config/job-hunter/env
export JOB_HUNTER_ARCHIVE_URL=file:///tmp/jh-archive   # or s3://bucket/prefix + AWS_* for R2
export JOB_HUNTER_DATABASE_URL=postgresql://jobhunter:jobhunter@localhost:5432/jobhunter
job-hunter doctor                  # every variable, both backends, schema and role
job-hunter sync                    # drain pending manifests, fetch every board, extract
job-hunter pulse                   # what changed since the last pulse, in one call
```

Reading the corpus (read-only; `q` and `pulse` need only a read role):

```bash
job-hunter q postings --status open --search anthropic --fields uid,title,company
job-hunter q posting <uid>              # lifecycle, versions, current document
job-hunter q document <hash-prefix>     # the canonical markdown of one version
job-hunter q profile --doc <prefix>     # what that posting demands (summary; --full for raw)
job-hunter q claims --mention Python    # who asks for it, across the corpus
job-hunter q events --since 24h         # raw lifecycle events, stateless
job-hunter q boards --unhealthy         # boards whose last fetch was not ok
```

Operating and repairing the store:

```bash
job-hunter status                       # per-board fetch health + store health
job-hunter registry check               # validate companies.toml
job-hunter registry list                # board panel: when each board joined/left
job-hunter fetch                        # archive every board, then ingest what is new
job-hunter ingest                       # replay archived manifests not yet ingested
job-hunter rebuild --yes                # replay the whole archive into a fresh schema, swap
job-hunter db init                      # create the jobhunter schema (sync/fetch do this too)
job-hunter db version                   # code vs database schema version
```

Output is one JSON envelope (`{"ok", "data", "meta"}`) when stdout is piped and
a human table on a TTY; `-o json` / `-o table` forces either. Exit codes: `0`
success, `1` verify findings failed, `2` usage, `3` config, `4` not found or
ambiguous, `5` backend unavailable, `6` systemic. `job-hunter schema` prints the
machine catalog of every command, flag and exit code; `job-hunter skill` prints
the agent guide (`job-hunter skill > ~/.claude/skills/job-hunter-cli/SKILL.md`).

`JOB_HUNTER_DROP_RATIO` (default `0.5`) sets the drop guard: a board returning
less than that share of its previous count is `suspect_drop` and its postings
are not closed on that attempt.

Deployment on R2 + Neon + GitHub Actions:
`docs/runbooks/2026-08-18-deploy-fetcher.md`.
