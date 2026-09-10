# jobhunter — the ingestion package

The `job-hunter` CLI's implementation: fetch postings from official ATS APIs
(Greenhouse, Lever, Ashby), write every response to an immutable archive, and
ingest manifests into a temporal Postgres store that tracks each posting's
lifecycle. Built to `docs/2026-08-18-ingestion-layer-spec.md`.

## Layout

- `cli.py` — Typer entry point (`job-hunter`). Commands: `pulse`, `sync`,
  `doctor`, `schema`, `skill`, `version`, `fetch`, `ingest`, `rebuild --yes`,
  `status`, `verify`; sub-apps `q` (see `cli_q.py`), `extract run|review|rebuild`,
  `archive ls`, `registry check|list`, `db init|version`.
- `cli_output.py` — the output contract every verb speaks: the `{ok, data, meta}`
  envelope on piped stdout, human text on a TTY (`-o json|table` forces either),
  teaching errors (`kind`/`message`/`hint`/`valid`) on stderr, and the typed
  `Exit` table (0 ok · 1 verify findings · 2 usage · 3 config · 4 not
  found/ambiguous · 5 backend · 6 systemic). Nothing else writes JSON to stdout.
- `cli_q.py` — the read-only `q` namespace: `postings`, `posting`, `events`,
  `claims`, `document`, `profile`, `boards`. Bounded (`--limit` 50, hard cap
  500), `meta.truncated` always present, `--after` cursors, `--fields`.
- `views.py` — payload assembly, pure: `(conn, filters…) -> Page`. The CLI and
  the MCP server call the same functions, so the two faces cannot drift; views
  raise `ValueError` and know nothing of typer, envelopes or exit codes.
- `mcp.py` — the hosted read surface (`job-hunter-mcp`, spec
  `docs/superpowers/specs/2026-09-02-hosted-mcp-design.md`): a streamable-HTTP
  MCP app whose eight tools call `views.py`, a static-bearer ASGI gate with
  `/healthz` open, and one connection per call (no pool — Cloud Run reaps
  instances between requests). Writes nothing but `mcp_cursors`.
- `cursors.py` — named client-side watermarks in the state dir
  (`cursors.json`); personal state never enters the shared store.
- `pulse.py` — the delta payload behind `job-hunter pulse`: events since the
  watermark, inline profile summaries, the attention block.
- `skill_data/SKILL.md` — the agent guide shipped with the package, printed
  verbatim by `job-hunter skill` (the one verb whose piped stdout is the file
  itself, not an envelope; `-o json` wraps it as `{"markdown": …}`).
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
  (`quotes.py`), versioned fact transforms (`transforms.py`, `validator/11` —
  floor grammar for "at least/minimum/over N years", single-amount
  compensation with a currency signal, omission scan skips boilerplate;
  10 is v2's), JSON schemas v1 (`schemas_data/1/`), the pure `verify()` suite
  (`verify.py`), findings types (`report.py`) — all no-I/O, no-LLM; plus the
  harness: prompt `demand-profile/v5` (`prompt.py`), engine backends
  (`engines.py`: openai-compat, claude-cli, codex-cli; observed model only),
  emit→record assembly (`assemble.py`), immutable attempt objects
  (`attempts.py`), pure state derivation (`state.py`), the serial drain loop
  (`runner.py`: ladder, breaker, caps, catch-up scan, k-sampling), archive
  replay (`rebuild.py`); the M3 quality loop: cross-sample agreement
  (`agreement.py`), the demote-only refuter (`refuter.py`), and weekly
  consolidation — drift report, human audit queue, refuter summary
  (`consolidate.py`). `VALIDATOR_VERSION` (see `transforms.py`) is frozen per
  version — any check or threshold change bumps it, never edits in place.
  - **bundles** (`bundles.py`) — the six things that define an engine tuple
    travel together as one frozen `Bundle` the runner is handed: prompt
    version + frozen bytes + renderer, emit schema version, validator version,
    emit→record assembly, the record verifier, and the two storage projections
    (`profile_of` for the `extractions.profile` blob, `mention_rows` for
    `profile_mentions`). The loop around them — ladder, breaker, caps,
    catch-up, k-sampling, settle — names none of them. `get_bundle(name)`
    selects by name (`JOB_HUNTER_L2_BUNDLE`, `v1` default, validated in
    `config.py` so a typo fails at startup); `get_bundle_for_tuple(prompt,
    schema)` is replay's inverse, so a mixed archive folds each attempt under
    the bundle that judged it. Registered: **v1** = (`demand-profile/v5`,
    schema `1`, `transforms.VALIDATOR_VERSION`) — today's behaviour, byte for
    byte; **v2** = (`demand-profile/v6`, schema `2`, `validator/10`). The
    tuple keys every attempt and every derived row, so choosing a bundle is
    choosing a corpus partition: a flip re-queues the corpus under the new
    tuple, and rollback is selecting the previous bundle, never deleting rows.
    The write path follows the bundle; the **read path does not yet** —
    `views.profile_row`, `views.claims_view` and `pulse` compute "the engine
    tuple in force" from the v1 module constants (`l2.prompt.PROMPT_VERSION`,
    `l2.runner.SCHEMA_VERSION`, `l2.transforms.VALIDATOR_VERSION`), so v1 rows
    keep serving even once a validated v2 row exists for the same document.
    Making the served tuple follow the selected bundle is open work and gates
    any real cutover. It is not the whole gate, though: the v2 bundle's
    `mention_rows` projection returns nothing for an unaudited record (see
    `l2/v2/` below), so `profile_mentions` under the v2 tuple is empty and
    correcting the scoping alone leaves `q claims` returning nothing until
    `semantic-audit/v1` lands.
  - `l2/v2/` — the v2 semantic contract
    (`docs/superpowers/specs/2026-09-07-parsing-contract-v2-design.md`;
    increment 1 offline modules plus the increment-2 harness wiring —
    selectable from the runner and CLI through the v2 bundle, hosted MCP
    unchanged): source block annotation and
    exact reference binding (`source.py`, `blocks/1`), closed enums and typed
    derivation results (`types.py`), versioned derivation grammars
    (`facts.py`, `validator/10` — quantity comparisons, money/date
    derivation), emit→record assembly with collected binding errors
    (`assemble.py`), the pure verifier over `(record, markdown)` — references,
    facts, accounting, usability (`verify.py`), the six quality dimensions
    (`source`, `evidence`, `semantics`, `completeness`, `sampling`,
    `human_review`) plus the `search_eligible` policy (`quality.py`), pure
    mention/statement row projection (`project.py`). Schemas v2 live at
    `schemas_data/2/{emit,record}.schema.json`, served by the same
    version-parameterized loader as v1. `parsing-rules/2` and `aliases/1` are
    the other identifiers frozen with this increment. Wiring adds two equally
    pure modules: `prompt.py` (`demand-profile/v6` — the spec §4 extractor text
    over a numbered `blocks/1` listing, so the model cites block ids and exact
    substrings and never computes offsets) and `serve.py` (what a record looks
    like once it leaves the archive: the stored slice with its `"schema": "2"`
    marker, the claim index the agreement gate compares, `profile_mentions`
    rows whose importance comes from each mention's linked STATEMENT — the C04
    fix — and a `summary()` whose keys match `pulse.profile_summary` so every
    current renderer works unchanged). `pulse.py` and `extract show` dispatch
    on `profile["schema"] == "2"`; storage is NOT migrated. No model calls and
    no database/archive I/O inside `l2/v2/` itself.
    **The mention projection is gated on `search_eligible` and therefore
    yields nothing today.** `assemble.py` calls `quality.assess(source=…,
    evidence="pass")` and leaves `semantics`/`completeness` at `not_checked`,
    which no offline phase can clear, so `project.mention_rows` (and
    `serve.mention_rows` through it) short-circuits to `[]` for every record a
    v2 run produces — pinned by
    `tests/l2/test_runner_v2.py::test_an_unaudited_record_stores_its_profile_but_indexes_no_mentions`.
    The blob is populated, the aggregate is not; `semantic-audit/v1` is what
    unblocks it.

## Conventions

- Strict typing (`mypy --strict`), ruff line length 100, import sorting on.
- Identity/hashing only via `hashing.py`; time only via `timeutil.py`;
  environment only via `config.py`.
- Not built yet (design docs): M3 alerting (attention digests via generic
  webhook), concept linker (L3), workspace/tracker, TUI.

Parent: [../../CLAUDE.md](../../CLAUDE.md)
