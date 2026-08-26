# jobhunter — the ingestion package

The `job-hunter` CLI's implementation: fetch postings from official ATS APIs
(Greenhouse, Lever, Ashby), write every response to an immutable archive, and
ingest manifests into a temporal Postgres store that tracks each posting's
lifecycle. Built to `docs/2026-08-18-ingestion-layer-spec.md`.

## Layout

- `cli.py` — Typer entry point (`job-hunter`). Commands: `version`, `fetch`,
  `ingest`, `rebuild`, `report`, `status`, `verify`; sub-apps
  `extract run|review|rebuild`, `archive ls`,
  `registry check|list`, `db init|version`. Every command accepts `--json`;
  exit 0 normal, 2 systemic (`verify` adds exit 1 = findings failed).
- `models.py` — frozen dataclasses shared by every module. No I/O.
- `registry.py` — `companies.toml` → validated `Board` list + revision hash.
- `fetch.py` — one run: registry → fetch every board (thread pool) → archive
  manifest + blob → ingest into the store.
- `ingest.py` — repair path: replay archived manifests newer than the last
  ingested one.
- `rebuild.py` — replay the whole archive into a fresh schema, swap it live.
- `markdown.py` — L0 HTML→Markdown converter (`md/1`), deterministic,
  versioned as `NORMALIZER_VERSION`; Markdown is the only canonical text.
- `hashing.py` — sole owner of canonical serialisation and hashing
  (`version_hash` identities).
- `http.py` — one HTTP client for all sources: timeouts, bounded retries,
  size cap, honest transport verdicts.
- `config.py` — env settings; the only module that reads `os.environ`.
- `timeutil.py` — UTC helpers; all timestamps are tz-aware UTC.

## Sub-packages

- [`sources/`](sources/CLAUDE.md) — per-ATS adapters, no I/O.
- [`archive/`](archive/CLAUDE.md) — content-addressed write-once store
  (local FS / S3-R2).
- [`store/`](store/CLAUDE.md) — Postgres schema, lifecycle write path, panel,
  read queries.
- `l2/` — the demand-profile layer (increments 1–2 of
  `docs/2026-08-26-l2-extraction-harness.md`): quote/span resolution
  (`quotes.py`), versioned fact transforms (`transforms.py`), JSON schemas v1
  (`schemas_data/`), the pure `verify()` suite (`verify.py`) — all no-I/O,
  no-LLM; plus the harness: prompt `demand-profile/v1` (`prompt.py`), engine
  backends (`engines.py`: openai-compat, claude-cli; observed model only),
  emit→record assembly (`assemble.py`), immutable attempt objects
  (`attempts.py`), pure state derivation (`state.py`), the serial drain loop
  (`runner.py`: ladder, breaker, caps, catch-up scan), archive replay
  (`rebuild.py`). `VALIDATOR_VERSION = "1"` is frozen — any check or
  threshold change bumps it.

## Conventions

- Strict typing (`mypy --strict`), ruff line length 100, import sorting on.
- Identity/hashing only via `hashing.py`; time only via `timeutil.py`;
  environment only via `config.py`.
- Not built yet (design docs): M3 quality loop (k-sampling, refuter,
  consolidate, alerts), M4 access verbs, concept linker (L3).

Parent: [../../CLAUDE.md](../../CLAUDE.md)
