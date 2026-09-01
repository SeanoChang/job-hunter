# Agentic CLI Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the `job-hunter` CLI around an AI agent as primary consumer: one output contract (JSON envelope, typed exit codes, teaching errors), a `pulse` delta verb with client-side cursors, a `q` read namespace, `sync` as the one operator entry, `doctor`/`schema`/`skill` introspection, and one derived table (`profile_mentions`).

**Architecture:** A new `cli_output.py` module owns the envelope/exit-code contract; every command routes output through it. New read queries live in `store/queries.py` (existing pattern: readers are plain SQL there). `q` verbs and `pulse` live in a new `cli_q.py` typer sub-app; cursors are client-side JSON state in `cursors.py`. Schema v3 adds `profile_mentions`, populated inside the existing single-writer `upsert_state`.

**Tech Stack:** Python 3.12, uv, typer, psycopg 3, pytest (store tests need `docker compose up -d postgres`; if docker is unavailable locally, run non-store tests and note it), ruff (line 100), mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-01-agentic-cli-rework-design.md` — read it first; it defines the envelope, exit codes, verb table, cursor semantics, and breaking changes.

## Global Constraints

- Strict typing: `uv run mypy` must stay clean; ruff line length 100.
- Env only via `config.py`; time only via `timeutil.py`; identity via `hashing.py`.
- All list verbs: `--limit` default 50, hard cap 500; `meta.truncated` always present; no dump verb.
- Exit codes: 0 OK · 1 verify findings · 2 usage · 3 config · 4 not found/ambiguous · 5 backend unavailable · 6 systemic. The constant `EXIT_SYSTEMIC = 2` is replaced everywhere.
- Data on stdout, diagnostics on stderr; no interactive prompts; `rebuild` requires `--yes` when stdin is not a TTY.
- Commit after every task with a conventional-commit message; branch `cli/agentic-rework` (already exists, spec committed).
- The old `--json` flag and the `report` command are deleted (spec §11); the fetch workflow, README, and runbook are updated in Task 11.
- Every command's envelope goes through `cli_output.emit`/`cli_output.fail` — no direct `json.dumps` to stdout outside `cli_output.py` (exception: `verify`'s report JSON passes through `emit` as `data`).

---

### Task 1: Output contract module (`cli_output.py`)

**Files:**
- Create: `src/jobhunter/cli_output.py`
- Test: `tests/test_cli_output.py`

**Interfaces:**
- Produces: `class Exit(IntEnum)` with members `OK=0, FINDINGS=1, USAGE=2, CONFIG=3, NOT_FOUND=4, BACKEND=5, SYSTEMIC=6`; `output_option()` returning a typer Option for `--output/-o`; `use_json(output: str | None) -> bool`; `emit(data, *, human, output, count=None, truncated=False, next_cursor=None, hint=None, extra_meta=None)`; `fail(kind: str, message: str, *, code: Exit, hint=None, valid=None, output=None) -> NoReturn`.
- Consumes: nothing project-internal (only typer, json, sys).

- [x] **Step 1: Write the failing tests**

```python
# tests/test_cli_output.py
"""The output contract: envelope shape, exit codes, TTY detection."""
import json

import pytest
import typer

from jobhunter.cli_output import Exit, emit, fail, use_json


def test_exit_codes_are_the_documented_table():
    assert [e.value for e in Exit] == [0, 1, 2, 3, 4, 5, 6]
    assert Exit.SYSTEMIC == 6 and Exit.USAGE == 2


def test_use_json_forced_modes():
    assert use_json("json") is True
    assert use_json("table") is False


def test_use_json_rejects_unknown_mode():
    with pytest.raises(typer.Exit) as e:
        use_json("yaml")
    assert e.value.exit_code == Exit.USAGE


def test_emit_json_envelope(capsys):
    emit([{"a": 1}], human="a", output="json", hint="next: q posting a")
    out = capsys.readouterr()
    body = json.loads(out.out)
    assert body["ok"] is True
    assert body["data"] == [{"a": 1}]
    assert body["meta"]["count"] == 1
    assert body["meta"]["truncated"] is False
    assert body["meta"]["hint"] == "next: q posting a"
    assert out.err == ""  # data stream stays pure


def test_emit_table_prints_human_and_hints_to_stderr(capsys):
    emit([{"a": 1}], human="one row", output="table", hint="try --full")
    out = capsys.readouterr()
    assert out.out == "one row\n"
    assert "try --full" in out.err


def test_fail_json_envelope_and_code(capsys):
    with pytest.raises(typer.Exit) as e:
        fail("not_found", "no document matches 'ab'", code=Exit.NOT_FOUND,
             hint="prefixes need >= 6 hex chars", output="json")
    assert e.value.exit_code == 4
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert body["error"]["kind"] == "not_found"
    assert body["error"]["hint"].startswith("prefixes")


def test_fail_table_goes_to_stderr(capsys):
    with pytest.raises(typer.Exit):
        fail("config", "JOB_HUNTER_ARCHIVE_URL is required", code=Exit.CONFIG,
             valid=None, output="table")
    out = capsys.readouterr()
    assert out.out == ""
    assert "JOB_HUNTER_ARCHIVE_URL" in out.err
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_output.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobhunter.cli_output'`

- [x] **Step 3: Implement**

```python
# src/jobhunter/cli_output.py
"""Output contract for every verb: envelope, exit codes, TTY detection.

Data goes to stdout; diagnostics go to stderr. Piped stdout emits exactly one
JSON envelope; a TTY renders human text. --output/-o overrides detection.
"""
from __future__ import annotations

import json
import sys
from enum import IntEnum
from typing import Any, NoReturn

import typer


class Exit(IntEnum):
    OK = 0
    FINDINGS = 1       # verify: checks ran, findings failed
    USAGE = 2          # bad flag/argument shape
    CONFIG = 3         # missing/invalid JOB_HUNTER_* configuration
    NOT_FOUND = 4      # unknown or ambiguous identifier
    BACKEND = 5        # DB/archive/network unavailable
    SYSTEMIC = 6       # everything the old exit 2 meant


OUTPUT_MODES = ("json", "table")


def output_option() -> Any:
    return typer.Option(
        None, "--output", "-o",
        help="Output mode: json or table. Default: json when stdout is piped, table on a TTY.",
    )


def use_json(output: str | None) -> bool:
    if output is None:
        return not sys.stdout.isatty()
    if output not in OUTPUT_MODES:
        fail("usage", f"--output must be one of: {', '.join(OUTPUT_MODES)}",
             code=Exit.USAGE, valid=list(OUTPUT_MODES), output="table")
    return output == "json"


def emit(
    data: Any,
    *,
    human: str,
    output: str | None,
    count: int | None = None,
    truncated: bool = False,
    next_cursor: str | None = None,
    hint: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> None:
    if use_json(output):
        meta: dict[str, Any] = {"truncated": truncated}
        if count is not None:
            meta["count"] = count
        elif isinstance(data, list):
            meta["count"] = len(data)
        if next_cursor is not None:
            meta["next_cursor"] = next_cursor
        if hint is not None:
            meta["hint"] = hint
        if extra_meta:
            meta.update(extra_meta)
        typer.echo(json.dumps({"ok": True, "data": data, "meta": meta},
                              ensure_ascii=False, default=str))
    else:
        typer.echo(human)
        if hint is not None:
            typer.echo(f"hint: {hint}", err=True)


def fail(
    kind: str,
    message: str,
    *,
    code: Exit,
    hint: str | None = None,
    valid: list[Any] | None = None,
    output: str | None = None,
) -> NoReturn:
    if output != "table" and use_json(output):
        typer.echo(json.dumps(
            {"ok": False,
             "error": {"kind": kind, "message": message, "hint": hint, "valid": valid}},
            ensure_ascii=False))
    else:
        typer.echo(f"error: {message}", err=True)
        if hint:
            typer.echo(f"hint: {hint}", err=True)
        if valid:
            typer.echo(f"valid: {', '.join(str(v) for v in valid)}", err=True)
    raise typer.Exit(int(code))
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_output.py -q && uv run ruff check src/jobhunter/cli_output.py && uv run mypy`
Expected: PASS, clean.

- [x] **Step 5: Commit**

```bash
git add src/jobhunter/cli_output.py tests/test_cli_output.py
git commit -m "feat(cli): output contract — envelope, typed exit codes, TTY detection"
```

---

### Task 2: Config file loading + state dir (`config.py`)

**Files:**
- Modify: `src/jobhunter/config.py`
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `Settings.load(env=None)` unchanged in signature but now merging file layers; new module function `load_env_files(environ: Mapping[str, str]) -> dict[str, str]`; new `Settings.state_dir: Path` field (from `JOB_HUNTER_STATE_DIR`, default `$XDG_STATE_HOME/job-hunter` else `~/.local/state/job-hunter`).
- Consumes: nothing new.

Precedence (spec §7): process env > `./.env` > `~/.config/job-hunter/env`. Files are simple `KEY=VALUE` lines; `#` comments and blank lines ignored; no quoting rules (values taken verbatim after the first `=`). Only keys starting with `JOB_HUNTER_` or `AWS_` are read from files — a stray `PATH=` in a .env must never leak into settings.

- [x] **Step 1: Write the failing tests** (append to `tests/test_config.py`)

```python
def test_env_file_layering(tmp_path, monkeypatch):
    from jobhunter.config import load_env_files

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "JOB_HUNTER_ARCHIVE_URL=file:///from-dotenv\n# comment\nPATH=/evil\n")
    cfg = tmp_path / "cfghome" / "job-hunter"
    cfg.mkdir(parents=True)
    (cfg / "env").write_text(
        "JOB_HUNTER_ARCHIVE_URL=file:///from-config\nJOB_HUNTER_DROP_RATIO=0.7\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfghome"))
    merged = load_env_files({"JOB_HUNTER_DATABASE_URL": "postgresql://x"})
    assert merged["JOB_HUNTER_ARCHIVE_URL"] == "file:///from-dotenv"  # .env beats config
    assert merged["JOB_HUNTER_DROP_RATIO"] == "0.7"                   # config fills gaps
    assert merged["JOB_HUNTER_DATABASE_URL"] == "postgresql://x"      # process env survives
    assert "PATH" not in merged                                       # non-prefixed keys ignored


def test_process_env_beats_files(tmp_path, monkeypatch):
    from jobhunter.config import load_env_files

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JOB_HUNTER_ARCHIVE_URL=file:///from-dotenv\n")
    merged = load_env_files({"JOB_HUNTER_ARCHIVE_URL": "file:///from-process"})
    assert merged["JOB_HUNTER_ARCHIVE_URL"] == "file:///from-process"


def test_state_dir_default_and_override(monkeypatch):
    from jobhunter.config import Settings

    s = Settings.load({"JOB_HUNTER_ARCHIVE_URL": "file:///a",
                       "JOB_HUNTER_STATE_DIR": "/tmp/js"})
    assert str(s.state_dir) == "/tmp/js"
    s2 = Settings.load({"JOB_HUNTER_ARCHIVE_URL": "file:///a", "HOME": "/home/u"})
    assert str(s2.state_dir).endswith(".local/state/job-hunter")
```

- [x] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'load_env_files'`

- [x] **Step 3: Implement**

In `config.py`: add to the dataclass `state_dir: Path = Path("~/.local/state/job-hunter")`. Add:

```python
_FILE_KEY_PREFIXES = ("JOB_HUNTER_", "AWS_")


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith(_FILE_KEY_PREFIXES):
            out[key] = value.strip()
    return out


def load_env_files(environ: Mapping[str, str]) -> dict[str, str]:
    """Merge config layers: process env > ./.env > ~/.config/job-hunter/env."""
    xdg = environ.get("XDG_CONFIG_HOME") or str(
        Path(environ.get("HOME", "~")).expanduser() / ".config")
    merged = _read_env_file(Path(xdg) / "job-hunter" / "env")
    merged.update(_read_env_file(Path.cwd() / ".env"))
    merged.update(environ)
    return merged
```

In `Settings.load`, first line becomes:

```python
        e: Mapping[str, str] = load_env_files(os.environ) if env is None else env
```

(passing `env` explicitly — the test path — skips file loading, preserving every existing test). Compute `state_dir` before the final `return cls(...)`:

```python
        if e.get("JOB_HUNTER_STATE_DIR"):
            state_dir = Path(e["JOB_HUNTER_STATE_DIR"])
        else:
            xdg_state = e.get("XDG_STATE_HOME") or str(
                Path(e.get("HOME", "~")).expanduser() / ".local/state")
            state_dir = Path(xdg_state) / "job-hunter"
```

and add `state_dir=state_dir` to the constructor call.

- [x] **Step 4: Run full config tests**

Run: `uv run pytest tests/test_config.py -q && uv run mypy`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/jobhunter/config.py tests/test_config.py
git commit -m "feat(config): .env + ~/.config/job-hunter/env layering, state_dir"
```

---

### Task 3: Migrate every existing command onto the contract

**Files:**
- Modify: `src/jobhunter/cli.py` (whole file), `tests/test_cli.py`, `tests/test_fetch.py` (only where it invokes the CLI), any test using `--json`
- Test: existing suites, updated

**Interfaces:**
- Consumes: Task 1's `Exit`, `emit`, `fail`, `output_option`.
- Produces: every command gains `output: str | None = output_option()` in place of `as_json: bool = typer.Option(False, "--json")`; module-level `EXIT_SYSTEMIC` is deleted.

This is one mechanical transformation applied to each command. The pattern, worked on `ingest`:

```python
# BEFORE (per command):
def ingest(as_json: bool = typer.Option(False, "--json")) -> None:
    ...
    typer.echo(f"archive error: {e}")
    raise typer.Exit(EXIT_SYSTEMIC) from e
    ...
    _emit({...}, as_json, human_text)

# AFTER:
def ingest(output: str | None = output_option()) -> None:
    ...
    fail("backend", f"archive error: {e}", code=Exit.BACKEND, output=output)
    ...
    emit({...}, human=human_text, output=output, hint=hint_or_None)
```

Error-class mapping (apply consistently; `fail` from inside `except` blocks — typer.Exit propagates through the existing `finally` blocks unchanged):

| old site | kind | code |
| --- | --- | --- |
| `config error:` (`_settings`, `_conn`, `require_l2`) | `config` | `Exit.CONFIG` |
| `database error:` / `archive error:` (connection, psycopg, ArchiveError) | `backend` | `Exit.BACKEND` |
| `registry error:` / `schema error:` / `rebuild error:` / `engine error:` / gaps / breaker | `systemic` | `Exit.SYSTEMIC` |
| `--board must look like source:board` (`_split_board`), bad prefix shape in `_resolve_doc` | `usage` | `Exit.USAGE` |
| `no document matches` / `is ambiguous` (`_resolve_doc`), `no extraction row for` | `not_found` | `Exit.NOT_FOUND` (ambiguous hint: "lengthen the prefix") |
| `verify` findings fail | unchanged `raise typer.Exit(1)` (`Exit.FINDINGS`) |

Sites to convert (every one; `grep -n 'as_json\|EXIT_SYSTEMIC' src/jobhunter/cli.py` must return nothing afterwards): module docstring, `_settings`, `_conn`, `_store`, `_emit` (delete it), `_split_board`, `_resolve_doc`, `_load_stored_extraction`, `_verify_output`, `version`, `verify`, `fetch`, `ingest`, `rebuild`, `report` (converted here, deleted in Task 11), `status`, `archive_ls`, `registry_check`, `registry_list`, `db_init`, `db_version`, `extract_run`, `extract_rebuild`, `_review_verb`, `review_list`, `review_show`, `review_accept/reject/retry/flag`, `extract_show`.

Helpers `_settings`/`_conn`/`_store`/`_resolve_doc` need the active `output` mode: give each an `output: str | None` parameter and pass it through from each command (mypy will find every call site).

`rebuild` additionally gains `yes: bool = typer.Option(False, "--yes")` and, before doing work:

```python
    if not yes and not sys.stdin.isatty():
        fail("usage", "rebuild replaces the live schema",
             hint="re-run with --yes to confirm non-interactively",
             code=Exit.USAGE, output=output)
```

Test updates in the same task (the suite is the contract's proof):
- Every `runner.invoke(app, [..., "--json"])` becomes `[..., "-o", "json"]` and the assertion unwraps the envelope: `json.loads(result.stdout)["data"]` (list payloads) — commands that emitted a dict keep it under `"data"` too.
- Every human-output assertion adds `"-o", "table"` (CliRunner is not a TTY, so the default is now JSON).
- Every `assert result.exit_code == 2` for a systemic path becomes `== 6`; config-error tests `== 3`; unknown-board/bad-prefix `== 2` (usage) or `== 4` (not found) per the mapping. Read each test's intent, don't sed blindly.
- Add one new test: piped default emits an envelope:

```python
def test_version_pipes_envelope_by_default(runner):
    r = runner.invoke(app, ["version"])
    body = json.loads(r.stdout)
    assert body["ok"] is True and body["data"]["version"]
```

- [x] **Step 1:** Convert `cli.py` per the mapping above (single pass, keep commits atomic to this task).
- [x] **Step 2:** Run `uv run pytest tests/test_cli.py -q` — expect many failures listing exactly the assertions to update.
- [x] **Step 3:** Update the tests per the rules above.
- [x] **Step 4:** Run: `uv run pytest -q && uv run ruff check . && uv run mypy` — full suite green (store tests need Postgres; if unavailable, run `uv run pytest -q --ignore=tests/store --ignore=tests/integration` and say so in the commit body).
- [x] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(cli)!: every verb speaks the envelope contract; typed exit codes; --json removed"
```

---

### Task 4: New read queries (`store/queries.py`)

**Files:**
- Modify: `src/jobhunter/store/queries.py`
- Test: `tests/store/test_queries.py` (append; uses existing store-test fixtures/helpers from `tests/store/helpers.py` and `tests/conftest.py` — follow the ingestion patterns already in that file to seed postings/events)

**Interfaces (produced; consumed by Tasks 5–6):**

```python
def postings_page(conn, *, source: str | None = None, board: str | None = None,
                  status: str | None = None, since: datetime | None = None,
                  search: str | None = None, limit: int = 50,
                  after: str | None = None) -> list[dict[str, Any]]: ...
    # row: uid, source, board, status, title, company, url, first_seen_at,
    #      last_seen_at, version_count, reopen_count, closed_lower_at, closed_upper_at
    # order: first_seen_at DESC, uid DESC; keyset cursor "iso_first_seen_at|uid";
    # fetch limit+1 rows so the caller knows truncated.

def posting_detail(conn, uid: str) -> dict[str, Any] | None: ...
    # posting row + versions: [{version_hash, title, at=first_seen}], events: [...],
    # current document_hash (join documents on current_version_hash, latest normalizer)

def events_page(conn, *, since: datetime | None = None, kinds: tuple[str, ...] | None = None,
                source: str | None = None, board: str | None = None, uid: str | None = None,
                limit: int = 50, after_event_id: int | None = None) -> list[dict[str, Any]]: ...
    # events_since's join + filters + keyset on event_id; fetch limit+1.

def events_after_watermark(conn, *, at: datetime, exclude_ids: tuple[int, ...],
                           limit: int) -> list[dict[str, Any]]: ...
    # WHERE (e.at > %(at)s OR (e.at = %(at)s AND NOT e.event_id = ANY(%(ids)s)))
    # ORDER BY e.at, e.event_id LIMIT limit+1 — pulse's delta feed.

def boards_overview(conn) -> list[dict[str, Any]]: ...
    # board_health ⨝ open_counts, one row per board: board, health, open, error, started_at

def docs_for_events(conn, uids: list[str], normalizer_version: str) -> dict[str, str]: ...
    # uid -> document_hash of the CURRENT version under normalizer_version

def validated_profiles(conn, doc_hashes: list[str], *, model_regex: str,
                       prompt_version: str, schema_version: str,
                       validator_version: str) -> dict[str, dict[str, Any]]: ...
    # document_hash -> profile JSONB for status='validated' rows in the engine glob
```

- [x] **Step 1:** Write failing tests in `tests/store/test_queries.py` covering: keyset pagination returns page 2 without overlap (`postings_page` with `after` from page 1's last row); `search` matches title OR company case-insensitively; `events_after_watermark` excludes the tie-break ids at the watermark instant and includes a later event at the same timestamp; `posting_detail` returns `None` for unknown uid; `validated_profiles` filters on status and model regex. Seed data exactly as neighbouring tests in that file do (via lifecycle ingest of fixture manifests or direct helpers — copy the file's existing setup idiom).
- [x] **Step 2:** Run `uv run pytest tests/store/test_queries.py -q` — expect failures (missing functions). (Needs Postgres: `docker compose up -d postgres`.)
- [x] **Step 3:** Implement the seven functions with plain parametrized SQL, `%(name)s` style, reusing `events_since`'s join for title/company/url. `search` becomes `AND (v.title ILIKE %(q)s OR v.company ILIKE %(q)s)` with `q = f"%{search}%"`.
- [x] **Step 4:** `uv run pytest tests/store -q && uv run mypy` — PASS.
- [x] **Step 5: Commit** — `git add -A && git commit -m "feat(store): agent-facing read queries — pages, watermark delta, profiles"`

---

### Task 5: The `q` namespace (`cli_q.py`)

**Files:**
- Create: `src/jobhunter/cli_q.py`
- Modify: `src/jobhunter/cli.py` (add `from jobhunter.cli_q import q_app` + `app.add_typer(q_app, name="q")`)
- Test: `tests/test_cli_q.py`

**Interfaces:**
- Consumes: Task 4 queries, Task 1 contract, `_resolve_doc`/`_conn`/`_settings`/`_store` from `cli.py` (import them — they are module functions).
- Produces: commands `q postings`, `q posting UID`, `q events`, `q boards`, `q document PREFIX`, `q profile --doc PREFIX [--full]`. (`q claims` arrives in Task 7.)

Shared flags on list verbs: `--limit int = 50` (clamped: `limit = max(1, min(limit, 500))`), `--after str|None`, `--fields str|None` (comma list; unknown field → `fail("usage", ..., valid=sorted(row.keys()))`), `-o/--output`. Every verb: fetch `limit+1`, emit `truncated=len(rows)>limit`, `next_cursor` from the last emitted row.

Representative implementation (`q postings`) — the others follow the same skeleton with their Task-4 query:

```python
# src/jobhunter/cli_q.py
"""Read-only q namespace: the agent-facing query surface (spec §4)."""
from __future__ import annotations

from typing import Any

import typer

from jobhunter.cli_output import Exit, emit, fail, output_option
from jobhunter.timeutil import iso

q_app = typer.Typer(help="Read the corpus: postings, events, documents, profiles")

MAX_LIMIT = 500


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _select_fields(rows: list[dict[str, Any]], fields: str | None,
                   output: str | None) -> list[dict[str, Any]]:
    if not fields:
        return rows
    want = [f.strip() for f in fields.split(",") if f.strip()]
    if rows:
        unknown = [f for f in want if f not in rows[0]]
        if unknown:
            fail("usage", f"unknown field(s): {', '.join(unknown)}",
                 valid=sorted(rows[0]), code=Exit.USAGE, output=output)
    return [{k: r[k] for k in want if k in r} for r in rows]


@q_app.command("postings")
def q_postings(
    board: str | None = typer.Option(None, "--board", help="source:board"),
    status: str | None = typer.Option(None, "--status", help="open|closed"),
    since: str | None = typer.Option(None, "--since", help="Nm, Nh or Nd (first seen)"),
    search: str | None = typer.Option(None, "--search", help="ILIKE over title+company"),
    fields: str | None = typer.Option(None, "--fields"),
    limit: int = typer.Option(50, "--limit"),
    after: str | None = typer.Option(None, "--after", help="opaque cursor from meta.next_cursor"),
    output: str | None = output_option(),
) -> None:
    """List postings, newest first. Bounded; meta.truncated + meta.next_cursor page on."""
    from jobhunter.cli import _conn, _parse_since, _settings, _split_board
    from jobhunter.store.queries import postings_page

    if status not in (None, "open", "closed"):
        fail("usage", f"--status must be open or closed: {status!r}",
             valid=["open", "closed"], code=Exit.USAGE, output=output)
    src, brd = _split_board(board)
    settings = _settings(output)
    limit = _clamp(limit)
    conn = _conn(settings, output=output)
    try:
        rows = postings_page(
            conn, source=src, board=brd, status=status,
            since=(_now() - _parse_since(since)) if since else None,
            search=search, limit=limit, after=after)
    except Exception as e:
        fail("backend", f"database error: {e}", code=Exit.BACKEND, output=output)
    finally:
        conn.close()
    truncated = len(rows) > limit
    rows = rows[:limit]
    data = [{**r, "first_seen_at": iso(r["first_seen_at"]),
             "last_seen_at": iso(r["last_seen_at"]),
             "closed_between": [iso(r["closed_lower_at"]), iso(r["closed_upper_at"])]
             if r["closed_lower_at"] else None} for r in rows]
    for d in data:
        d.pop("closed_lower_at", None); d.pop("closed_upper_at", None)
    data = _select_fields(data, fields, output)
    cursor = (f"{rows[-1]['first_seen_at'].isoformat()}|{rows[-1]['uid']}"
              if truncated and rows else None)
    human = "\n".join(
        f"{r['status']:6} {r['uid']:44} {(r.get('company') or '-'):18} {r.get('title') or '-'}"
        for r in data) or "(no postings)"
    emit(data, human=human, output=output, truncated=truncated, next_cursor=cursor,
         hint=data and f"q posting {rows[0]['uid']} for lifecycle detail" or None)
```

(`_now` imported from `jobhunter.cli` alongside the helpers.) `q document` reuses `_resolve_doc` + `markdown_for`, takes `--slice S:E` parsed as codepoint ints, and emits `{"document_hash": ..., "markdown": text}` (human mode prints the markdown raw). `q profile` reuses the loading part of `extract_show` (extract the row-loading into a helper `_profile_row(conn, doc)` in `cli_q.py`); default output is the summary produced by Task 6's `profile_summary`; `--full` emits the stored `profile` verbatim; no validated row → `fail("not_found", ..., hint="run: job-hunter extract run --doc <hash>", code=Exit.NOT_FOUND)`.

- [x] **Step 1:** Write `tests/test_cli_q.py` following `tests/test_cli.py`'s runner/monkeypatch idiom (substitute `_conn`/`_settings` the way existing CLI tests do; where CLI tests hit real Postgres via fixtures, do the same): cover — envelope shape on `q postings`; `--status bogus` exits 2 with `valid` list; `--limit 9999` clamps to 500; truncation sets `next_cursor` and a second call with `--after` returns the next page without overlap; `q posting unknown-uid` exits 4; `q document` with an ambiguous 4-hex prefix exits 4 with the lengthen-hint; `q profile --doc X` on a validated fixture returns summary keys `areas/mentions/facts`; `--fields uid,title` drops other keys and rejects unknown names.
- [x] **Step 2:** `uv run pytest tests/test_cli_q.py -q` — FAIL (no module).
- [x] **Step 3:** Implement all six verbs.
- [x] **Step 4:** `uv run pytest tests/test_cli_q.py tests/test_cli.py -q && uv run ruff check . && uv run mypy` — PASS.
- [x] **Step 5: Commit** — `git commit -am "feat(cli): q namespace — postings, posting, events, boards, document, profile"`

---

### Task 6: Cursors + `pulse`

**Files:**
- Create: `src/jobhunter/cursors.py`, `src/jobhunter/pulse.py`
- Modify: `src/jobhunter/cli_q.py` (or register on `app`): add top-level `pulse` command in `cli.py`
- Test: `tests/test_cursors.py`, `tests/test_pulse.py`

**Interfaces:**

```python
# cursors.py
@dataclass(frozen=True)
class Watermark:
    at: str                        # ISO-8601 UTC of newest reported event
    event_ids_at: tuple[int, ...]  # ids at exactly `at` (tie-break; survive rebuild by at)

def read_cursor(state_dir: Path, name: str) -> Watermark | None
def write_cursor(state_dir: Path, name: str, wm: Watermark) -> None
    # single JSON file state_dir/"cursors.json" {name: {"at":…, "event_ids_at":[…]}};
    # atomic: write tmp file in the same dir, os.replace.

# pulse.py
def profile_summary(profile: dict[str, Any]) -> dict[str, Any]
    # {"areas": [{"name","kind","importance","level"}...],
    #  "mentions": top 8 unique across areas,
    #  "facts": {"compensation": [{"min","max","currency","period"}...],
    #            "experience_months": {"min","max"} | None,
    #            "deadline": date | None}}

def build_pulse(conn, settings, *, wm: Watermark | None, limit: int,
                boards: tuple[str, ...] | None, now: datetime) -> tuple[dict, Watermark | None]
    # payload: {"window": {"from","to"}, "first_run": bool, "events": [...],
    #           "attention": {"unhealthy_boards": [...], "extraction": {...} | None}}
    # events: q-events shape + "profile": profile_summary(...) | None on opened/changed
    # returns new watermark from last emitted event (None when no events)
```

`pulse` command skeleton (in `cli.py`; the flags: `--cursor NAME` default `"default"`, `--since TS` mutually exclusive with `--cursor`, `--peek`, `--boards`, `--limit`, `-o`):

```python
@app.command()
def pulse(
    cursor: str = typer.Option("default", "--cursor"),
    since: str | None = typer.Option(None, "--since", help="ISO timestamp; bypasses the cursor"),
    boards: str | None = typer.Option(None, "--boards", help="comma list of source:board"),
    peek: bool = typer.Option(False, "--peek", help="Report without advancing the cursor"),
    limit: int = typer.Option(200, "--limit"),
    output: str | None = output_option(),
) -> None:
    """Everything new since the last pulse: events, profiles, attention. One call."""
    ...
    wm = None if since else read_cursor(settings.state_dir, cursor)
    payload, new_wm = build_pulse(conn, settings, wm=..., limit=_clamp(limit), ...)
    truncated = payload.pop("_truncated")
    emit(payload, human=_pulse_human(payload), output=output, truncated=truncated,
         hint=payload["events"] and "q profile --doc <hash> / q posting <uid> to drill down"
         or None,
         extra_meta={"cursor": cursor, "first_run": payload["first_run"]})
    if not peek and since is None and new_wm is not None:
        write_cursor(settings.state_dir, cursor, new_wm)   # AFTER emit: crash re-reports
```

`build_pulse` internals: no watermark → `events_page(conn, since=now - timedelta(hours=24), limit=limit)` and `first_run=True`; with watermark → `events_after_watermark(conn, at=parse(wm.at), exclude_ids=wm.event_ids_at, limit=limit)`. Enrich: collect uids of opened/changed events → `docs_for_events` → `validated_profiles` (glob regex from `settings.l2_models` via `globs_to_regex`, versions from the same imports `_extraction_block` uses) → attach `profile_summary`. Attention: `boards_overview` filtered to `health != 'ok'`, plus `_extraction_block(settings)`-shaped dict (reuse that helper). If truncated: new watermark comes from the last *emitted* event (spec §3).

- [ ] **Step 1:** Failing tests. `tests/test_cursors.py`: round-trip; missing file → None; atomicity (write, then corrupt tmp leftovers don't matter — assert `cursors.json` parses); two names coexist. `tests/test_pulse.py`: `profile_summary` on the fixture `tests/l2/fixtures/anthropic.extraction.json`'s record produces the documented keys and ≤8 mentions; `build_pulse` with a fake conn (monkeypatched query functions, as CLI tests do) — first-run flag set without watermark; watermark excludes tie-break ids; truncation caps events and the returned watermark matches the last emitted event, not the last DB event. CLI-level test: `pulse --peek` twice returns identical events; without `--peek` the second call returns empty; crash simulation — monkeypatch `write_cursor` to raise after `emit`, assert the *next* call re-reports (cursor unchanged on disk).
- [ ] **Step 2:** Run both test files — FAIL.
- [ ] **Step 3:** Implement `cursors.py`, `pulse.py`, wire the command.
- [ ] **Step 4:** `uv run pytest tests/test_cursors.py tests/test_pulse.py -q && uv run mypy` — PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(cli): pulse — cursor-driven delta with profile summaries and attention block"`

---

### Task 7: Schema v3 — `profile_mentions` + `q claims`

**Files:**
- Modify: `src/jobhunter/store/schema.sql`, `src/jobhunter/store/db.py` (SCHEMA_VERSION, upgrades), `src/jobhunter/store/extraction.py` (`upsert_state`), `src/jobhunter/store/queries.py` (`claims_by_mention`), `src/jobhunter/cli_q.py` (`q claims`)
- Test: `tests/store/test_extraction.py` (append), `tests/test_cli_q.py` (append)

**Interfaces:**
- Produces: `claims_by_mention(conn, *, mention, importance=None, source=None, board=None, limit=50) -> list[dict]` (row: document_hash, mention, area_kind, importance, plus title/company/url of a posting currently on that document); `q claims --mention X [--importance] [--board] [--limit]`.

DDL appended to `schema.sql` (idempotent, so `_ADDITIVE_UPGRADES` handles the migration):

```sql
-- derived from extractions.profile by store/extraction.upsert_state; rebuildable
CREATE TABLE IF NOT EXISTS profile_mentions (
  document_hash     TEXT NOT NULL,
  model             TEXT NOT NULL,
  prompt_version    TEXT NOT NULL,
  schema_version    TEXT NOT NULL,
  validator_version TEXT NOT NULL,
  mention           TEXT NOT NULL,
  area_kind         TEXT NOT NULL,
  importance        TEXT NOT NULL,
  PRIMARY KEY (document_hash, model, prompt_version, schema_version,
               validator_version, mention, area_kind, importance)
);
CREATE INDEX IF NOT EXISTS ix_mentions_mention ON profile_mentions (mention, importance);
```

`db.py`: `SCHEMA_VERSION = "3"`; `_ADDITIVE_UPGRADES = {("1", "2"), ("2", "3"), ("1", "3")}`.

`upsert_state`: both DELETE branches also `DELETE FROM profile_mentions` with the same predicates (config-scoped; and model-mismatch-scoped). After the extractions INSERT, when `profile` is not None and `state.status == "validated"`:

```python
    conn.execute(
        "DELETE FROM profile_mentions WHERE document_hash=%s AND model=%s"
        " AND prompt_version=%s AND schema_version=%s AND validator_version=%s", key)
    rows = [
        (*key, mention, area["kind"], area["importance"])
        for area in ((profile.get("demand_profile") or {}).get("areas") or [])
        for mention in dict.fromkeys(area.get("mentions") or [])
    ]
    if rows:
        conn.executemany(
            "INSERT INTO profile_mentions (document_hash, model, prompt_version,"
            " schema_version, validator_version, mention, area_kind, importance)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", rows)
```

(Non-validated statuses keep the config's mentions deleted — validated-only aggregates, per the 2026-08-26 ruling.) `extract rebuild` and store `rebuild` already flow through `upsert_state`, so replay repopulates the table for free — the test proves it.

- [ ] **Step 1:** Failing tests: `upsert_state` with a validated state + the anthropic fixture profile inserts mention rows keyed by the engine tuple; re-upserting as `rejected` clears them; `rebuild_extractions` (existing test setup in `tests/l2/test_rebuild.py` — extend there if more natural) leaves the table populated; `claims_by_mention(mention="Python")` finds the doc; `q claims --mention Python` emits envelope rows and `--importance bogus` exits 2 with `valid`.
- [ ] **Step 2:** Run — FAIL (missing table/function).
- [ ] **Step 3:** Implement DDL, version bump, writer, query, verb.
- [ ] **Step 4:** `uv run pytest tests/store tests/l2/test_rebuild.py tests/test_cli_q.py -q && uv run mypy` — PASS. Also run `uv run pytest tests/store/test_db.py -q` to prove the ("2","3") additive upgrade path.
- [ ] **Step 5: Commit** — `git commit -am "feat(store): schema v3 profile_mentions + q claims --mention"`

---

### Task 8: `sync`

**Files:**
- Modify: `src/jobhunter/cli.py`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Produces: `job-hunter sync [--no-extract] [--extract-max-docs N] [-o]` — runs exactly the CI choreography: (1) `replay_pending` under the ingest lock, (2) `fetch_run`, (3) unless `--no-extract` and only when `settings.l2_model_candidates` is non-empty: `l2_runner.run` under its own lock. Emits one envelope `{"ingest": {...}, "fetch": {...}, "extract": {... | "skipped_reason"}}`.

Semantics (mirror the workflow's philosophy): collection failure → exit per Task 3's mapping; extraction failure is captured as `data["extract"]["error"]` and does **not** fail the run (collection is irreplaceable, extraction recomputable — CI comment in `fetch.yml`), except `breaker_abort`/all-throttled which sets exit `Exit.SYSTEMIC` exactly as `extract_run` does today. Ingest gaps → hint + `Exit.SYSTEMIC` (unchanged behavior, now aggregated). Reuse the bodies by extracting the current `ingest` and `extract_run` command internals into private helpers `_ingest_once(settings, store, output) -> dict` and `_extract_once(settings, store, output, max_docs, max_usd) -> dict` that both the standalone commands and `sync` call — no logic duplication.

- [ ] **Step 1:** Failing tests: `sync` with the fake fetcher + tmp archive (the idiom `tests/test_cli.py` already uses for `fetch`) returns an envelope with all three keys ordered ingest→fetch→extract; `--no-extract` yields `{"extract": {"skipped_reason": "--no-extract"}}`; missing L2 candidates yields `skipped_reason: "no JOB_HUNTER_L2_MODEL_CANDIDATES"`; an ingest gap exits 6.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement (helpers first, then the command).
- [ ] **Step 4:** `uv run pytest tests/test_cli.py -q && uv run mypy` — PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(cli): sync — ingest, fetch, budgeted extract in one verb"`

---

### Task 9: `doctor`

**Files:**
- Modify: `src/jobhunter/cli.py`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Produces: `job-hunter doctor [-o]`. Data: `{"checks": [{"name", "ok", "detail", "hint"}...]}`. Exit: 0 all ok · 3 any config check failed · 5 config ok but a probe failed.

Checks, in order (each independent; run all, never stop at the first failure):
1. `archive_url` — set? parseable by `open_store`? hint: `export JOB_HUNTER_ARCHIVE_URL=s3://bucket/prefix (or file:///path)`.
2. `aws_credentials` — only when archive_url is s3: `AWS_ENDPOINT_URL`/`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_DEFAULT_REGION` present (never print values; detail is "set"/"missing: NAME").
3. `archive_probe` — `open_store(...)` then one cheap read (`latest_per_board` wrapped, or `store.exists` on a known-impossible key just to exercise the backend); failure detail is the exception class + message.
4. `database_url` — set? hint names the env var.
5. `database_probe` — `_db.connect` + `SELECT 1`; failure → hint "is Postgres reachable? docker compose up -d postgres for local".
6. `schema_version` — `stored_schema_version` vs `_db.SCHEMA_VERSION`; mismatch hint: "run: job-hunter rebuild".
7. `role` — `SELECT has_table_privilege(current_user, 'postings', 'INSERT') AS w` (inside the schema search_path); when `w` is true, `ok: true` with detail `"writer DSN — fine for operators; use a read-only role on agent machines"` (informational, not a failure).
8. `l2` — engine config coherent per `require_l2()` when candidates are set; unset → ok with detail "extraction not configured (optional)".

Human rendering: one line per check, `ok`/`FAIL` + detail, hints on failures.

- [ ] **Step 1:** Failing tests: empty env → checks 1 and 4 fail, exit 3, every failing check carries a non-empty hint; file archive + working test DB (store fixture) → exit 0; good config but unreachable DB (DSN `postgresql://nobody:x@127.0.0.1:1/x`) → exit 5 and `database_probe.ok is False`.
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** `uv run pytest tests/test_cli.py -q` — PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(cli): doctor — config, connectivity, schema and role checks with fixes"`

---

### Task 10: `schema` + `skill`

**Files:**
- Create: `src/jobhunter/skill_data/SKILL.md` (package data; add to `[tool.hatch]`/`pyproject.toml` package-data the same way `l2/schemas_data` ships — check how that is included and mirror it)
- Modify: `src/jobhunter/cli.py`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- `job-hunter schema [-o]` — data: `{"contract": {"envelope": {…json-schema…}, "exit_codes": {"0": "success", …}}, "versions": {"cli", "schema_version", "normalizer", "prompt", "validator"}, "commands": [{"path": "q postings", "help": …, "params": [{"name","opts","type","default","choices"}...]}...]}`. Built by walking the live click tree: `typer.main.get_command(app)` → recurse `.commands` for `click.Group`s; params from `cmd.params` (name, opts, type name, default, `choices` when the type has them). Generated, never hand-written — it cannot drift.
- `job-hunter skill` — prints `skill_data/SKILL.md` verbatim via `importlib.resources` (stdout, no envelope: the file IS the payload; `-o json` wraps it as `{"markdown": …}`).

SKILL.md content (write it fully in this task — frontmatter `name: job-hunter-cli`, `description: Query Sean's job-posting corpus and compose update digests via the job-hunter CLI`; body sections: the hourly loop — `pulse --cursor <name> -o json`, empty ⇒ quiet no-op, else compose update and drill down with `q profile --doc` / `q document`; the exit-code table and what to do per code — 3 ⇒ run `doctor` and relay, 4 ⇒ lengthen prefix or re-list, 5 ⇒ retry later, never loop; token economy — prefer `--fields`, summaries before `--full`, never raise `--limit` past what you will read; install hint: `job-hunter skill > ~/.claude/skills/job-hunter-cli/SKILL.md`).

- [ ] **Step 1:** Failing tests: `schema -o json` envelope contains every registered command path including `q postings` and `pulse` (walk assertion: `"q postings" in {c["path"] for c in body["data"]["commands"]}`); exit-code map has 7 entries; `skill` output starts with `---` frontmatter and mentions `pulse --cursor`.
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement both + write SKILL.md. **Step 4:** `uv run pytest tests/test_cli.py -q && uv run mypy` — PASS; also `uv run python -c "import importlib.resources as r; print(r.files('jobhunter.skill_data').joinpath('SKILL.md').read_text()[:40])"` to prove packaging.
- [ ] **Step 5: Commit** — `git commit -am "feat(cli): schema introspection + shipped agent skill"`

---

### Task 11: Deletions, CI, docs sweep

**Files:**
- Modify: `src/jobhunter/cli.py` (delete `report`), `tests/test_cli.py` (drop report tests — `q events`/`pulse` tests already cover the behavior), `.github/workflows/fetch.yml`, `README.md`, `CLAUDE.md`, `src/jobhunter/CLAUDE.md`, `docs/runbooks/2026-08-18-deploy-fetcher.md`, `docs/README.md`

- [ ] **Step 1:** Delete the `report` command and its tests. `grep -rn '\breport\b' src tests README.md` — remove/replace every CLI reference.
- [ ] **Step 2:** `fetch.yml`: replace the three steps `ingest pending` / `fetch` / `extract` (if present on main) with one `sync` step carrying the union of their env (secrets stay step-scoped to this one step; keep `| tee summary.json`, keep `continue-on-error` OFF because sync already downgrades extraction failure internally); keep `status`, artifact upload, keepalive. Update the workflow_dispatch input to pass `--extract-max-docs`.
- [ ] **Step 3:** Docs: README quickstart becomes `uv tool install` + `doctor` + `pulse`; CLAUDE.md command lists updated (`--json` → `-o json`, new verbs, exit-code table); runbook: add the read-only role SQL (`CREATE ROLE jobhunter_ro ...; GRANT USAGE ON SCHEMA jobhunter TO jobhunter_ro; GRANT SELECT ON ALL TABLES IN SCHEMA jobhunter TO jobhunter_ro; ALTER DEFAULT PRIVILEGES IN SCHEMA jobhunter GRANT SELECT ON TABLES TO jobhunter_ro;`) and the agent-machine env recipe; `docs/README.md` gains one line pointing at the spec as current for the CLI surface.
- [ ] **Step 4:** `grep -rn '"--json"\|--json' src tests .github README.md | grep -v '\-o json'` returns nothing; full `uv run pytest -q && uv run ruff check . && uv run mypy` — PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(cli)!: drop report and --json; CI runs sync; docs on the new contract"`

---

### Task 12: Integration proof + PR

- [ ] **Step 1:** Extend `tests/integration/test_three_days.py`: after the existing three-day ingest, run `pulse` (CliRunner, tmp `JOB_HUNTER_STATE_DIR`) three times interleaved with the days: day-1 pulse reports the opens and `first_run`; day-2 pulse reports only day-2 deltas (changed/closed incl. an interval-censored close with both bounds); day-3 empty delta → `data["events"] == []`; a `--peek` between them changes nothing.
- [ ] **Step 2:** `docker compose up -d postgres && uv run pytest -q && uv run ruff check . && uv run mypy` — full suite green; paste the tail of the output into the PR body.
- [ ] **Step 3:** Manual smoke against the real corpus (needs the R2/Neon env exported): `job-hunter doctor`, `job-hunter pulse --cursor smoketest --peek -o json | head -c 2000`, `job-hunter q postings --search anthropic --limit 5 -o json`. Record outputs.
- [ ] **Step 4:** Push and open the PR:

```bash
git push -u origin cli/agentic-rework
gh pr create --base main --title "CLI rework: agent-first contract, pulse, q namespace, sync" \
  --body "Implements docs/superpowers/specs/2026-09-01-agentic-cli-rework-design.md ..."
```

- [ ] **Step 5:** After merge: create the hourly Claude schedule per spec §8 (separate follow-up with the user — the routine prompt embeds the interest sketch; not part of this repo change).

## Execution notes

- Tasks 1→2→3 are strictly sequential. Task 4 depends only on Task 1 landing (test idioms), Tasks 5–6 on 3+4, Task 7 on 5, Task 8–10 on 3, Task 11 last before 12.
- Store/integration tests need local Postgres (`docker compose up -d postgres`). If docker is unavailable in the executing environment, run the rest of the suite, mark store tests as not-run in the commit body, and Task 12 MUST run them before the PR opens.
