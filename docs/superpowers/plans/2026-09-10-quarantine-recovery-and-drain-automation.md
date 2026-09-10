# Quarantine Recovery and Drain Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover the recoverable share of the 1,107 quarantined documents and turn the manual codex drain into an unattended loop, so the 47k-document backlog drains continuously.

**Architecture:** Three code changes (a validator/12 international money grammar, a proper extraction toggle for the hourly fetch, a drain-cycle driver that automates dump → local codex drain → encrypted upload → ingest), then two operational campaigns that use them (the $0 replay, the quarantine retry batches). Everything rides the existing pipeline: `extract-queue-dump.yml` / `scripts/local_codex_drain.py` / `outbox-ingest.yml` and the runner's own catch-up.

**Tech Stack:** Python ≥3.12 under uv, `gh` CLI for all GitHub interaction, codex-cli as the only engine, GitHub Actions for every store/archive touch (no credentials on the machine).

**Basis (findings of the 2026-09-09/10 quarantine audit, this plan's spec):**
- 1,115 quarantined rows at audit time, all model `gpt-5.6-luna`: dominant classes were single-amount compensation (344 → 306 after validator/11), emit-schema violations from the pre-strict era (295, all validator/2 rows), anchors-not-found (163), ungrounded mentions/fragments (~150).
- validator/11 + replay recovered 58 docs. 263 of 329 comp-failing docs now parse **every** comp anchor, yet stay quarantined — their archived responses also carry model noise, so only **fresh generations** can save them; the queue never re-offers a doc with a row at the current tuple (hence `pool=quarantined` in the dump, commit `ed85f63`).
- 91 comp anchors remain unparseable: international formats (`CA$125,300`, `kr539,400 – kr809,200 DKK`, `zł188.400 PLN`, `Kč2,206,000 CZK`, `RM2,000`, `210 300.00 USD`, `65,000−87,500` with U+2212, `$40/hour to $65/hour`, `$130.600,00`), plus prose the grammar is right to refuse ("competitive salaries").
- The hourly `fetch` is disabled entirely because its only extraction off-switch (`JOB_HUNTER_L2_MAX_DOCS=0`) fails config validation before collection runs (lost hour, run 34412983679). Collection is paused — that is corpus damage accruing daily.
- 114 junk `._*` AppleDouble keys sit under `extractions/attempts/` in R2 from the first ingest (inert; the catch-up key regex skips them).

## Global Constraints

- Frozen identifiers stay frozen: validator `9`, `10` (v2), `11` are taken; this plan mints **`12`** for v1 and nothing else. Any grammar change lands as 12, never as an edit to 11.
- uv only; ruff (line 100) and `mypy --strict` green on every commit; no model calls and no network in any test.
- Single attempt writer at a time: while local drains run, no CI job may write attempts (fetch extraction stays off via the Task 2 toggle; `extract-backfill.yml` stays disabled except for `mode=rebuild` dispatches).
- Codex calls happen only on the owner's machine via `scripts/local_codex_drain.py`; CI never holds codex auth.
- Destructive actions (the R2 junk-key deletion) execute only after the owner ticks the approval criterion — never autonomously.
- All commits land on main directly (repo convention for ops/CI work), author SeanoChang.

---

### Task 1: validator/12 — international compensation grammar

**Files:**
- Modify: `src/jobhunter/l2/transforms.py`
- Test: `tests/l2/test_transforms.py`

**Interfaces:**
- Consumes: existing `_amount`, `_CURRENCY`, `parse_compensation` / `_single_amount` structure (validator/11 shape).
- Produces: `VALIDATOR_VERSION == "12"`; `parse_compensation` accepting the international forms below; `TRANSFORMS` re-keyed automatically.

**Design (exact semantics):**
- Multi-character currency signs, each mapping to exactly one currency: `CA$→CAD, A$→AUD, NZ$→NZD, S$→SGD, HK$→HKD, R$→BRL, zł→PLN, Kč→CZK, RM→MYR`. `kr` is matched as a sign but maps to no currency (DKK/SEK/NOK ambiguity) — currency comes only from an explicit code. Letter-based signs take a `(?<![A-Za-z])` guard so "okr"/"firm" never donate one.
- `_CODE` grows `DKK|NOK|PLN|CZK|MYR|BRL|MXN|ILS` (keep the existing list; append).
- `_AMOUNT` accepts space-thousands (`210 300.00`) and a decimal-comma tail (`130.600,00` — tail discarded like cents): `(\d{1,3}(?:[, ]\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(k)?`.
- Range separators add the Unicode minus family: `(?:--?|–|—|−|to)`.
- An optional period fragment may sit inside each bound and before a trailing code: `(?:\s*/\s*(?:hr|hour|yr|year|mo|month))?` — fixes `$40/hour to $65/hour` and `110,000 - 200,000/year SGD`.
- Group arity of every money regex is preserved (parse_compensation unpacks 6 groups from `_MONEY`); the sign alternation stays one group.
- Anything still outside the grammar stays `None` — "competitive salaries", "$M+", percentage splits are correct refusals; no prose heuristics.

- [ ] **Step 1: Write the failing tests** — extend `tests/l2/test_transforms.py` with a `test_compensation_international` parametrize built from the real quarantined anchors:

```python
@pytest.mark.parametrize(
    "text,expected",
    [
        ("CA$110,200 - CA$160,200 CAD gross",
         {"min": 110200, "max": 160200, "currency": "CAD", "period": None}),
        ("kr539,400 – kr809,200 DKK gross",
         {"min": 539400, "max": 809200, "currency": "DKK", "period": None}),
        ("kr739,000 SEK - kr1,109,000 SEK",
         {"min": 739000, "max": 1109000, "currency": "SEK", "period": None}),
        ("zł188.400 PLN - zł282.600 PLN",
         {"min": 188400, "max": 282600, "currency": "PLN", "period": None}),
        ("Kč2,206,000 CZK - Kč3,308,000 CZK",
         {"min": 2206000, "max": 3308000, "currency": "CZK", "period": None}),
        ("639,400 - 799,300 DKK", {"min": 639400, "max": 799300, "currency": "DKK", "period": None}),
        ("DKK 53283 - DKK 66608", {"min": 53283, "max": 66608, "currency": "DKK", "period": None}),
        ("210 300.00 USD - 273 400.00 USD",
         {"min": 210300, "max": 273400, "currency": "USD", "period": None}),
        ("Approximately 65,000−87,500 OTE annually",
         {"min": 65000, "max": 87500, "currency": None, "period": "year"}),
        ("$40/hour to $65/hour", {"min": 40, "max": 65, "currency": None, "period": "hour"}),
        ("110,000 - 200,000/year SGD",
         {"min": 110000, "max": 200000, "currency": "SGD", "period": "year"}),
        ("$130.600,00 to $ 209.300,00 USD per year",
         {"min": 130600, "max": 209300, "currency": "USD", "period": "year"}),
        ("RM2,000", {"min": 2000, "max": 2000, "currency": "MYR", "period": None}),
        # right refusals stay refusals
        ("competitive salaries", None),
        ("85% paid through base salary and 15% variable compensation", None),
        ("$M+", None),
        ("5-10% of the time", None),
        # regression: nothing about the US forms moves
        ("$163,800 - $245,800 USD per year",
         {"min": 163800, "max": 245800, "currency": "USD", "period": "year"}),
        ("$332,200.00", {"min": 332200, "max": 332200, "currency": None, "period": None}),
    ],
)
def test_compensation_international(text: str, expected: dict[str, object] | None) -> None:
    assert parse_compensation(text) == expected
```

- [ ] **Step 2: Run to verify the new cases fail** — `uv run pytest tests/l2/test_transforms.py::test_compensation_international -q` → the international rows FAIL, the refusal and regression rows already pass.
- [ ] **Step 3: Implement** — apply the design above in `transforms.py`: new `_SIGN` alternation + `_PREFIX_CURRENCY` map, extended `_CODE`, widened `_AMOUNT`, separator and period-fragment changes threaded through `_MONEY`, `_MONEY_CODE`, `_MONEY_CODE_LEAD`, `_MONEY_TOKEN`, `_MONEY_ONE*`; bump `VALIDATOR_VERSION` to `"12"` with a dated comment citing this plan; re-pin the two guard tests (`test_registry_shape`, `test_validator_version_bumped_for_the_grammar_change`) to `"12"`.
- [ ] **Step 4: Full check** — `uv run pytest tests/l2/ -q && uv run ruff check . && uv run mypy` → all green; **every pre-existing compensation test unchanged and passing** (the frozen-meaning proof).
- [ ] **Step 5: Update `src/jobhunter/CLAUDE.md`** — the `transforms.py` line reads `validator/12` and one clause about international money forms.
- [ ] **Step 6: Commit** — `feat(l2): validator/12 — international compensation forms`.

### Task 2: the hourly fetch gets a real extraction toggle

**Files:**
- Modify: `.github/workflows/fetch.yml` (the `sync` step only)
- Test: `tests/test_ci_workflow.py`

**Interfaces:**
- Consumes: the step's existing guard (`-z $JOB_HUNTER_L2_API_KEY` → `--no-extract`) and the stub-`uv` harness in `tests/test_ci_workflow.py`.
- Produces: repo variable `JOB_HUNTER_EXTRACT_IN_FETCH`; `"false"` → the step passes `--no-extract`; anything else (including unset) → today's behavior, byte for byte.

- [ ] **Step 1: Write the failing test** — in `tests/test_ci_workflow.py`, a stub `uv` that records its argv to a file, run the sync step body with `JOB_HUNTER_EXTRACT_IN_FETCH=false` in env, assert the recorded args contain `--no-extract` and the step exits 0; a second case with the variable unset asserts `--no-extract` is NOT passed when an API key is present:

```python
@needs_shell
def test_extract_toggle_off_passes_no_extract(tmp_path: Path) -> None:
    argv_log = tmp_path / "argv.txt"
    proc = _run_sync_step_recording(
        tmp_path, argv_log, stdout=_envelope({"validated": 0}), code=0,
        env={"JOB_HUNTER_L2_API_KEY": "k", "JOB_HUNTER_EXTRACT_IN_FETCH": "false"},
    )
    assert proc.returncode == 0
    assert "--no-extract" in argv_log.read_text()
```

(`_run_sync_step_recording` is `_run_sync_step` with an argv-recording stub — add it beside the existing helper, reusing its structure.)

- [ ] **Step 2: Run to verify it fails** — the current step body ignores the variable.
- [ ] **Step 3: Implement** — in `fetch.yml`'s sync step: add `JOB_HUNTER_EXTRACT_IN_FETCH: ${{ vars.JOB_HUNTER_EXTRACT_IN_FETCH }}` to `env`, and change the guard's first branch to:

```bash
if [ -z "${JOB_HUNTER_L2_API_KEY}" ] || [ "${JOB_HUNTER_EXTRACT_IN_FETCH:-true}" = "false" ]; then
  echo "extraction skipped: toggled off or no key"
  args=(--no-extract)
```

- [ ] **Step 4: Run the whole CI-workflow test file** — `uv run pytest tests/test_ci_workflow.py -q` → green.
- [ ] **Step 5: Commit**, then flip the switches (owner-visible, reversible): `gh variable set JOB_HUNTER_EXTRACT_IN_FETCH --body false && gh workflow enable fetch.yml` — **collection resumes hourly with zero attempt writes**; record the first green scheduled run id in the ticket.

### Task 3: the drain-cycle driver

**Files:**
- Create: `scripts/codex_drain_cycle.py`
- Test: `tests/test_drain_cycle.py`

**Interfaces:**
- Consumes: `gh` CLI (dispatch/watch/download/release), `scripts/local_codex_drain.py` (subprocess), `openssl`, `COPYFILE_DISABLE=1 tar`.
- Produces: `uv run python scripts/codex_drain_cycle.py --batch 100 --cycles 0 --pool auto` — one process that loops dump → drain → upload → ingest until both pools are dry, codex throttles past the retry budget, or `--cycles` runs out. Every subprocess goes through an injectable `run()` so tests never touch the network.

**Behavior contract:**
1. `dump(pool, batch)` — `gh workflow run extract-queue-dump.yml -f count=<batch> -f pool=<pool>`, resolve the new run id (`gh run list --workflow=... --limit 1 --json databaseId,createdAt`, taken only if newer than dispatch time), `gh run watch --exit-status`, `gh run download` into a fresh working dir. Empty `queue.jsonl` → pool is dry.
2. `pool=auto` starts at `queue`; when `queue` returns 0 docs it switches to `quarantined`; when both return 0 the loop exits 0.
3. `drain` — `uv run python scripts/local_codex_drain.py <queue.jsonl> outbox/ --max-docs <batch>`; exit 3 (throttled) → sleep `--throttle-wait` minutes (default 45) and retry, at most `--throttle-retries` (default 4) times per cycle before exiting 3.
4. `upload` — skip when the outbox gained no new blobs; else `COPYFILE_DISABLE=1 tar`, `openssl enc` with `~/.config/job-hunter/outbox.key`, `gh release create outbox-<UTCstamp> --draft`, `gh workflow run outbox-ingest.yml -f tag=...`, watch it green.
5. Each cycle appends one JSON line (cycle, pool, dumped, drained, counts, ingest run id) to `outbox/cycle-log.jsonl`; the loop prints the same line to stdout.
6. No timestamps invented for keys — the drain script owns attempt stamping; the driver only orchestrates.

- [ ] **Step 1: Write the failing tests** — `tests/test_drain_cycle.py` with a `FakeRun` recording invocations and scripted outputs: (a) auto-pool switches queue→quarantined→exit on two empty dumps; (b) throttle exit 3 triggers wait-and-retry then gives up after the budget; (c) empty outbox delta skips upload; (d) a full happy cycle invokes dump → drain → tar/encrypt → release → ingest in order. Import the module's functions directly; no subprocess runs.
- [ ] **Step 2: Run to verify they fail** — module doesn't exist.
- [ ] **Step 3: Implement** `scripts/codex_drain_cycle.py` to the contract, all shell-outs through `run: Callable[..., CompletedProcess]` defaulting to `subprocess.run`, no heredocs, argparse flags as above.
- [ ] **Step 4: Full check** — `uv run pytest tests/test_drain_cycle.py -q && uv run ruff check . && uv run mypy` → green.
- [ ] **Step 5: Live smoke** — one real cycle at `--batch 5 --cycles 1`; verify the ingest summary's `replayed` ≥ the batch's blob count.
- [ ] **Step 6: Commit** — `feat(ops): unattended codex drain cycle`; add the driver invocation to `docs/runbooks/2026-09-09-local-codex-drain.md` as the preferred loop.

### Task 4: the recovery campaign (operational, commanded)

**Files:** none created — this task is dispatches and measurements, recorded in the ticket.

- [ ] **Step 1: $0 replay under validator/12** — after Task 1 merges: `gh workflow enable extract-backfill.yml && gh workflow run extract-backfill.yml -f mode=rebuild -f max_docs=0 -f max_usd=0 && gh workflow disable extract-backfill.yml`; watch green. Expected: a further tranche of the 306 comp-class docs revalidates free (the 91-anchor international share, minus mixed-noise docs).
- [ ] **Step 2: baseline counts** — `gh workflow run store-counts.yml`, record validated/quarantined/never-extracted.
- [ ] **Step 3: retry-pool campaign** — `uv run python scripts/codex_drain_cycle.py --pool quarantined --batch 100 --cycles 0` until the pool is dry (expected ~2–4 codex sessions across days, given plan limits; the driver's throttle backoff handles the pacing).
- [ ] **Step 4: measure and close** — store-counts again; the ticket records the before/after deltas. Success = quarantined-docs count falls by ≥150 (the mixed-noise cohort recovering at the observed ~70% fresh-generation rate); anything less reopens the analysis with a fresh error-class dump (`quarantine-dump.yml`).
- [ ] **Step 5: steady state** — leave `codex_drain_cycle.py --pool auto` as the standing loop against the 47k never-extracted backlog.

### Task 5: archive hygiene — the 114 junk keys (owner-gated)

**Files:**
- Create: `.github/workflows/archive-clean-junk.yml` (workflow_dispatch only)

**Interfaces:** `gh workflow run archive-clean-junk.yml -f confirm=DELETE` — lists every key under `extractions/attempts/` whose leaf starts with `._`, verifies each fails `parse_x_attempt_key`, prints the list, and deletes only when `confirm == "DELETE"`; any other value runs in list-only mode.

- [ ] **Step 1: Write the workflow** — a python step using `open_store(...).list(...)`, filtering leafs matching `/\._`, double-checking `keys.parse_x_attempt_key(key) is None` for every candidate, printing count + keys; deletion via `boto3` `delete_objects` on the archive bucket only under the confirm gate (the `ArchiveStore` interface deliberately has no delete — do not add one; the workflow talks to boto3 directly and says why in a comment).
- [ ] **Step 2: Dispatch list-only** — `gh workflow run archive-clean-junk.yml -f confirm=list`; verify the printed list is exactly 114 `._*` keys and nothing else.
- [ ] **Step 3 (MANUAL — owner):** the owner reviews the list-only output and, if satisfied, runs the DELETE dispatch themselves. This step is never run by an agent.
- [ ] **Step 4: Commit** the workflow with a comment stating the one-time purpose; delete the workflow file again once the cleanup has happened (a standing delete lever against a write-once archive should not exist).

### Task 6: docs and freeze

**Files:**
- Modify: `docs/runbooks/2026-09-09-local-codex-drain.md`, `docs/README.md`, `src/jobhunter/CLAUDE.md` (if not already touched by Tasks 1–3)

- [ ] **Step 1:** Runbook gains the driver loop, the `pool=quarantined` retry story, and the fetch toggle; the stand-down section says `gh variable set JOB_HUNTER_EXTRACT_IN_FETCH --body true` instead of implying MAX_DOCS tricks.
- [ ] **Step 2:** `docs/README.md` indexes this plan as the quarantine-recovery record; `validator/12` noted as frozen.
- [ ] **Step 3:** Final gate `uv run pytest tests/ -q && uv run ruff check . && uv run mypy` captured as merge evidence; commit.

## Backbone

1. Task 1 (validator/12) — unlocks the free replay tranche.
2. Task 2 (fetch toggle) — collection resumes; independent of Task 1.
3. Task 3 (driver) — independent of 1–2.
4. Task 4 (campaign) — needs 1 (replay) and 3 (driver).
5. Task 5 (hygiene) — independent, owner-gated.
6. Task 6 (docs) — last.

## Not in this plan

- Grounding-noise quarantines beyond what fresh retries recover — that is parsing v2 increment 2 (block-scoped references, audit/repair prompts), which has its own roadmap.
- The 361 `needs_review` rows — a human review queue, not an extraction defect.
- The 342 validator/2-era rows — their documents re-enter the normal queue by construction; no special handling.
- Any v2 cutover, store migration, or MCP/CLI surface change.
