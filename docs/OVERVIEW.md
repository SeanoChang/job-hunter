# job-hunter — project overview

Last updated: 2026-08-25. Authored directories only (generated/vendor content
excluded). Entry points: root [CLAUDE.md](../CLAUDE.md), docs index
[README.md](README.md).

```
job-hunter/
├── CLAUDE.md                    # orientation: stack, structure, commands, conventions
├── README.md                    # what it is, how to run the fetcher locally
├── pyproject.toml               # uv manifest: deps, CLI entry point, ruff/mypy/pytest config
├── uv.lock                      # lockfile — uv is the only package manager
├── companies.toml               # registry: 79 ATS boards (company/source/board/country/tags)
├── compose.yaml                 # local infra: postgres:17, MinIO (S3), fetcher container
├── Dockerfile                   # fetcher image (CI builds and smoke-runs it)
├── scripts/
│   └── live_smoke.py            # opt-in live fetch of every registered board; writes nothing
├── src/jobhunter/               # the package (CLI: job-hunter)
│   ├── CLAUDE.md                # module map + conventions
│   ├── cli.py                   # Typer app: version/fetch/ingest/rebuild/report/status + sub-apps
│   ├── models.py                # frozen dataclasses shared by all modules (no I/O)
│   ├── registry.py              # companies.toml -> validated Board list + revision hash
│   ├── fetch.py                 # one run: fetch boards -> archive -> ingest
│   ├── ingest.py                # replay pending archived manifests (repair path)
│   ├── rebuild.py               # replay whole archive into fresh schema, swap live
│   ├── markdown.py              # L0 HTML->Markdown converter (md/1, NORMALIZER_VERSION)
│   ├── hashing.py               # canonical serialisation + hashing (version_hash)
│   ├── http.py                  # single HTTP client: timeouts, retries, size cap
│   ├── config.py                # env settings; only reader of os.environ
│   ├── timeutil.py              # tz-aware UTC helpers
│   ├── archive/                 # immutable content-addressed store
│   │   ├── base.py              # ArchiveStore protocol
│   │   ├── keys.py              # key layout: attempts/, blobs/sha256/
│   │   ├── manifests.py         # attempt manifest read/write
│   │   ├── local.py             # file:// backend
│   │   └── s3.py                # S3/R2 backend (boto3)
│   ├── sources/                 # ATS adapters (no I/O)
│   │   ├── base.py              # Source protocol + shared normalisers
│   │   ├── greenhouse.py        # Greenhouse adapter
│   │   ├── lever.py             # Lever adapter
│   │   └── ashby.py             # Ashby adapter
│   └── store/                   # Postgres temporal store
│       ├── db.py                # connection, schema lifecycle, advisory lock, swap
│       ├── schema.sql           # DDL
│       ├── lifecycle.py         # THE write path: manifest->store, one txn per attempt
│       ├── panel.py             # versioned board membership from registry snapshots
│       └── queries.py           # read helpers for the CLI
├── tests/                       # pytest suite mirroring src layout
│   ├── conftest.py              # shared fixtures
│   ├── test_<module>.py         # unit tests per top-level module
│   ├── fixtures/                # recorded boards + markdown cases
│   ├── sources/ archive/ store/ # mirror their src counterparts
│   └── integration/             # end-to-end over three synthetic days
├── docs/                        # design writing; README.md is the status index
│   ├── README.md                # doc index + standing rulings
│   ├── 2026-08-18-ingestion-layer-spec.md   # normative ingestion spec
│   ├── 2026-08-17-parsing-direction.md      # canonical parsing direction
│   ├── sources/                 # real ATS payload analysis per vendor
│   ├── research/                # dated market/technical memos
│   ├── runbooks/                # deploy fetcher (R2 + Neon + GitHub Actions)
│   ├── superpowers/plans/       # archived implementation plans (increments 1–2)
│   └── changelog/               # scaffold-docs snapshots
└── prototypes/parsing/          # RETIRED rule-based parser; reference only
```

Not built yet (per docs): demand-profile extractor (L2), concept linker,
workspace/tracker faces beyond the CLI, skills.
