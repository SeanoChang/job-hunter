# docs — design documents, research, runbooks

All design and research writing for job-hunter. **`README.md` is the index**:
one line per document marking it current, partially superseded (status note
inside), or historical. When documents disagree, the one marked current wins.

## Key files

- `README.md` — index + status; also carries the standing rulings list
  (chronological decisions still in force).
- `2026-08-18-ingestion-layer-spec.md` — **normative spec** for everything in
  `src/jobhunter/`.
- `2026-08-17-parsing-direction.md` — canonical parsing/extraction direction
  (LLM-first, evidence-first demand profile). L2 extractor not yet built.
- `sources/*.md` — real ATS payload analysis per vendor (Greenhouse, Lever,
  Ashby); basis of the adapters.
- `runbooks/` — operational procedures (deploy fetcher on R2 + Neon +
  GitHub Actions).
- `research/` — dated market/technical memos; context, not design.
- `superpowers/plans/` — archived implementation plans for increments 1–2.
- Dated files at the root follow `YYYY-MM-DD-<topic>.md` naming.

Parent: [../CLAUDE.md](../CLAUDE.md)
