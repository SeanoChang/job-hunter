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
- `tests/l2/` — the demand-profile layer: quotes, transforms (`validator/9`),
  schemas v1, verify, the harness (engines, prompt, assemble, attempts,
  state, runner, rebuild, consolidate, agreement, refuter). No Postgres, no
  network; recorded fixtures only.
  - `tests/l2/v2/` — the offline v2 contract (`validator/10`): source
    annotation, types, facts, assemble, verify, quality, project, schemas v2,
    plus `test_cases.py`, which runs the twelve audit case contracts
    (`C01`-`C12`) and seven synthetic minimal pairs (at-least vs more-than,
    and/or connective evidence, same-number different units, not-required vs
    prohibited, CJK source lines, duplicate text occurrences, prompt
    injection). Eleven cases load a `.source.md`/`.emit.json` fixture pair
    from `tests/l2/v2/cases/`; C10 (truncated engine output) and most minimal
    pairs are inline-coded — the and/or pair instead reuses the C09 fixture.
    Fully offline and deterministic — zero model calls.
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
