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
- `queries.py` — read helpers backing the read verbs (`q *`, `pulse`,
  `registry list`, `status`): keyset-paged pages, the watermark delta feed,
  board overview, and validated profiles/mentions per engine tuple.
- `mcp_state.py` — the hosted server's pulse watermarks (schema v4
  `mcp_cursors`): the **only** writer of that table, and the one piece of
  store state a rebuild cannot re-derive from the archive, so it is carried
  across the swap (`carry_cursors`) instead.
- `schema.sql` — DDL, applied by `db init` / `rebuild`. `SCHEMA_VERSION = "4"`;
  purely additive version pairs upgrade in place (`db._ADDITIVE_UPGRADES`),
  anything else demands a `rebuild`.

## Patterns

- All writes go through `lifecycle.py` — `mcp_state.py` is the one exception,
  and it may touch nothing but `mcp_cursors`; readers use plain SQL in
  `queries.py`.
- Timestamps are tz-aware UTC everywhere (`jobhunter.timeutil`).
- A rebuild swaps in a schema with an empty ACL, so `db.capture_grants` /
  `apply_grants` replay the `jobhunter_ro` and `jobhunter_mcp` privileges
  across it; nothing else re-grants them.

## Dependencies

Imports `jobhunter.models`, `jobhunter.archive` (panel reads registry
snapshots). Consumed by `cli.py`, `fetch.py`, `ingest.py`, `rebuild.py`,
`views.py`, `mcp.py`, and `tests/store/`.

Parent: [../CLAUDE.md](../CLAUDE.md)
