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

## Running the fetcher

```bash
uv sync
export JOB_HUNTER_ARCHIVE_URL=file:///tmp/jh-archive   # or s3://bucket/prefix + AWS_* for R2
uv run job-hunter registry check
uv run job-hunter fetch
uv run job-hunter status
```

Deployment on R2 + GitHub Actions: `docs/runbooks/2026-08-18-deploy-fetcher.md`.
