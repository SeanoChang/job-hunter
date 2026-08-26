# archive — immutable content-addressed store

The raw archive is the system's source of truth (spec §5.2): every fetch attempt
writes an attempt manifest plus gzipped response blobs, write-once, keyed by
content hash. Backends: local filesystem or S3/R2 behind one protocol.

## Key files

- `base.py` — `ArchiveStore` protocol (`put`/iterate); `ArchiveError` for
  unreachable/misconfigured backends.
- `keys.py` — the only place that knows key layout (`attempts/<source>/<board>/<ts>.json`,
  `blobs/sha256/<xx>/<sha256>.gz`). Parse/build helpers live here.
- `manifests.py` — read/write `AttemptManifest` records.
- `local.py` — filesystem backend (`file://`).
- `s3.py` — S3/R2 backend via boto3.

## Patterns

- Never construct archive keys by hand; use `keys.py`.
- Write-once semantics: `put` returns whether it created the object.

## Dependencies

Imports `jobhunter.models` and `jobhunter.timeutil`. Consumed by
`jobhunter.fetch`, `jobhunter.ingest`, `jobhunter.rebuild`,
`jobhunter.store.panel`, and `tests/archive/`.

Parent: [../CLAUDE.md](../CLAUDE.md)
