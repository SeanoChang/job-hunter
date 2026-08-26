# store — Postgres temporal store

Durable state on Postgres (Neon in production). Tracks every posting version
and its presence intervals from first seen to closed; the company panel tracks
board membership over time. Spec §5 of `docs/2026-08-18-ingestion-layer-spec.md`.

## Key files

- `db.py` — connection, schema lifecycle (`schema.sql`), advisory lock, atomic
  schema swap for rebuilds. No business logic. `SchemaMismatch` means the DB
  schema_version differs from code → run `rebuild`.
- `lifecycle.py` — **the one write path** (spec §5.4): manifest → store, one
  transaction per attempt under the single-writer advisory lock. Set-based on
  purpose: bounded statements per attempt (prefetch state, classify new /
  changed / opened / closed / reopened in Python), never per-record round trips.
  Implements the drop guard and interval-censored closes.
- `panel.py` — versioned board membership (spec §5.5) derived from archived
  registry snapshots.
- `queries.py` — read helpers backing CLI commands (`report`, `registry list`,
  `status`).
- `schema.sql` — DDL, applied by `db init` / `rebuild`.

## Patterns

- All writes go through `lifecycle.py`; readers use plain SQL in `queries.py`.
- Timestamps are tz-aware UTC everywhere (`jobhunter.timeutil`).

## Dependencies

Imports `jobhunter.models`, `jobhunter.archive` (panel reads registry
snapshots). Consumed by `cli.py`, `fetch.py`, `ingest.py`, `rebuild.py`, and
`tests/store/`.

Parent: [../CLAUDE.md](../CLAUDE.md)
