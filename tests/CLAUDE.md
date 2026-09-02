# tests

Pytest suite mirroring the package layout. Run with `uv run pytest` (config in
the root `pyproject.toml`, `-q` by default).

## Layout

- `test_<module>.py` at the root — unit tests for the matching top-level module
  in `src/jobhunter/` (fetch, ingest, rebuild, models, markdown, hashing,
  http, config, registry, timeutil, cli).
- `tests/sources/` — adapter tests against recorded board payloads in
  `fixtures/*_board.json`.
- `tests/archive/` — key layout, local FS backend, manifests, S3 backend
  (moto, from dev dependencies).
- `tests/store/` — lifecycle, panel, queries, db. Needs Postgres;
  `JOB_HUNTER_TEST_DATABASE_URL` points at it (CI runs a postgres:17 service).
  `helpers.py` holds shared store-test setup.
- `test_ci_workflow.py` — the scheduled `fetch` workflow: its `sync` step body is
  extracted from the YAML and run under `bash -eo pipefail` against a stub `uv`
  (which exit codes fail the hourly job is a decision made in shell, not Python).
- `tests/integration/test_three_days.py` — end-to-end over three synthetic days.
- `conftest.py` — shared fixtures, including the autouse one that makes every
  test hermetic: an empty config home and cwd, and no ambient `JOB_HUNTER_*` /
  `AWS_*`, so the developer's `./.env` and `~/.config/job-hunter/env` cannot
  decide whether the suite is green.
- `fixtures/md/` — HTML→Markdown conversion cases for `test_markdown.py`.

## Conventions

- Directory names mirror `src/jobhunter/`; keep new tests beside their subject.
- CI (`.github/workflows/test.yml`) additionally runs ruff, mypy strict, docker
  build + image smoke.

Parent: [../CLAUDE.md](../CLAUDE.md)
