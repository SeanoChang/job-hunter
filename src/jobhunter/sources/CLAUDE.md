# sources — ATS API adapters

Adapters for the three supported applicant-tracking systems (Greenhouse, Lever,
Ashby). Each adapter normalises one vendor's board/posting JSON into the shared
`PostingVersion` / `RawRecord` model; adapters do **no I/O** — they take an
already-fetched body and return records.

## Key files

- `base.py` — `Source` protocol plus the normalisers shared by all adapters
  (JSON parsing, HTML description extraction). Raises `EnvelopeError` when a
  body is not the shape the source promises. Also the `TwoPhaseSource` protocol
  for list+detail ATSes (`list_url`/`parse_list`/`detail_url`/`normalize_detail`
  over `RequestSpec`/`ListPage`/`ListRow`, spec
  `docs/superpowers/specs/2026-09-04-multi-ats-expansion-design.md` §3.2): the
  adapter only *describes* requests, `fetch.py` issues them and owns the page
  cap and detail budget.
- `greenhouse.py` / `lever.py` / `ashby.py` — one adapter per ATS.
- `__init__.py` — `get_source(name)` factory used by `fetch.py` and
  `scripts/live_smoke.py`; `get_two_phase(name)` returns the two-phase adapter
  for a source, or None (the `TWO_PHASE_SOURCES` registry is empty until the
  first such adapter lands, so the driver ships dark).

## Patterns

- Adapter contract lives in `base.py`; keep vendor quirks inside each adapter.
- New sources are added to the registry (`companies.toml`) only after an
  adapter exists here; real payload analysis per source is in `docs/sources/`.

## Dependencies

Imports `jobhunter.models`. Consumed by `jobhunter.fetch`,
`scripts/live_smoke.py`, and `tests/sources/`.

Parent: [../CLAUDE.md](../CLAUDE.md)
