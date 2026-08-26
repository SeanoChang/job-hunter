# job-hunter

Local-first job-hunting kit that turns an existing coding agent into a personal
job-hunting agent: postings are ingested from official ATS APIs (Greenhouse,
Lever, Ashby) into an immutable archive and a temporal Postgres store tracking
each posting's lifecycle from first seen to closed. No scraping, no auto-apply.
Currently the **ingestion layer only**; the demand-profile extractor (L2),
concept linker, workspace/tracker faces (CLI/TUI/MCP beyond the CLI), and
skills are designed but not built.

## Tech stack

- Python ≥ 3.12, managed with **uv** (`uv.lock`; never introduce another manager)
- httpx (fetching), boto3 (S3/R2 archive), psycopg 3 (Postgres), typer (CLI)
- Dev: pytest, ruff (line 100), mypy strict, moto[s3]
- Infra: Docker Compose (postgres:17 + MinIO for local S3), GitHub Actions
  (`test` on push/PR; `fetch` daily on R2 + Neon)

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
- `companies.toml` — the board registry (one table per ATS board); editing it
  grows the corpus
- `scripts/live_smoke.py` — opt-in live fetch check; writes nothing
- `.github/workflows/` — `test` CI, scheduled `fetch`

## Commands

```bash
uv sync                                   # install
uv run pytest                             # tests (store tests need Postgres)
uv run ruff check . && uv run mypy        # lint + strict typecheck
docker compose up -d postgres             # local Postgres (compose also has MinIO)
uv run job-hunter --help                  # CLI entry point
```

CLI: `version`, `fetch`, `ingest`, `rebuild`, `report`, `status`,
`archive ls`, `registry check|list`, `db init|version` — all accept `--json`;
exit 0 normal, 2 systemic. Env config via `JOB_HUNTER_*` variables (see
`src/jobhunter/config.py`); full run instructions in `README.md`.

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
