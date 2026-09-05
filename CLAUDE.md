# job-hunter

Local-first job-hunting kit that turns an existing coding agent into a personal
job-hunting agent: postings are ingested from official ATS APIs and other
first-party structured JSON endpoints (Greenhouse, Lever, Ashby, Workday,
Oracle Recruiting Cloud, Amazon, SmartRecruiters, Eightfold) into an immutable
archive and a temporal Postgres store tracking each posting's lifecycle from
first seen to closed. Never HTML scraping, never authentication, never
bypassing a bot challenge; no auto-apply.
Built: the ingestion layer, the L2 demand-profile extractor, the agent-first
CLI, and the hosted MCP server that serves the same read surface over HTTPS.
The concept linker (L3), the workspace/tracker, the TUI and the skills are
designed but not built.

## Tech stack

- Python ≥ 3.12, managed with **uv** (`uv.lock`; never introduce another manager)
- httpx (fetching), boto3 (S3/R2 archive), psycopg 3 (Postgres), typer (CLI),
  `mcp` SDK (the hosted server)
- Dev: pytest, ruff (line 100), mypy strict, moto[s3]
- Infra: Docker Compose (postgres:17 + MinIO for local S3), GitHub Actions
  (`test` on push/PR; `fetch` daily on R2 + Neon), Cloud Run for the MCP
  server (Terraform in `infra/`)

## Structure

- `src/jobhunter/` — the package ([CLAUDE.md](src/jobhunter/CLAUDE.md))
  - `sources/` — ATS adapters, no I/O ([CLAUDE.md](src/jobhunter/sources/CLAUDE.md))
  - `archive/` — content-addressed write-once store: local FS / S3-R2
    ([CLAUDE.md](src/jobhunter/archive/CLAUDE.md))
  - `store/` — Postgres schema, lifecycle write path, board panel, queries
    ([CLAUDE.md](src/jobhunter/store/CLAUDE.md))
- `tests/` — pytest suite mirroring the package ([CLAUDE.md](tests/CLAUDE.md))
- `docs/` — design docs; `docs/README.md` is the index of what's current
  ([CLAUDE.md](docs/CLAUDE.md))
- `prototypes/parsing/` — retired rule parser, reference only
  ([CLAUDE.md](prototypes/parsing/CLAUDE.md))
- `infra/` — Terraform for the Cloud Run MCP service; Sean applies it, never
  the assistant ([README.md](infra/README.md))
- `companies.toml` — the board registry (one table per ATS board); editing it
  grows the corpus
- `scripts/live_smoke.py` — opt-in live fetch check; writes nothing
- `.github/workflows/` — `test` CI, scheduled `fetch`
- `.mcp.json` — client config for the hosted server; the token is an env
  reference, never a value (the repo is public)

## Commands

```bash
uv sync                                   # install
uv run pytest                             # tests (store tests need Postgres)
uv run ruff check . && uv run mypy        # lint + strict typecheck
docker compose up -d postgres             # local Postgres (compose also has MinIO)
uv run job-hunter --help                  # CLI entry point
uv run job-hunter-mcp                     # the MCP server on $PORT (needs JOB_HUNTER_MCP_TOKEN)
```

CLI (agent-first contract, `docs/superpowers/specs/2026-09-01-agentic-cli-rework-design.md`):
`pulse` (cursor-driven delta), `q postings|posting|events|claims|document|profile|boards`
(read-only), `sync` (ingest→fetch→extract), `doctor`, `schema`, `skill`, plus
`version`, `fetch`, `ingest`, `rebuild --yes`, `status`, `verify`,
`extract run|review|rebuild`, `archive ls`, `registry check|list`,
`db init|version`. Output is one JSON envelope (`{ok, data, meta}`) when stdout
is piped, a human table on a TTY; `-o json|table` forces either (the old
`--json` flag is gone). Exit codes: 0 ok · 1 verify findings · 2 usage ·
3 config · 4 not found/ambiguous · 5 backend unavailable · 6 systemic. Config
via `JOB_HUNTER_*` in the process env, `./.env`, then
`~/.config/job-hunter/env` (see `src/jobhunter/config.py`); full run
instructions in `README.md`.

MCP (`job-hunter-mcp`, spec `docs/superpowers/specs/2026-09-02-hosted-mcp-design.md`):
the same read surface over streamable HTTP — tools `pulse` plus the seven `q`
verbs, one static bearer (`JOB_HUNTER_MCP_TOKEN`), `/healthz` open. Deploy:
`docs/runbooks/2026-09-02-deploy-mcp.md`.

## Conventions

- The archive is truth; the store is derived and rebuildable from it.
- One canonical text per version: HTML → Markdown via `markdown.py`
  (`md/1`, `NORMALIZER_VERSION`).
- Identity/hashing only through `hashing.py`; time through `timeutil.py`
  (tz-aware UTC everywhere); env only through `config.py`.
- All writes go through `store/lifecycle.py` under a single-writer advisory
  lock; readers use plain SQL in `store/queries.py`.
- Design authority: `docs/2026-08-18-ingestion-layer-spec.md` (normative) and
  `docs/2026-08-17-parsing-direction.md` (canonical parsing direction);
  standing rulings listed in `docs/README.md`.
