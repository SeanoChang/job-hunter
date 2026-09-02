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
  (LLM-first, evidence-first demand profile); the L2 extractor is built to it.
- `sources/*.md` — real ATS payload analysis per vendor (Greenhouse, Lever,
  Ashby); basis of the adapters.
- `runbooks/` — operational procedures: the fetcher on R2 + Neon + GitHub
  Actions, the hosted MCP server on Cloud Run. Owner-run commands, Terraform
  config in `infra/`.
- `research/` — dated market/technical memos; context, not design.
- `superpowers/specs/` — approved designs for work after the ingestion spec
  (the agentic CLI, the hosted MCP server); `superpowers/plans/` — the
  implementation plans executed against them, kept as the build record.
- Dated files at the root follow `YYYY-MM-DD-<topic>.md` naming.

Parent: [../CLAUDE.md](../CLAUDE.md)
