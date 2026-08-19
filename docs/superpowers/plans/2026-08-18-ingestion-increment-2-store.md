# Ingestion Increment 2 — Postgres Store, L0, Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the archive increment 1 is collecting into the Postgres temporal store: version identities, canonical Markdown documents, presence intervals, posting lifecycle with the drop guard and interval-censored closes, `ingest`/`rebuild`/`report`, and a rebuild that reproduces the incremental store row for row.

**Architecture:** `store/db.py` owns the connection, schema (`jobhunter`), advisory lock and schema swap; `store/lifecycle.py` owns the one write path `Ingestor.ingest(manifest)` (spec §5.4) fed only by archive manifests; `hashing.version_hash` and `markdown.to_markdown` are pure functions the ingestor calls; `fetch.run` gains "connect → lock → archive → ingest new manifests"; the CLI gains `ingest`, `rebuild`, `report`, `registry list`, `db init|version`. Tests run against a real Postgres (Docker locally, a service container in CI) with one throwaway schema per test.

**Tech Stack:** Python 3.12, uv, psycopg 3 (`psycopg[binary]`), Postgres 17 (Neon in production, `postgres:17` container in dev/CI), httpx, boto3, typer; pytest, ruff, mypy.

**Spec:** `docs/2026-08-18-ingestion-layer-spec.md` — this plan implements §3.7–3.10, §4 steps 1/4/5, §5.1 (posting/version/document identities), §5.3–5.6, §6.2 (`ingest`, `rebuild`, `report`, `registry list`, `db`), §6.3 (Neon), §9 (store + integration tiers), §10 item 2. Read it first.

## Global Constraints

- Everything in increment 1's Global Constraints still applies (uv only; `--json` on every command; exit `0`/`2`; manifests/blobs immutable; no network in tests).
- New runtime dependency: `psycopg[binary]>=3.2`. No others. No ORM.
- All tables live in Postgres schema **`jobhunter`** (never `public`); every connection sets `search_path = jobhunter, public`. `rebuild` builds `jobhunter_new` and swaps.
- Provenance tables (`fetch_attempts`, `posting_versions`, `documents`) are written only with `INSERT … ON CONFLICT DO NOTHING`; never `UPDATE`/`DELETE`. `presence` is append-mostly (only the open interval's tail is updated). Everything else is regenerable.
- Identities: `uid = {gh|lv|ab}:{board}:{source_id}`; `version_hash` = sha256 of canonical JSON of the §5.1 field list, `VERSION_HASH_V = 1`; `document_hash = sha256(markdown utf-8)`; `NORMALIZER_VERSION = "md/1"`.
- The ingest of one attempt is one transaction. `ingest` is idempotent per `attempt_id`; an attempt older than `schema_meta.last_ingested_at` raises `OutOfOrder`.
- Health verdict: `prev` = latest attempt on the same board with `health != 'error'`; `suspect_drop` iff `prev` exists and `observed_count < drop_ratio × prev.observed_count` (default `0.5`, env `JOB_HUNTER_DROP_RATIO`). Reconcile only when `health = 'ok'`.
- Timestamps are `TIMESTAMPTZ` UTC; the observation instant of an attempt is its `started_at`.
- Single writer: `pg_try_advisory_lock(0x6a6f6268)` (`"jobh"`) at the start of `fetch`, `ingest`, `rebuild`; if held, exit `0` with "already running".
- Tests needing Postgres read `JOB_HUNTER_TEST_DATABASE_URL` (default `postgresql://jobhunter:jobhunter@localhost:5432/jobhunter`, matching `compose.yaml`) and create a fresh schema per test; if the server is unreachable they **fail** with an instruction, never skip.
- Commit after every task with the message given.

---

## File structure (created or modified by this plan)

```text
pyproject.toml                          + psycopg[binary]
compose.yaml                            + postgres:17 service
.github/workflows/test.yml              + postgres service container + env
.github/workflows/fetch.yml             + JOB_HUNTER_DATABASE_URL secret on fetch/status
src/jobhunter/config.py                 + drop_ratio; database_url required by store commands
src/jobhunter/hashing.py                + VERSION_HASH_V, version_fields, version_hash
src/jobhunter/markdown.py               L0 converter (new)
src/jobhunter/store/__init__.py
src/jobhunter/store/schema.sql          DDL (spec §5.3, in schema jobhunter)
src/jobhunter/store/db.py               connect, init, schema helpers, advisory lock, swap_schema
src/jobhunter/store/lifecycle.py        Ingestor.ingest(manifest), OutOfOrder, AttemptResult
src/jobhunter/store/panel.py            apply_registry_snapshot(conn, boards, at, revision)
src/jobhunter/store/queries.py          read helpers for report / registry list / status
src/jobhunter/rebuild.py                rebuild(settings, store, dsn) -> RebuildSummary
src/jobhunter/fetch.py                  + DB connect/lock/ingest, db_error in summary
src/jobhunter/cli.py                    + ingest, rebuild, report, registry list, db init|version
tests/conftest.py                       + pg fixture (fresh schema per test), helpers
tests/store/__init__.py  test_db.py  test_lifecycle.py  test_panel.py  test_queries.py
tests/test_hashing.py                   + version_hash tests
tests/test_markdown.py                  goldens + properties
tests/fixtures/md/*.md                  golden Markdown for 5 fixture postings
tests/test_fetch.py                     + DB integration of run()
tests/test_rebuild.py
tests/test_cli.py                       + new commands
tests/integration/__init__.py  test_three_days.py   full scenario, rebuild == incremental
docs/runbooks/2026-08-18-deploy-fetcher.md   + Neon steps
```

---

### Task 1: Postgres plumbing — dependency, compose, CI service, `store/db.py`, `db init|version`

**Files:**
- Modify: `pyproject.toml`, `compose.yaml`, `.github/workflows/test.yml`, `src/jobhunter/config.py`, `src/jobhunter/cli.py`, `tests/conftest.py`, `tests/test_config.py`
- Create: `src/jobhunter/store/__init__.py`, `src/jobhunter/store/schema.sql`, `src/jobhunter/store/db.py`, `tests/store/__init__.py`, `tests/store/test_db.py`

**Interfaces:**
- Produces:
  - `config.Settings` gains `drop_ratio: float` (env `JOB_HUNTER_DROP_RATIO`, default `0.5`; `ConfigError` if not a float in `(0, 1]`); `Settings.require_database_url() -> str` (raises `ConfigError` naming `JOB_HUNTER_DATABASE_URL`).
  - `store.db.SCHEMA = "jobhunter"`, `store.db.SCHEMA_VERSION = "1"`, `store.db.LOCK_KEY = 0x6A6F6268`.
  - `store.db.connect(dsn: str, *, schema: str = SCHEMA) -> psycopg.Connection[dict[str, Any]]` — `autocommit=False`, `row_factory=dict_row`, sets `search_path` to `schema, public`.
  - `store.db.init(conn, schema: str = SCHEMA) -> None` — `CREATE SCHEMA IF NOT EXISTS`, executes `schema.sql` inside it, inserts `schema_meta(schema_version)` if absent. Idempotent.
  - `store.db.schema_exists(conn, schema) -> bool`; `store.db.stored_schema_version(conn) -> str | None`.
  - `store.db.try_lock(conn) -> bool`; `store.db.unlock(conn) -> None`.
  - `store.db.swap_schema(conn, new: str, target: str = SCHEMA, previous: str = "jobhunter_previous") -> None` — drops `previous` if it exists, renames `target`→`previous` (if it exists), `new`→`target`; one transaction.
  - `store.db.get_meta(conn, key) -> str | None`; `store.db.set_meta(conn, key, value) -> None` (upsert).
  - CLI: `db init` (creates schema; prints versions), `db version` (prints `code`, `db` versions; exit 2 on mismatch).
  - `tests/conftest.py`: `pg` fixture yielding a connection whose `search_path` is a fresh schema `t_<hex>` with the DDL applied; drops it afterwards. `TEST_DSN` constant.

- [ ] **Step 1: Dependency, compose, CI**

`pyproject.toml` — add to `dependencies`: `"psycopg[binary]>=3.2",`. Then `uv sync`.

`compose.yaml` — add a service (keep the existing MinIO services):

```yaml
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: jobhunter
      POSTGRES_PASSWORD: jobhunter
      POSTGRES_DB: jobhunter
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U jobhunter -d jobhunter"]
      interval: 2s
      timeout: 5s
      retries: 30
    volumes: ["pg-data:/var/lib/postgresql/data"]
```

and add `pg-data: {}` under `volumes:`.

`.github/workflows/test.yml` — inside `jobs.test`, add before `steps:`:

```yaml
    services:
      postgres:
        image: postgres:17
        env:
          POSTGRES_USER: jobhunter
          POSTGRES_PASSWORD: jobhunter
          POSTGRES_DB: jobhunter
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U jobhunter -d jobhunter"
          --health-interval 5s --health-timeout 5s --health-retries 10
    env:
      JOB_HUNTER_TEST_DATABASE_URL: postgresql://jobhunter:jobhunter@localhost:5432/jobhunter
```

Locally: `docker compose up -d postgres` before running the suite.

- [ ] **Step 2: Failing tests**

`tests/test_config.py` — append:

```python
def test_drop_ratio_default_and_override(tmp_path: Path) -> None:
    base = {"JOB_HUNTER_ARCHIVE_URL": f"file://{tmp_path}"}
    assert Settings.load(base).drop_ratio == 0.5
    assert Settings.load({**base, "JOB_HUNTER_DROP_RATIO": "0.8"}).drop_ratio == 0.8
    with pytest.raises(ConfigError, match="JOB_HUNTER_DROP_RATIO"):
        Settings.load({**base, "JOB_HUNTER_DROP_RATIO": "2"})


def test_require_database_url(tmp_path: Path) -> None:
    s = Settings.load({"JOB_HUNTER_ARCHIVE_URL": f"file://{tmp_path}"})
    with pytest.raises(ConfigError, match="JOB_HUNTER_DATABASE_URL"):
        s.require_database_url()
    s2 = Settings.load({"JOB_HUNTER_ARCHIVE_URL": f"file://{tmp_path}",
                        "JOB_HUNTER_DATABASE_URL": "postgresql://x"})
    assert s2.require_database_url() == "postgresql://x"
```

`tests/conftest.py` — append:

```python
import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg

TEST_DSN = os.environ.get(
    "JOB_HUNTER_TEST_DATABASE_URL",
    "postgresql://jobhunter:jobhunter@localhost:5432/jobhunter",
)


@pytest.fixture
def pg() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """A connection whose search_path is a fresh schema with the DDL applied."""
    from jobhunter.store import db

    schema = f"t_{uuid.uuid4().hex[:10]}"
    try:
        conn = db.connect(TEST_DSN, schema=schema)
    except psycopg.OperationalError as e:  # pragma: no cover - environment guidance
        pytest.fail(
            f"Postgres not reachable at {TEST_DSN}: {e}\n"
            "Start it with `docker compose up -d postgres` or set JOB_HUNTER_TEST_DATABASE_URL."
        )
    db.init(conn, schema)
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()
```

`tests/store/__init__.py`: empty.

`tests/store/test_db.py`:

```python
from typing import Any

import psycopg

from jobhunter.store import db
from tests.conftest import TEST_DSN

EXPECTED_TABLES = {
    "fetch_attempts", "posting_versions", "documents", "presence", "runs", "panel",
    "postings", "posting_events", "schema_meta",
}


def _tables(conn: psycopg.Connection[dict[str, Any]], schema: str) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)
    ).fetchall()
    return {r["table_name"] for r in rows}


def _schema_of(conn: psycopg.Connection[dict[str, Any]]) -> str:
    return str(conn.execute("SELECT current_schema()").fetchone()["current_schema"])


def test_init_creates_all_tables_and_version(pg: psycopg.Connection[dict[str, Any]]) -> None:
    schema = _schema_of(pg)
    assert _tables(pg, schema) == EXPECTED_TABLES
    assert db.stored_schema_version(pg) == db.SCHEMA_VERSION


def test_init_is_idempotent(pg: psycopg.Connection[dict[str, Any]]) -> None:
    schema = _schema_of(pg)
    db.init(pg, schema)
    pg.commit()
    assert _tables(pg, schema) == EXPECTED_TABLES


def test_meta_roundtrip(pg: psycopg.Connection[dict[str, Any]]) -> None:
    assert db.get_meta(pg, "nope") is None
    db.set_meta(pg, "k", "1")
    db.set_meta(pg, "k", "2")
    assert db.get_meta(pg, "k") == "2"


def test_advisory_lock_is_exclusive_across_connections(pg: psycopg.Connection[dict[str, Any]]) -> None:
    other = db.connect(TEST_DSN, schema=_schema_of(pg))
    try:
        assert db.try_lock(pg) is True
        assert db.try_lock(other) is False
        db.unlock(pg)
        assert db.try_lock(other) is True
        db.unlock(other)
    finally:
        other.close()


def test_swap_schema(pg: psycopg.Connection[dict[str, Any]]) -> None:
    target = _schema_of(pg)
    new, prev = f"{target}_new", f"{target}_previous"
    db.init(pg, new)
    pg.execute(f'INSERT INTO "{new}".schema_meta (key, value) VALUES (%s, %s)', ("marker", "new"))
    pg.commit()
    db.swap_schema(pg, new=new, target=target, previous=prev)
    pg.commit()
    assert db.schema_exists(pg, target) and db.schema_exists(pg, prev)
    assert not db.schema_exists(pg, new)
    row = pg.execute(
        f'SELECT value FROM "{target}".schema_meta WHERE key = %s', ("marker",)
    ).fetchone()
    assert row is not None and row["value"] == "new"
    pg.execute(f'DROP SCHEMA "{prev}" CASCADE')
    pg.commit()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `docker compose up -d postgres && uv run pytest tests/store tests/test_config.py -q`
Expected: `ModuleNotFoundError: jobhunter.store` / attribute errors on `Settings`.

- [ ] **Step 4: Implement**

`src/jobhunter/config.py` — replace the dataclass with:

```python
@dataclass(frozen=True, slots=True)
class Settings:
    archive_url: str
    registry_path: Path
    home: Path
    database_url: str | None
    drop_ratio: float = 0.5

    def require_database_url(self) -> str:
        if not self.database_url:
            raise ConfigError("JOB_HUNTER_DATABASE_URL is required for this command (Postgres DSN)")
        return self.database_url

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        archive_url = e.get("JOB_HUNTER_ARCHIVE_URL")
        if not archive_url:
            raise ConfigError(
                "JOB_HUNTER_ARCHIVE_URL is required (s3://bucket/prefix or file:///path)"
            )
        home_default = Path(e.get("HOME", "~")).expanduser() / ".local/share/job-hunter"
        raw_ratio = e.get("JOB_HUNTER_DROP_RATIO", "0.5")
        try:
            drop_ratio = float(raw_ratio)
        except ValueError as ex:
            raise ConfigError(f"JOB_HUNTER_DROP_RATIO must be a number, got {raw_ratio!r}") from ex
        if not 0 < drop_ratio <= 1:
            raise ConfigError(f"JOB_HUNTER_DROP_RATIO must be in (0, 1], got {drop_ratio}")
        return cls(
            archive_url=archive_url,
            registry_path=Path(e.get("JOB_HUNTER_REGISTRY", "companies.toml")),
            home=Path(e["JOB_HUNTER_HOME"]) if e.get("JOB_HUNTER_HOME") else home_default,
            database_url=e.get("JOB_HUNTER_DATABASE_URL") or None,
            drop_ratio=drop_ratio,
        )
```

`src/jobhunter/store/__init__.py`: empty docstring module `"""Postgres temporal store."""`.

`src/jobhunter/store/schema.sql` — the spec §5.3 DDL verbatim, unqualified names (they land in the schema selected by `search_path`), plus `IF NOT EXISTS` on every `CREATE TABLE`/`CREATE INDEX` so `init` is idempotent:

```sql
-- provenance --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_attempts (
  attempt_id        TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL,
  source            TEXT NOT NULL,
  board             TEXT NOT NULL,
  started_at        TIMESTAMPTZ NOT NULL,
  finished_at       TIMESTAMPTZ NOT NULL,
  http_status       INTEGER,
  transport         TEXT NOT NULL,
  health            TEXT NOT NULL,
  blob_sha256       TEXT,
  payload_bytes     INTEGER,
  observed_count    INTEGER NOT NULL DEFAULT 0,
  parsed_count      INTEGER NOT NULL DEFAULT 0,
  failed_count      INTEGER NOT NULL DEFAULT 0,
  unidentifiable_count INTEGER NOT NULL DEFAULT 0,
  prev_observed_count INTEGER,
  adapter_version   TEXT NOT NULL,
  registry_revision TEXT NOT NULL,
  cli_version       TEXT NOT NULL,
  warnings          JSONB,
  error             TEXT
);
CREATE INDEX IF NOT EXISTS ix_attempts_board_time ON fetch_attempts (source, board, started_at);

CREATE TABLE IF NOT EXISTS posting_versions (
  version_hash      TEXT PRIMARY KEY,
  version_hash_v    INTEGER NOT NULL,
  uid               TEXT NOT NULL,
  source            TEXT NOT NULL,
  board             TEXT NOT NULL,
  source_id         TEXT NOT NULL,
  title             TEXT NOT NULL,
  company           TEXT NOT NULL,
  locations         JSONB NOT NULL,
  workplace_type    TEXT,
  is_remote         BOOLEAN,
  department        TEXT,
  team              TEXT,
  employment_type   TEXT,
  compensation      JSONB,
  url               TEXT,
  apply_url         TEXT,
  source_created_at TIMESTAMPTZ,
  first_seen_attempt TEXT NOT NULL REFERENCES fetch_attempts (attempt_id)
);
CREATE INDEX IF NOT EXISTS ix_versions_uid ON posting_versions (uid);

CREATE TABLE IF NOT EXISTS documents (
  document_hash      TEXT PRIMARY KEY,
  version_hash       TEXT NOT NULL REFERENCES posting_versions (version_hash),
  normalizer_version TEXT NOT NULL,
  markdown           TEXT NOT NULL,
  UNIQUE (version_hash, normalizer_version)
);

-- derived -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS presence (
  uid            TEXT NOT NULL,
  version_hash   TEXT,
  parse_status   TEXT NOT NULL,
  first_attempt  TEXT NOT NULL,
  last_attempt   TEXT NOT NULL,
  first_at       TIMESTAMPTZ NOT NULL,
  last_at        TIMESTAMPTZ NOT NULL,
  runs           INTEGER NOT NULL,
  PRIMARY KEY (uid, first_attempt)
);
CREATE INDEX IF NOT EXISTS ix_presence_last ON presence (last_attempt);
CREATE INDEX IF NOT EXISTS ix_presence_uid_last ON presence (uid, last_at DESC);

CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT PRIMARY KEY,
  started_at     TIMESTAMPTZ NOT NULL,
  finished_at    TIMESTAMPTZ NOT NULL,
  cli_version    TEXT NOT NULL,
  boards_total   INTEGER NOT NULL,
  boards_ok      INTEGER NOT NULL,
  boards_suspect INTEGER NOT NULL,
  boards_error   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS panel (
  source            TEXT NOT NULL,
  board             TEXT NOT NULL,
  company           TEXT NOT NULL,
  added_at          TIMESTAMPTZ NOT NULL,
  removed_at        TIMESTAMPTZ,
  registry_revision TEXT NOT NULL,
  PRIMARY KEY (source, board, added_at)
);

CREATE TABLE IF NOT EXISTS postings (
  uid                  TEXT PRIMARY KEY,
  source               TEXT NOT NULL,
  board                TEXT NOT NULL,
  source_id            TEXT NOT NULL,
  status               TEXT NOT NULL,
  current_version_hash TEXT,
  version_count        INTEGER NOT NULL DEFAULT 0,
  reopen_count         INTEGER NOT NULL DEFAULT 0,
  first_seen_attempt   TEXT NOT NULL,
  first_seen_at        TIMESTAMPTZ NOT NULL,
  last_seen_attempt    TEXT NOT NULL,
  last_seen_at         TIMESTAMPTZ NOT NULL,
  closed_lower_at      TIMESTAMPTZ,
  closed_upper_at      TIMESTAMPTZ,
  closed_by_attempt    TEXT,
  source_updated_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_postings_board_status ON postings (source, board, status);

CREATE TABLE IF NOT EXISTS posting_events (
  event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  uid             TEXT NOT NULL,
  kind            TEXT NOT NULL,
  attempt_id      TEXT NOT NULL,
  at              TIMESTAMPTZ NOT NULL,
  from_version    TEXT,
  to_version      TEXT,
  closed_lower_at TIMESTAMPTZ,
  closed_upper_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_events_uid ON posting_events (uid, event_id);
CREATE INDEX IF NOT EXISTS ix_events_time ON posting_events (at);

CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

`src/jobhunter/store/db.py`:

```python
"""Connection, schema lifecycle, advisory lock, schema swap. No business logic."""

from __future__ import annotations

from importlib import resources
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

SCHEMA = "jobhunter"
SCHEMA_VERSION = "1"
LOCK_KEY = 0x6A6F6268  # "jobh"

Conn = psycopg.Connection[dict[str, Any]]


def connect(dsn: str, *, schema: str = SCHEMA) -> Conn:
    conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    conn.commit()
    return conn


def load_schema_sql() -> str:
    return resources.files("jobhunter.store").joinpath("schema.sql").read_text(encoding="utf-8")


def schema_exists(conn: Conn, schema: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,)
    ).fetchone()
    return row is not None


def init(conn: Conn, schema: str = SCHEMA) -> None:
    """Create the schema and all tables if absent; record schema_version. Idempotent."""
    with conn.transaction():
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute(load_schema_sql())
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', %s) "
            "ON CONFLICT (key) DO NOTHING",
            (SCHEMA_VERSION,),
        )


def stored_schema_version(conn: Conn) -> str | None:
    return get_meta(conn, "schema_version")


def get_meta(conn: Conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = %s", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(conn: Conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


def try_lock(conn: Conn, key: int = LOCK_KEY) -> bool:
    row = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,)).fetchone()
    conn.commit()
    return bool(row is not None and row["ok"])


def unlock(conn: Conn, key: int = LOCK_KEY) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
    conn.commit()


def swap_schema(conn: Conn, new: str, target: str = SCHEMA, previous: str = "jobhunter_previous") -> None:
    """Atomically make `new` the live schema; the old live schema becomes `previous`."""
    with conn.transaction():
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(previous)))
        if schema_exists(conn, target):
            conn.execute(
                sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                    sql.Identifier(target), sql.Identifier(previous)
                )
            )
        conn.execute(
            sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(sql.Identifier(new), sql.Identifier(target))
        )
```

Also add to `pyproject.toml` under `[tool.hatch.build.targets.wheel]` nothing extra (the `.sql` file is inside the package directory and hatchling includes it), but add `[tool.hatch.build] include = ["src/jobhunter/**/*.sql", "src/jobhunter/**/*.py"]` only if `uv run python -c "from jobhunter.store.db import load_schema_sql; load_schema_sql()"` fails after `uv sync`.

CLI (`src/jobhunter/cli.py`) — add a `db` sub-app:

```python
db_app = typer.Typer(help="Postgres store")
app.add_typer(db_app, name="db")


def _conn(settings: Settings) -> Any:
    from jobhunter.store import db as _db

    try:
        return _db.connect(settings.require_database_url())
    except ConfigError as e:
        typer.echo(f"config error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    except Exception as e:  # psycopg.OperationalError and friends
        typer.echo(f"database error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e


@db_app.command("init")
def db_init(as_json: bool = typer.Option(False, "--json")) -> None:
    """Create the jobhunter schema and tables (idempotent)."""
    from jobhunter.store import db as _db

    settings = _settings()
    conn = _conn(settings)
    try:
        _db.init(conn)
        conn.commit()
        payload = {"schema": _db.SCHEMA, "schema_version": _db.stored_schema_version(conn)}
    finally:
        conn.close()
    _emit(payload, as_json, f"schema {payload['schema']} ready, version {payload['schema_version']}")


@db_app.command("version")
def db_version(as_json: bool = typer.Option(False, "--json")) -> None:
    """Print the code's schema version and the database's; exit 2 on mismatch."""
    from jobhunter.store import db as _db

    settings = _settings()
    conn = _conn(settings)
    try:
        stored = _db.stored_schema_version(conn) if _db.schema_exists(conn, _db.SCHEMA) else None
    finally:
        conn.close()
    payload = {"code": _db.SCHEMA_VERSION, "db": stored, "match": stored == _db.SCHEMA_VERSION}
    _emit(payload, as_json, f"code {payload['code']}  db {stored or 'absent'}")
    if not payload["match"]:
        raise typer.Exit(EXIT_SYSTEMIC)
```

- [ ] **Step 5: Run tests, lint, types; commit**

Run: `uv run pytest tests/store tests/test_config.py -q && uv run ruff check . && uv run mypy && uv run job-hunter db --help`
Expected: pass; `db init` and `db version` listed.

```bash
git add pyproject.toml uv.lock compose.yaml .github/workflows/test.yml src/jobhunter/config.py src/jobhunter/cli.py src/jobhunter/store tests/conftest.py tests/store tests/test_config.py
git commit -m "feat(store): psycopg dependency, Postgres test service, schema DDL, db.connect/init/lock/swap, db init|version"
```

---

### Task 2: `version_hash` v1

**Files:**
- Modify: `src/jobhunter/hashing.py`
- Test: `tests/test_hashing.py`

**Interfaces:**
- Produces: `hashing.VERSION_HASH_V = 1`; `hashing.version_fields(pv: PostingVersion) -> dict[str, Any]` (the exact §5.1 preparation); `hashing.version_hash(pv) -> str`.

- [ ] **Step 1: Failing tests** — append to `tests/test_hashing.py`:

```python
from dataclasses import replace
from datetime import UTC, datetime

from jobhunter.hashing import VERSION_HASH_V, version_fields, version_hash
from jobhunter.models import Compensation, PostingVersion


def _pv(**over: object) -> PostingVersion:
    base = PostingVersion(
        source="ashby", board="ramp", source_id="1", title="  Engineer ", company="Ramp",
        locations=("NYC", "Remote", "NYC"), workplace_type="Hybrid", is_remote=True,
        department="Eng", team=None, employment_type="full_time",
        compensation=Compensation(100.0, 200.0, "USD", "year"),
        url="https://a", apply_url="https://b",
        source_created_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_updated_at=datetime(2026, 2, 2, tzinfo=UTC),
        description_html="<p>Hello   \n world</p>",
    )
    return replace(base, **over)  # type: ignore[arg-type]


def test_version_fields_preparation() -> None:
    f = version_fields(_pv())
    assert f == {
        "title": "Engineer",
        "locations": ["NYC", "Remote"],
        "workplace_type": "hybrid",
        "is_remote": True,
        "department": "Eng",
        "team": None,
        "employment_type": "full_time",
        "compensation": {"min": 100.0, "max": 200.0, "currency": "USD", "interval": "year"},
        "description_html": "<p>Hello world</p>",
    }


def test_version_hash_is_stable_golden() -> None:
    assert VERSION_HASH_V == 1
    assert version_hash(_pv()) == version_hash(_pv())
    assert len(version_hash(_pv())) == 64
    # Golden: pin the value so an accidental change to the field list is caught.
    assert version_hash(_pv()) == "GOLDEN"  # replace with the value printed in Step 3


def test_excluded_fields_do_not_change_hash() -> None:
    h = version_hash(_pv())
    assert version_hash(_pv(url="https://z", apply_url=None)) == h
    assert version_hash(_pv(company="Other")) == h
    assert version_hash(_pv(source_updated_at=datetime(2030, 1, 1, tzinfo=UTC))) == h
    assert version_hash(_pv(source_created_at=None)) == h
    assert version_hash(_pv(locations=("Remote", "NYC"))) == h  # order-insensitive


def test_included_fields_change_hash() -> None:
    h = version_hash(_pv())
    assert version_hash(_pv(title="Engineer II")) != h
    assert version_hash(_pv(description_html="<p>Hello world!</p>")) != h
    assert version_hash(_pv(compensation=None)) != h
    assert version_hash(_pv(is_remote=None)) != h
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_hashing.py -q` — expected: `ImportError` for `version_hash`.

- [ ] **Step 3: Implement** — append to `src/jobhunter/hashing.py`:

```python
import re
from dataclasses import asdict

from jobhunter.models import PostingVersion

VERSION_HASH_V = 1
_WS = re.compile(r"\s+")


def version_fields(pv: PostingVersion) -> dict[str, Any]:
    """The employer-visible fields that define a posting version (spec §5.1), prepared."""
    return {
        "title": pv.title.strip(),
        "locations": sorted({s.strip() for s in pv.locations if s and s.strip()}),
        "workplace_type": pv.workplace_type.strip().lower() if pv.workplace_type else None,
        "is_remote": pv.is_remote,
        "department": pv.department.strip() if pv.department else None,
        "team": pv.team.strip() if pv.team else None,
        "employment_type": pv.employment_type.strip() if pv.employment_type else None,
        "compensation": asdict(pv.compensation) if pv.compensation else None,
        "description_html": _WS.sub(" ", pv.description_html).strip(),
    }


def version_hash(pv: PostingVersion) -> str:
    return sha256_hex(canonical_json(version_fields(pv)))
```

(Move the new imports to the top of the module; keep `canonical_json`/`sha256_hex` unchanged.) Then run `uv run python -c "from tests.test_hashing import _pv; from jobhunter.hashing import version_hash; print(version_hash(_pv()))"` and paste the printed value over `"GOLDEN"` in the test.

- [ ] **Step 4: Verify** — `uv run pytest tests/test_hashing.py -q && uv run ruff check . && uv run mypy` → pass.

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/hashing.py tests/test_hashing.py
git commit -m "feat: version_hash v1 over the spec field list with golden"
```

---

### Task 3: L0 — HTML → Markdown converter with goldens

**Files:**
- Create: `src/jobhunter/markdown.py`, `tests/test_markdown.py`, `tests/fixtures/md/{greenhouse_anthropic,ashby_ramp,lever_palantir,linkedin_notion,workday_nvidia}.md`

**Interfaces:**
- Produces: `markdown.NORMALIZER_VERSION = "md/1"`; `markdown.to_markdown(html: str) -> str`; `markdown.visible_text(html: str) -> str` (whitespace-collapsed text content, no script/style); `markdown.strip_markdown(md: str) -> str` (whitespace-collapsed text with markers removed) — the two used by the text-preservation property.

- [ ] **Step 1: Failing tests**

`tests/test_markdown.py`:

```python
import html
import json
from pathlib import Path

import pytest

from jobhunter.markdown import NORMALIZER_VERSION, strip_markdown, to_markdown, visible_text
from tests.conftest import FIXTURES

GOLDEN = FIXTURES / "md"


def _fixture_htmls() -> dict[str, str]:
    gh = json.loads((FIXTURES / "greenhouse_board.json").read_text())["jobs"][0]
    ab = json.loads((FIXTURES / "ashby_board.json").read_text())["jobs"][0]
    lv = json.loads((FIXTURES / "lever_board.json").read_text())[0]
    proto = Path(__file__).resolve().parents[1] / "prototypes" / "parsing" / "fixtures"
    notion = json.loads((proto / "linkedin_notion_early-career-ai.json").read_text())
    nvidia = json.loads((proto / "workday_nvidia_backend-compiler.json").read_text())
    from jobhunter.sources.lever import _description

    return {
        "greenhouse_anthropic": html.unescape(gh["content"]),
        "ashby_ramp": ab["descriptionHtml"],
        "lever_palantir": _description(lv),
        "linkedin_notion": notion["descriptionHtml"],
        "workday_nvidia": nvidia["descriptionHtml"],
    }


def test_normalizer_version() -> None:
    assert NORMALIZER_VERSION == "md/1"


@pytest.mark.parametrize(
    "src,expected",
    [
        ("<h2><strong>About</strong></h2><p>Hi <em>there</em>.</p>", "## **About**\n\nHi *there*."),
        ("<ul><li>a</li><li>b<ul><li>c</li></ul></li></ul>", "- a\n- b\n  - c"),
        ("<ol><li>x</li><li>y</li></ol>", "1. x\n2. y"),
        ("<p>See <a href=\"https://x\">here</a>.</p>", "See [here](https://x)."),
        ("<div><div><p>nested</p></div></div>", "nested"),
        ("<p>line<br>break</p>", "line\nbreak"),
        ("<p>a</p><hr><p>b</p>", "a\n\n---\n\nb"),
        ("<script>x()</script><p>only</p><style>p{}</style>", "only"),
        ("<p>ﬁ ①</p>", "fi 1"),  # NFKC
        ("<li><p>para in li</p><p>second</p></li>", "- para in li second"),
        ("<p>  many    spaces \n here </p>", "many spaces here"),
        ("<p></p><div></div><p>kept</p>", "kept"),
    ],
)
def test_small_cases(src: str, expected: str) -> None:
    assert to_markdown(src) == expected


def test_idempotent_whitespace_and_no_trailing() -> None:
    md = to_markdown("<p>a</p>\n\n\n<p>b</p>   ")
    assert md == "a\n\nb"
    assert not any(line != line.rstrip() for line in md.splitlines())


@pytest.mark.parametrize("name", sorted(_fixture_htmls()))
def test_goldens(name: str) -> None:
    md = to_markdown(_fixture_htmls()[name])
    golden = (GOLDEN / f"{name}.md").read_text(encoding="utf-8")
    assert md == golden, f"golden drift for {name}; regenerate deliberately if md/1 changed"


@pytest.mark.parametrize("name", sorted(_fixture_htmls()))
def test_text_is_preserved(name: str) -> None:
    src = _fixture_htmls()[name]
    assert strip_markdown(to_markdown(src)) == visible_text(src)


@pytest.mark.parametrize("name", sorted(_fixture_htmls()))
def test_structure_survives(name: str) -> None:
    md = to_markdown(_fixture_htmls()[name])
    assert "\n- " in md or "\n1. " in md or md.startswith("- ")  # every posting has lists
    assert "<" not in md.replace("<=", "")  # no leftover tags
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_markdown.py -q` — expected: `ModuleNotFoundError: jobhunter.markdown`.

- [ ] **Step 3: Implement**

`src/jobhunter/markdown.py`:

```python
"""L0: HTML -> Markdown, the only canonical text. Deterministic; versioned as NORMALIZER_VERSION.

Handles the dialect ATS postings use: headings, paragraphs, div wrappers, nested ul/ol,
bold/italic, links, br, hr, blockquote. Drops script/style/img. NFKC-normalises. The visible
text of the output equals the visible text of the input (tested).
"""

from __future__ import annotations

import html as _html
import re
import unicodedata
from html.parser import HTMLParser

NORMALIZER_VERSION = "md/1"

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_BOUNDARY = {"p", "div", "section", "article", "header", "footer", "main", "aside",
                   "table", "tr", "thead", "tbody", "blockquote", "pre", "figure", "nav"}
_SKIP = {"script", "style", "noscript", "template", "head", "title"}
_WS = re.compile(r"\s+")


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.buf: list[str] = []
        self.lists: list[list[object]] = []  # each: [kind, counter]
        self.li_depth = 0
        self.heading: int | None = None
        self.skip = 0
        self.links: list[str | None] = []
        self.quote = 0

    # -- helpers
    def _flush(self) -> None:
        text = "".join(self.buf)
        self.buf = []
        text = _WS.sub(" ", text).strip()
        text = text.replace(" \n ", "\n").replace("\n ", "\n").replace(" \n", "\n")
        if not text:
            self.heading = None
            return
        if self.heading:
            self.blocks.append("#" * self.heading + " " + text)
            self.heading = None
        elif self.li_depth:
            kind, n = self.lists[-1]
            indent = "  " * (len(self.lists) - 1)
            marker = "- " if kind == "ul" else f"{n}. "
            self.blocks.append(indent + marker + text)
        elif self.quote:
            self.blocks.append("\n".join("> " + ln for ln in text.split("\n")))
        else:
            self.blocks.append(text)

    # -- parser callbacks
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in _HEADINGS:
            self._flush()
            self.heading = _HEADINGS[tag]
        elif tag in ("ul", "ol"):
            self._flush()
            self.lists.append([tag, 0])
        elif tag == "li":
            self._flush()
            if not self.lists:
                self.lists.append(["ul", 0])
            self.lists[-1][1] = int(self.lists[-1][1]) + 1  # type: ignore[call-overload]
            self.li_depth += 1
        elif tag == "br":
            self.buf.append("\n")
        elif tag == "hr":
            self._flush()
            self.blocks.append("---")
        elif tag in ("strong", "b"):
            self.buf.append("**")
        elif tag in ("em", "i"):
            self.buf.append("*")
        elif tag == "a":
            href = dict(attrs).get("href")
            self.links.append(href)
            self.buf.append("[")
        elif tag == "blockquote":
            self._flush()
            self.quote += 1
        elif tag in _BLOCK_BOUNDARY:
            if self.li_depth:
                self.buf.append(" ")  # paragraphs inside a list item stay on the item's line
            else:
                self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in _HEADINGS:
            self._flush()
        elif tag == "li":
            self._flush()
            self.li_depth = max(0, self.li_depth - 1)
        elif tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()
        elif tag in ("strong", "b"):
            self.buf.append("**")
        elif tag in ("em", "i"):
            self.buf.append("*")
        elif tag == "a":
            href = self.links.pop() if self.links else None
            self.buf.append(f"]({href})" if href else "]")
        elif tag == "blockquote":
            self._flush()
            self.quote = max(0, self.quote - 1)
        elif tag in _BLOCK_BOUNDARY:
            if self.li_depth:
                self.buf.append(" ")
            else:
                self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        self.buf.append(data)

    def result(self) -> str:
        self._flush()
        return "\n\n".join(self.blocks)


def _tidy(md: str) -> str:
    md = unicodedata.normalize("NFKC", md)
    # empty emphasis produced by empty tags
    md = md.replace("****", "").replace("**  **", "").replace("* *", "")
    lines = [ln.rstrip() for ln in md.split("\n")]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    # consecutive list items are separated by single newlines, not blank lines
    out = re.sub(r"\n\n(?=(?: {2})*(?:- |\d+\. ))", "\n", out)
    out = re.sub(r"(?<=\n> [^\n]{0,500})\n\n(?=> )", "\n", out)
    return out.strip()


def to_markdown(html: str) -> str:
    conv = _Converter()
    conv.feed(html)
    conv.close()
    return _tidy(conv.result())


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK_BOUNDARY or tag in _HEADINGS or tag in ("li", "br", "ul", "ol", "hr"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        else:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def visible_text(html: str) -> str:
    p = _TextOnly()
    p.feed(html)
    p.close()
    return _WS.sub(" ", unicodedata.normalize("NFKC", "".join(p.parts))).strip()


_MD_MARK = re.compile(
    r"^(?:#{1,6} |(?: {2})*(?:- |\d+\. )|> )|"  # line-leading structure
    r"\*\*|(?<!\w)\*(?=\S)|(?<=\S)\*(?!\w)|"      # emphasis toggles
    r"^---$",
    re.M,
)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def strip_markdown(md: str) -> str:
    text = _MD_LINK.sub(r"\1", md)
    text = _MD_MARK.sub("", text)
    return _WS.sub(" ", text).strip()
```

Then generate the goldens (they are the *result* of the converter — check them by eye before committing; the checks below are what "by eye" means):

```bash
mkdir -p tests/fixtures/md && uv run python - <<'PY'
from pathlib import Path
from tests.test_markdown import _fixture_htmls
from jobhunter.markdown import to_markdown
for name, src in _fixture_htmls().items():
    md = to_markdown(src)
    Path("tests/fixtures/md", f"{name}.md").write_text(md, encoding="utf-8")
    print("==", name, len(md), "chars"); print(md[:400]); print("...")
PY
```

Review each `tests/fixtures/md/*.md`: headings appear as `## …`; bullet lists render as `- ` lines with nesting where the source nests; bold lead-ins survive (`**Requirements:**`); no `<`/`>` tags; no runs of blank lines; the Anthropic file starts with `## **About Anthropic**` and the Ramp file with `# **About Ramp**`. If a case looks wrong (e.g. a list item glued to the previous paragraph), fix the converter, regenerate, and only then run the tests.

- [ ] **Step 4: Verify** — `uv run pytest tests/test_markdown.py -q && uv run ruff check . && uv run mypy` → pass. If a small-case expectation and the converter disagree on a *cosmetic* choice, prefer the expectation unless it violates text preservation; record the decision in the commit body.

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/markdown.py tests/test_markdown.py tests/fixtures/md
git commit -m "feat(L0): deterministic HTML->Markdown converter md/1 with goldens and text-preservation property"
```

---

### Task 4: Panel from registry snapshots

**Files:**
- Create: `src/jobhunter/store/panel.py`, `tests/store/test_panel.py`

**Interfaces:**
- Consumes: `registry.Registry.snapshot_json` format (list of `{board, company, country, source, tags}`), archive `registry/<revision>.json`.
- Produces:
  - `store.panel.boards_from_snapshot(data: bytes) -> tuple[Board, ...]`.
  - `store.panel.apply_snapshot(conn, boards: Iterable[Board], at: datetime, revision: str) -> PanelDelta(added: list[str], removed: list[str])` — boards without an open panel row get `added_at=at`; open rows not in `boards` get `removed_at=at`. Idempotent for the same set.
  - `store.panel.load_snapshot(store: ArchiveStore, revision: str) -> tuple[Board, ...]` (raises `KeyError` if absent).

- [ ] **Step 1: Failing tests** — `tests/store/test_panel.py`:

```python
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from jobhunter.models import Board
from jobhunter.registry import Registry
from jobhunter.store.panel import apply_snapshot, boards_from_snapshot

B1 = Board("Anthropic", "greenhouse", "anthropic")
B2 = Board("Ramp", "ashby", "ramp", country="US", tags=("fintech",))
B3 = Board("Palantir", "lever", "palantir")


def _rows(conn: psycopg.Connection[dict[str, Any]]) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT source, board, company, added_at, removed_at, registry_revision "
        "FROM panel ORDER BY source, board, added_at"
    ).fetchall()


def test_snapshot_roundtrip() -> None:
    reg = Registry(boards=(B2, B1), revision="r")
    assert boards_from_snapshot(reg.snapshot_json()) == (B2, B1)


def test_add_remove_readd(pg: psycopg.Connection[dict[str, Any]]) -> None:
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    d = apply_snapshot(pg, [B1, B2], t0, "rev1")
    assert sorted(d.added) == ["ashby:ramp", "greenhouse:anthropic"] and d.removed == []
    d = apply_snapshot(pg, [B1, B2], t0 + timedelta(days=1), "rev1")
    assert d.added == [] and d.removed == []  # idempotent
    d = apply_snapshot(pg, [B1, B3], t0 + timedelta(days=2), "rev2")
    assert d.added == ["lever:palantir"] and d.removed == ["ashby:ramp"]
    d = apply_snapshot(pg, [B1, B2, B3], t0 + timedelta(days=3), "rev3")
    assert d.added == ["ashby:ramp"] and d.removed == []
    rows = _rows(pg)
    ramp = [r for r in rows if r["board"] == "ramp"]
    assert len(ramp) == 2
    assert ramp[0]["removed_at"] == t0 + timedelta(days=2) and ramp[0]["registry_revision"] == "rev1"
    assert ramp[1]["removed_at"] is None and ramp[1]["registry_revision"] == "rev3"
    assert [r for r in rows if r["board"] == "anthropic"][0]["removed_at"] is None
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/store/test_panel.py -q` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — `src/jobhunter/store/panel.py`:

```python
"""Versioned board membership (spec §5.5), derived from archived registry snapshots."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.keys import registry_key
from jobhunter.models import Board
from jobhunter.store.db import Conn


@dataclass(slots=True)
class PanelDelta:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def boards_from_snapshot(data: bytes) -> tuple[Board, ...]:
    rows: list[dict[str, Any]] = json.loads(data.decode("utf-8"))
    return tuple(
        Board(
            company=r["company"], source=r["source"], board=r["board"],
            country=r.get("country"), tags=tuple(r.get("tags") or ()),
        )
        for r in rows
    )


def load_snapshot(store: ArchiveStore, revision: str) -> tuple[Board, ...]:
    return boards_from_snapshot(store.get(registry_key(revision)))


def apply_snapshot(conn: Conn, boards: Iterable[Board], at: datetime, revision: str) -> PanelDelta:
    wanted = {b.key: b for b in boards}
    open_rows = conn.execute(
        "SELECT source, board FROM panel WHERE removed_at IS NULL"
    ).fetchall()
    open_keys = {f"{r['source']}:{r['board']}" for r in open_rows}
    delta = PanelDelta()
    for key in sorted(open_keys - wanted.keys()):
        source, board = key.split(":", 1)
        conn.execute(
            "UPDATE panel SET removed_at = %s WHERE source = %s AND board = %s AND removed_at IS NULL",
            (at, source, board),
        )
        delta.removed.append(key)
    for key in sorted(wanted.keys() - open_keys):
        b = wanted[key]
        conn.execute(
            "INSERT INTO panel (source, board, company, added_at, removed_at, registry_revision) "
            "VALUES (%s, %s, %s, %s, NULL, %s)",
            (b.source, b.board, b.company, at, revision),
        )
        delta.added.append(key)
    return delta
```

- [ ] **Step 4: Verify** — `uv run pytest tests/store -q && uv run ruff check . && uv run mypy` → pass.

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/store/panel.py tests/store/test_panel.py
git commit -m "feat(store): panel membership from registry snapshots"
```

---

### Task 5: `Ingestor.ingest` — parse, versions, documents, presence, health (no transitions yet)

**Files:**
- Create: `src/jobhunter/store/lifecycle.py`, `tests/store/test_lifecycle.py`, `tests/store/helpers.py`

**Interfaces:**
- Produces:
  - `store.lifecycle.OutOfOrder(Exception)`.
  - `store.lifecycle.AttemptResult(attempt_id, health, observed_count, parsed_count, failed_count, unidentifiable_count, new_versions, new_documents, opened, changed, closed, reopened)` (ints; the last four filled by Task 6, zero until then).
  - `store.lifecycle.Ingestor(conn: Conn, store: ArchiveStore, *, drop_ratio: float = 0.5, normalizer_version: str = NORMALIZER_VERSION, to_markdown=markdown.to_markdown)` with `.ingest(manifest: AttemptManifest) -> AttemptResult | None` (`None` = already ingested) — one transaction; steps 1–4 of spec §5.4 here, 5–7 in Task 6.
  - `store.lifecycle.gunzip(data) -> bytes`.
  - `tests/store/helpers.py`: `make_manifest(store, source, board, started_at, body: bytes | None, *, run_id="r", registry_revision, transport="ok", http_status=200) -> AttemptManifest` — writes blob + manifest to the store and returns the manifest (transport `!= "ok"` writes no blob); `write_registry(store, boards) -> revision`; `board_payload(source, records) -> bytes` (wraps records in the source's envelope); `gh_record(id, title, content_html, **extra)`, `lv_record(id, text, opening)`, `ab_record(id, title, description_html)` minimal valid records.

- [ ] **Step 1: Failing tests**

`tests/store/helpers.py`:

```python
from __future__ import annotations

import gzip
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.keys import attempt_key, blob_key, registry_key
from jobhunter.archive.manifests import write_manifest
from jobhunter.hashing import sha256_hex
from jobhunter.models import AttemptManifest, Board
from jobhunter.registry import Registry


def write_registry(store: ArchiveStore, boards: Iterable[Board]) -> str:
    ordered = tuple(sorted(boards, key=lambda b: (b.source, b.board)))
    reg = Registry(boards=ordered, revision="")
    snap = reg.snapshot_json()
    rev = sha256_hex(snap)
    store.put(registry_key(rev), snap)
    return rev


def gh_record(id_: int | str, title: str, content_html: str, **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "id": id_, "title": title, "company_name": "Anthropic",
        "absolute_url": f"https://job-boards.greenhouse.io/anthropic/jobs/{id_}",
        "location": {"name": "SF"}, "offices": [], "departments": [{"name": "Eng"}],
        "first_published": "2026-04-14T06:00:34-04:00", "updated_at": "2026-08-03T18:25:22-04:00",
        "content": content_html.replace("<", "&lt;").replace(">", "&gt;"),
    }
    rec.update(extra)
    return rec


def lv_record(id_: str, text: str, opening: str, **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "id": id_, "text": text, "categories": {"commitment": "Full-time", "location": "NYC",
                                                 "team": "Eng", "allLocations": ["NYC"]},
        "workplaceType": "hybrid", "createdAt": 1711403416463, "opening": opening,
        "descriptionBody": "", "additional": "", "lists": [],
        "hostedUrl": f"https://jobs.lever.co/palantir/{id_}",
        "applyUrl": f"https://jobs.lever.co/palantir/{id_}/apply",
    }
    rec.update(extra)
    return rec


def ab_record(id_: str, title: str, description_html: str, **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "id": id_, "title": title, "department": "Eng", "team": "Web",
        "employmentType": "FullTime", "location": "NYC", "secondaryLocations": [],
        "isRemote": False, "isListed": True, "workplaceType": "Hybrid",
        "publishedAt": "2026-03-09T17:44:00.817+00:00",
        "jobUrl": f"https://jobs.ashbyhq.com/ramp/{id_}",
        "applyUrl": f"https://jobs.ashbyhq.com/ramp/{id_}/application",
        "descriptionHtml": description_html, "compensation": None,
    }
    rec.update(extra)
    return rec


def board_payload(source: str, records: list[dict[str, Any]]) -> bytes:
    if source == "greenhouse":
        obj: Any = {"jobs": records, "meta": {"total": len(records)}}
    elif source == "lever":
        obj = records
    elif source == "ashby":
        obj = {"apiVersion": "v0.1", "jobs": records}
    else:
        raise ValueError(source)
    return json.dumps(obj).encode("utf-8")


def make_manifest(
    store: ArchiveStore,
    source: str,
    board: str,
    started_at: datetime,
    body: bytes | None,
    *,
    run_id: str = "r",
    registry_revision: str = "rev",
    transport: str = "ok",
    http_status: int | None = 200,
    adapter_version: str | None = None,
) -> AttemptManifest:
    sha: str | None = None
    if body is not None and transport == "ok":
        sha = sha256_hex(body)
        store.put(blob_key(sha), gzip.compress(body, mtime=0))
    m = AttemptManifest(
        attempt_id=attempt_key(source, board, started_at), run_id=run_id, source=source,
        board=board, started_at=started_at, finished_at=started_at, url="u",
        http_status=http_status, transport=transport, blob_sha256=sha,
        payload_bytes=len(body or b""), record_count=None,
        adapter_version=adapter_version or f"{source}/1", registry_revision=registry_revision,
        cli_version="test", error=None if transport == "ok" else "boom",
    )
    write_manifest(store, m)
    return m
```

`tests/store/test_lifecycle.py` (Task 5 portion):

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jobhunter.archive.keys import version_key
from jobhunter.archive.local import LocalFS
from jobhunter.models import Board
from jobhunter.store.lifecycle import Ingestor, OutOfOrder
from tests.store.helpers import (
    ab_record,
    board_payload,
    gh_record,
    lv_record,
    make_manifest,
    write_registry,
)

T0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
BOARDS = [Board("Anthropic", "greenhouse", "anthropic"), Board("Palantir", "lever", "palantir"),
          Board("Ramp", "ashby", "ramp")]


def day(n: int) -> datetime:
    return T0 + timedelta(days=n)


@pytest.fixture
def store(tmp_path: Path) -> LocalFS:
    return LocalFS(tmp_path / "archive")


@pytest.fixture
def rev(store: LocalFS) -> str:
    return write_registry(store, BOARDS)


def q(conn: psycopg.Connection[dict[str, Any]], sql: str, *args: Any) -> list[dict[str, Any]]:
    return conn.execute(sql, args).fetchall()


def test_ingest_ok_attempt_writes_provenance_and_presence(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    body = board_payload("greenhouse", [gh_record(1, "A", "<p>a</p>"), gh_record(2, "B", "<p>b</p>")])
    m = make_manifest(store, "greenhouse", "anthropic", day(0), body, registry_revision=rev)
    r = Ingestor(pg, store).ingest(m)
    pg.commit()
    assert r is not None and r.health == "ok"
    assert (r.observed_count, r.parsed_count, r.failed_count, r.unidentifiable_count) == (2, 2, 0, 0)
    assert r.new_versions == 2 and r.new_documents == 2
    att = q(pg, "SELECT * FROM fetch_attempts")[0]
    assert att["health"] == "ok" and att["observed_count"] == 2 and att["prev_observed_count"] is None
    vs = q(pg, "SELECT uid, title, first_seen_attempt FROM posting_versions ORDER BY uid")
    assert [v["uid"] for v in vs] == ["gh:anthropic:1", "gh:anthropic:2"]
    assert all(v["first_seen_attempt"] == m.attempt_id for v in vs)
    for v in q(pg, "SELECT version_hash FROM posting_versions"):
        assert store.exists(version_key(v["version_hash"]))
    docs = q(pg, "SELECT normalizer_version, markdown FROM documents ORDER BY markdown")
    assert [d["markdown"] for d in docs] == ["a", "b"] and docs[0]["normalizer_version"] == "md/1"
    pres = q(pg, "SELECT uid, parse_status, runs, first_attempt, last_attempt FROM presence ORDER BY uid")
    assert len(pres) == 2 and all(p["runs"] == 1 and p["parse_status"] == "ok" for p in pres)
    assert all(p["first_attempt"] == p["last_attempt"] == m.attempt_id for p in pres)
    assert q(pg, "SELECT * FROM panel")[0]["board"] == "anthropic"  # snapshot applied
    assert q(pg, "SELECT value FROM schema_meta WHERE key='last_ingested_attempt'")[0]["value"] == m.attempt_id


def test_presence_extends_then_splits(pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str) -> None:
    ing = Ingestor(pg, store)
    same = board_payload("greenhouse", [gh_record(1, "A", "<p>a</p>")])
    changed = board_payload("greenhouse", [gh_record(1, "A2", "<p>a</p>")])
    for n, body in enumerate([same, same, changed, changed]):
        ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(n), body, registry_revision=rev))
    pg.commit()
    rows = q(pg, "SELECT runs, first_at, last_at FROM presence ORDER BY first_at")
    assert [r["runs"] for r in rows] == [2, 2]
    assert rows[0]["first_at"] == day(0) and rows[0]["last_at"] == day(1)
    assert rows[1]["first_at"] == day(2) and rows[1]["last_at"] == day(3)
    assert q(pg, "SELECT count(*) AS n FROM posting_versions")[0]["n"] == 2


def test_gap_after_error_attempt_starts_new_interval(pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str) -> None:
    ing = Ingestor(pg, store)
    body = board_payload("ashby", [ab_record("x", "T", "<p>t</p>")])
    ing.ingest(make_manifest(store, "ashby", "ramp", day(0), body, registry_revision=rev))
    ing.ingest(make_manifest(store, "ashby", "ramp", day(1), None, transport="timeout", http_status=None,
                             registry_revision=rev))
    ing.ingest(make_manifest(store, "ashby", "ramp", day(2), body, registry_revision=rev))
    pg.commit()
    assert [r["runs"] for r in q(pg, "SELECT runs FROM presence ORDER BY first_at")] == [1, 1]
    assert q(pg, "SELECT health FROM fetch_attempts ORDER BY started_at")[1]["health"] == "error"


def test_failed_record_is_present_without_version(pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str) -> None:
    recs = [gh_record(1, "A", "<p>a</p>"), {"id": 2, "content": "&lt;p&gt;no title&lt;/p&gt;"}, "junk"]
    m = make_manifest(store, "greenhouse", "anthropic", day(0), board_payload("greenhouse", recs),
                      registry_revision=rev)
    r = Ingestor(pg, store).ingest(m)
    pg.commit()
    assert r is not None
    assert (r.observed_count, r.parsed_count, r.failed_count, r.unidentifiable_count) == (2, 1, 1, 1)
    pres = {p["uid"]: p for p in q(pg, "SELECT uid, parse_status, version_hash FROM presence")}
    assert pres["gh:anthropic:2"]["parse_status"] == "failed" and pres["gh:anthropic:2"]["version_hash"] is None
    assert pres["gh:anthropic:1"]["parse_status"] == "ok"


def test_duplicate_ids_in_payload_are_counted(pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str) -> None:
    recs = [gh_record(1, "A", "<p>a</p>"), gh_record(1, "A dup", "<p>a</p>")]
    m = make_manifest(store, "greenhouse", "anthropic", day(0), board_payload("greenhouse", recs),
                      registry_revision=rev)
    r = Ingestor(pg, store).ingest(m)
    pg.commit()
    assert r is not None and r.observed_count == 1
    assert q(pg, "SELECT warnings FROM fetch_attempts")[0]["warnings"] == {"duplicate_ids": 1}


def test_envelope_error_is_health_error(pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str) -> None:
    m = make_manifest(store, "lever", "palantir", day(0), b"<html>", registry_revision=rev)
    r = Ingestor(pg, store).ingest(m)
    pg.commit()
    assert r is not None and r.health == "error" and r.observed_count == 0
    assert q(pg, "SELECT error FROM fetch_attempts")[0]["error"].startswith("envelope")
    assert q(pg, "SELECT count(*) AS n FROM presence")[0]["n"] == 0


def test_drop_guard(pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str) -> None:
    ing = Ingestor(pg, store, drop_ratio=0.5)
    four = board_payload("lever", [lv_record(str(i), f"T{i}", "<p>x</p>") for i in range(4)])
    two = board_payload("lever", [lv_record(str(i), f"T{i}", "<p>x</p>") for i in range(2)])
    one = board_payload("lever", [lv_record("0", "T0", "<p>x</p>")])
    empty = board_payload("lever", [])
    healths = []
    for n, body in enumerate([four, two, one, empty, empty]):
        r = ing.ingest(make_manifest(store, "lever", "palantir", day(n), body, registry_revision=rev))
        assert r is not None
        healths.append((r.health, r.observed_count))
    pg.commit()
    # 4 -> 2: 2 < 0.5*4 is False -> ok ; 2 -> 1: 1 < 1 False -> ok ; 1 -> 0: suspect ; 0 -> 0: ok
    assert healths == [("ok", 4), ("ok", 2), ("ok", 1), ("suspect_drop", 0), ("ok", 0)]
    prevs = [a["prev_observed_count"] for a in q(pg, "SELECT prev_observed_count FROM fetch_attempts ORDER BY started_at")]
    assert prevs == [None, 4, 2, 1, 0]


def test_idempotent_and_out_of_order(pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str) -> None:
    ing = Ingestor(pg, store)
    body = board_payload("ashby", [ab_record("x", "T", "<p>t</p>")])
    m1 = make_manifest(store, "ashby", "ramp", day(1), body, registry_revision=rev)
    assert ing.ingest(m1) is not None
    assert ing.ingest(m1) is None
    m0 = make_manifest(store, "ashby", "ramp", day(0), body, registry_revision=rev)
    with pytest.raises(OutOfOrder):
        ing.ingest(m0)
    pg.commit()
    assert q(pg, "SELECT count(*) AS n FROM fetch_attempts")[0]["n"] == 1
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/store/test_lifecycle.py -q` → `ModuleNotFoundError: jobhunter.store.lifecycle`.

- [ ] **Step 3: Implement** — `src/jobhunter/store/lifecycle.py` (Task 6 extends `_transitions`/`_reconcile`; write them as no-op stubs returning zeros now, clearly marked, so this task's tests pass on the parts they assert):

```python
"""The one write path: archive manifest -> store (spec §5.4). One transaction per attempt."""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from jobhunter import markdown as md
from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.keys import blob_key, version_key
from jobhunter.hashing import VERSION_HASH_V, sha256_hex, version_hash
from jobhunter.models import AttemptManifest, Board, PostingVersion
from jobhunter.sources import get_source
from jobhunter.sources.base import EnvelopeError, NormalizeError
from jobhunter.store import db
from jobhunter.store.db import Conn
from jobhunter.store.panel import apply_snapshot, load_snapshot
from jobhunter.timeutil import iso, parse_iso


class OutOfOrder(Exception):
    """A manifest older than the last ingested one; run `rebuild`."""


def gunzip(data: bytes) -> bytes:
    return gzip.decompress(data)


@dataclass(slots=True)
class AttemptResult:
    attempt_id: str
    health: str
    observed_count: int = 0
    parsed_count: int = 0
    failed_count: int = 0
    unidentifiable_count: int = 0
    new_versions: int = 0
    new_documents: int = 0
    opened: int = 0
    changed: int = 0
    closed: int = 0
    reopened: int = 0
    warnings: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _Seen:
    uid: str
    source_id: str
    version_hash: str | None
    parse_status: str  # ok | failed
    pv: PostingVersion | None
    source_updated_at: datetime | None


class Ingestor:
    def __init__(
        self,
        conn: Conn,
        store: ArchiveStore,
        *,
        drop_ratio: float = 0.5,
        normalizer_version: str = md.NORMALIZER_VERSION,
        to_markdown: Callable[[str], str] = md.to_markdown,
    ) -> None:
        self.conn = conn
        self.store = store
        self.drop_ratio = drop_ratio
        self.normalizer_version = normalizer_version
        self.to_markdown = to_markdown
        self._boards_by_rev: dict[str, dict[str, Board]] = {}

    # ---- registry / panel
    def _boards(self, revision: str) -> dict[str, Board]:
        if revision not in self._boards_by_rev:
            try:
                boards = load_snapshot(self.store, revision)
            except KeyError:
                boards = ()
            self._boards_by_rev[revision] = {b.key: b for b in boards}
        return self._boards_by_rev[revision]

    def _apply_registry_if_changed(self, m: AttemptManifest) -> None:
        if db.get_meta(self.conn, "last_registry_revision") == m.registry_revision:
            return
        boards = self._boards(m.registry_revision)
        if boards:  # a missing snapshot leaves the panel untouched rather than removing everything
            apply_snapshot(self.conn, boards.values(), m.started_at, m.registry_revision)
        db.set_meta(self.conn, "last_registry_revision", m.registry_revision)

    def _board(self, m: AttemptManifest) -> Board:
        return self._boards(m.registry_revision).get(
            f"{m.source}:{m.board}", Board(company=m.board, source=m.source, board=m.board)
        )

    # ---- public
    def ingest(self, m: AttemptManifest) -> AttemptResult | None:
        with self.conn.transaction():
            if self.conn.execute(
                "SELECT 1 FROM fetch_attempts WHERE attempt_id = %s", (m.attempt_id,)
            ).fetchone():
                return None
            last_at = db.get_meta(self.conn, "last_ingested_at")
            if last_at and m.started_at < parse_iso(last_at):
                raise OutOfOrder(f"{m.attempt_id} is older than last ingested {last_at}; run rebuild")
            self._apply_registry_if_changed(m)
            result = self._ingest_inner(m)
            self._upsert_run(m.run_id)
            db.set_meta(self.conn, "last_ingested_attempt", m.attempt_id)
            db.set_meta(self.conn, "last_ingested_at", iso(m.started_at))
            return result

    # ---- steps
    def _ingest_inner(self, m: AttemptManifest) -> AttemptResult:
        if m.transport != "ok" or not m.blob_sha256:
            self._insert_attempt(m, "error", AttemptResult(m.attempt_id, "error"), None, m.error)
            return AttemptResult(m.attempt_id, "error")
        source = get_source(m.source)
        board = self._board(m)
        body = gunzip(self.store.get(blob_key(m.blob_sha256)))
        try:
            records = list(source.parse(body))
        except EnvelopeError as e:
            res = AttemptResult(m.attempt_id, "error")
            self._insert_attempt(m, "error", res, None, f"envelope: {e}")
            return res

        # phase 1: pure compute
        seen: dict[str, _Seen] = {}
        res = AttemptResult(m.attempt_id, "ok")
        for rec in records:
            if rec.source_id is None:
                res.unidentifiable_count += 1
                continue
            if rec.source_id in seen:
                res.warnings["duplicate_ids"] = res.warnings.get("duplicate_ids", 0) + 1
                continue
            try:
                pv = source.normalize(rec, board)
            except NormalizeError:
                uid = f"{_prefix(m.source)}:{m.board}:{rec.source_id}"
                seen[rec.source_id] = _Seen(uid, rec.source_id, None, "failed", None, None)
                res.failed_count += 1
                continue
            seen[rec.source_id] = _Seen(pv.uid, rec.source_id, version_hash(pv), "ok", pv,
                                        pv.source_updated_at)
            res.parsed_count += 1
        res.observed_count = len(seen)

        # Two different "previous" attempts, on purpose:
        #  - prev (non-error) feeds the drop guard: it is the last attempt that said anything
        #    about the board's size;
        #  - prev_any (any health) feeds presence continuity: an interval may only be extended
        #    across consecutive attempts; an error attempt in between is a gap we did not observe.
        prev = self.conn.execute(
            "SELECT attempt_id, observed_count FROM fetch_attempts "
            "WHERE source = %s AND board = %s AND health <> 'error' "
            "ORDER BY started_at DESC LIMIT 1",
            (m.source, m.board),
        ).fetchone()
        prev_any = self.conn.execute(
            "SELECT attempt_id FROM fetch_attempts WHERE source = %s AND board = %s "
            "ORDER BY started_at DESC LIMIT 1",
            (m.source, m.board),
        ).fetchone()
        prev_any_id = prev_any["attempt_id"] if prev_any else None
        prev_count = int(prev["observed_count"]) if prev else None
        if prev_count is not None and res.observed_count < self.drop_ratio * prev_count:
            res.health = "suspect_drop"

        # phase 2: writes
        self._insert_attempt(m, res.health, res, prev_count, None)
        for s in seen.values():
            if s.pv is not None and s.version_hash is not None:
                if self._insert_version(m, s.pv, s.version_hash):
                    res.new_versions += 1
                if self._insert_document(s.pv, s.version_hash):
                    res.new_documents += 1
            self._presence(s, m, prev_any_id)
        self._transitions(seen, m, res)
        if res.health == "ok":
            self._reconcile(m, res)
        return res

    def _insert_attempt(
        self, m: AttemptManifest, health: str, res: AttemptResult, prev_count: int | None,
        error: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO fetch_attempts (attempt_id, run_id, source, board, started_at, finished_at, "
            "http_status, transport, health, blob_sha256, payload_bytes, observed_count, parsed_count, "
            "failed_count, unidentifiable_count, prev_observed_count, adapter_version, "
            "registry_revision, cli_version, warnings, error) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (attempt_id) DO NOTHING",
            (m.attempt_id, m.run_id, m.source, m.board, m.started_at, m.finished_at, m.http_status,
             m.transport, health, m.blob_sha256, m.payload_bytes, res.observed_count,
             res.parsed_count, res.failed_count, res.unidentifiable_count, prev_count,
             m.adapter_version, m.registry_revision, m.cli_version,
             Jsonb(res.warnings) if res.warnings else None, error),
        )

    def _insert_version(self, m: AttemptManifest, pv: PostingVersion, vh: str) -> bool:
        cur = self.conn.execute(
            "INSERT INTO posting_versions (version_hash, version_hash_v, uid, source, board, source_id, "
            "title, company, locations, workplace_type, is_remote, department, team, employment_type, "
            "compensation, url, apply_url, source_created_at, first_seen_attempt) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (version_hash) DO NOTHING",
            (vh, VERSION_HASH_V, pv.uid, pv.source, pv.board, pv.source_id, pv.title, pv.company,
             Jsonb(list(pv.locations)), pv.workplace_type, pv.is_remote, pv.department, pv.team,
             pv.employment_type,
             Jsonb({"min": pv.compensation.min, "max": pv.compensation.max,
                    "currency": pv.compensation.currency, "interval": pv.compensation.interval})
             if pv.compensation else None,
             pv.url, pv.apply_url, pv.source_created_at, m.attempt_id),
        )
        inserted = cur.rowcount == 1
        if inserted:
            self.store.put(version_key(vh), gzip.compress(pv.description_html.encode("utf-8"), mtime=0))
        return inserted

    def _insert_document(self, pv: PostingVersion, vh: str) -> bool:
        markdown = self.to_markdown(pv.description_html)
        dh = sha256_hex(markdown.encode("utf-8"))
        cur = self.conn.execute(
            "INSERT INTO documents (document_hash, version_hash, normalizer_version, markdown) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (dh, vh, self.normalizer_version, markdown),
        )
        return cur.rowcount == 1

    def _presence(self, s: _Seen, m: AttemptManifest, prev_any_id: str | None) -> None:
        cur = self.conn.execute(
            "SELECT first_attempt, last_attempt, version_hash, parse_status FROM presence "
            "WHERE uid = %s ORDER BY last_at DESC LIMIT 1",
            (s.uid,),
        ).fetchone()
        if (
            cur is not None
            and prev_any_id is not None
            and cur["last_attempt"] == prev_any_id
            and cur["version_hash"] == s.version_hash
            and cur["parse_status"] == s.parse_status
        ):
            self.conn.execute(
                "UPDATE presence SET last_attempt = %s, last_at = %s, runs = runs + 1 "
                "WHERE uid = %s AND first_attempt = %s",
                (m.attempt_id, m.started_at, s.uid, cur["first_attempt"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO presence (uid, version_hash, parse_status, first_attempt, last_attempt, "
                "first_at, last_at, runs) VALUES (%s,%s,%s,%s,%s,%s,%s,1)",
                (s.uid, s.version_hash, s.parse_status, m.attempt_id, m.attempt_id,
                 m.started_at, m.started_at),
            )

    def _transitions(self, seen: dict[str, _Seen], m: AttemptManifest, res: AttemptResult) -> None:
        return  # Task 6

    def _reconcile(self, m: AttemptManifest, res: AttemptResult) -> None:
        return  # Task 6

    def _upsert_run(self, run_id: str) -> None:
        self.conn.execute(
            "INSERT INTO runs (run_id, started_at, finished_at, cli_version, boards_total, boards_ok, "
            "boards_suspect, boards_error) "
            "SELECT run_id, min(started_at), max(finished_at), max(cli_version), count(*), "
            "count(*) FILTER (WHERE health = 'ok'), count(*) FILTER (WHERE health = 'suspect_drop'), "
            "count(*) FILTER (WHERE health = 'error') FROM fetch_attempts WHERE run_id = %s "
            "GROUP BY run_id "
            "ON CONFLICT (run_id) DO UPDATE SET started_at = EXCLUDED.started_at, "
            "finished_at = EXCLUDED.finished_at, cli_version = EXCLUDED.cli_version, "
            "boards_total = EXCLUDED.boards_total, boards_ok = EXCLUDED.boards_ok, "
            "boards_suspect = EXCLUDED.boards_suspect, boards_error = EXCLUDED.boards_error",
            (run_id,),
        )


def _prefix(source: str) -> str:
    from jobhunter.models import SOURCE_PREFIX

    return SOURCE_PREFIX[source]
```

- [ ] **Step 4: Verify** — `uv run pytest tests/store -q && uv run ruff check . && uv run mypy` → pass (unused-argument warnings on the stubs are fine for ruff's selected rules; if `B` flags them, prefix the parameters with `_` in the stub only).

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/store/lifecycle.py tests/store/test_lifecycle.py tests/store/helpers.py
git commit -m "feat(store): Ingestor — attempts, versions (+html to archive), documents, presence intervals, drop guard, runs"
```

---

### Task 6: Transitions, reconcile, events

**Files:**
- Modify: `src/jobhunter/store/lifecycle.py` (replace the two stubs)
- Test: `tests/store/test_lifecycle.py` (append)

**Interfaces:**
- Produces: `AttemptResult.opened/changed/closed/reopened` populated; `postings` and `posting_events` rows per spec §5.4 steps 5–6.

- [ ] **Step 1: Failing tests** — append to `tests/store/test_lifecycle.py`:

```python
def _events(pg: psycopg.Connection[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(e["kind"], e["uid"]) for e in q(pg, "SELECT kind, uid FROM posting_events ORDER BY event_id")]


def test_open_change_close_reopen(pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str) -> None:
    ing = Ingestor(pg, store)
    v1 = board_payload("ashby", [ab_record("x", "T", "<p>t</p>"), ab_record("y", "U", "<p>u</p>")])
    v2 = board_payload("ashby", [ab_record("x", "T v2", "<p>t</p>"), ab_record("y", "U", "<p>u</p>")])
    v3 = board_payload("ashby", [ab_record("x", "T v2", "<p>t</p>")])
    v4 = board_payload("ashby", [ab_record("x", "T v2", "<p>t</p>"), ab_record("y", "U v2", "<p>u</p>")])
    results = []
    for n, body in enumerate([v1, v2, v3, v4]):
        r = ing.ingest(make_manifest(store, "ashby", "ramp", day(n), body, registry_revision=rev))
        assert r is not None
        results.append((r.opened, r.changed, r.closed, r.reopened))
    pg.commit()
    assert results == [(2, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)]
    assert _events(pg) == [
        ("opened", "ab:ramp:x"), ("opened", "ab:ramp:y"),
        ("changed", "ab:ramp:x"), ("closed", "ab:ramp:y"), ("reopened", "ab:ramp:y"),
    ]
    x = q(pg, "SELECT * FROM postings WHERE uid = 'ab:ramp:x'")[0]
    assert x["status"] == "open" and x["version_count"] == 2 and x["reopen_count"] == 0
    assert x["first_seen_at"] == day(0) and x["last_seen_at"] == day(3)
    y = q(pg, "SELECT * FROM postings WHERE uid = 'ab:ramp:y'")[0]
    assert y["status"] == "open" and y["reopen_count"] == 1 and y["version_count"] == 2
    assert y["closed_lower_at"] is None and y["closed_by_attempt"] is None
    closed = q(pg, "SELECT * FROM posting_events WHERE kind = 'closed'")[0]
    assert closed["closed_lower_at"] == day(1) and closed["closed_upper_at"] == day(2)
    reopened = q(pg, "SELECT * FROM posting_events WHERE kind = 'reopened'")[0]
    assert reopened["from_version"] != reopened["to_version"]
    changed = q(pg, "SELECT * FROM posting_events WHERE kind = 'changed'")[0]
    assert changed["from_version"] and changed["to_version"] and changed["at"] == day(1)


def test_suspect_drop_defers_closures_and_1_to_0_closes_next_run(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store)
    one = board_payload("lever", [lv_record("a", "A", "<p>a</p>")])
    empty = board_payload("lever", [])
    r0 = ing.ingest(make_manifest(store, "lever", "palantir", day(0), one, registry_revision=rev))
    r1 = ing.ingest(make_manifest(store, "lever", "palantir", day(1), empty, registry_revision=rev))
    r2 = ing.ingest(make_manifest(store, "lever", "palantir", day(2), empty, registry_revision=rev))
    pg.commit()
    assert r0 and r1 and r2
    assert (r1.health, r1.closed) == ("suspect_drop", 0)
    assert (r2.health, r2.closed) == ("ok", 1)
    p = q(pg, "SELECT * FROM postings")[0]
    assert p["status"] == "closed" and p["closed_lower_at"] == day(0) and p["closed_upper_at"] == day(2)
    assert p["closed_by_attempt"] == r2.attempt_id


def test_partial_payload_defers_then_closes_with_true_lower_bound(
    pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str
) -> None:
    ing = Ingestor(pg, store)
    ids = [str(i) for i in range(10)]
    full = board_payload("lever", [lv_record(i, "T", "<p>x</p>") for i in ids])
    third = board_payload("lever", [lv_record(i, "T", "<p>x</p>") for i in ids[:3]])
    ing.ingest(make_manifest(store, "lever", "palantir", day(0), full, registry_revision=rev))
    r1 = ing.ingest(make_manifest(store, "lever", "palantir", day(1), third, registry_revision=rev))
    r2 = ing.ingest(make_manifest(store, "lever", "palantir", day(2), full, registry_revision=rev))
    pg.commit()
    assert r1 and r1.health == "suspect_drop" and r1.closed == 0
    assert r2 and r2.health == "ok" and r2.closed == 0 and r2.reopened == 0
    assert q(pg, "SELECT count(*) AS n FROM postings WHERE status = 'open'")[0]["n"] == 10


def test_failed_parse_keeps_posting_open(pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str) -> None:
    ing = Ingestor(pg, store)
    good = board_payload("greenhouse", [gh_record(1, "A", "<p>a</p>")])
    broken = board_payload("greenhouse", [{"id": 1, "content": "x"}])  # no title -> NormalizeError
    ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(0), good, registry_revision=rev))
    r = ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(1), broken, registry_revision=rev))
    pg.commit()
    assert r and r.closed == 0 and r.failed_count == 1
    p = q(pg, "SELECT status, last_seen_at, current_version_hash FROM postings")[0]
    assert p["status"] == "open" and p["last_seen_at"] == day(1) and p["current_version_hash"]


def test_error_attempt_touches_nothing(pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str) -> None:
    ing = Ingestor(pg, store)
    good = board_payload("greenhouse", [gh_record(1, "A", "<p>a</p>")])
    ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(0), good, registry_revision=rev))
    r = ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(1), None, transport="timeout",
                                 http_status=None, registry_revision=rev))
    pg.commit()
    assert r and r.health == "error"
    p = q(pg, "SELECT status, last_seen_at FROM postings")[0]
    assert p["status"] == "open" and p["last_seen_at"] == day(0)
    runs = q(pg, "SELECT boards_total, boards_ok, boards_error FROM runs")[0]
    assert (runs["boards_total"], runs["boards_ok"], runs["boards_error"]) == (2, 1, 1)


def test_source_updated_at_is_refreshed(pg: psycopg.Connection[dict[str, Any]], store: LocalFS, rev: str) -> None:
    ing = Ingestor(pg, store)
    a = board_payload("greenhouse", [gh_record(1, "A", "<p>a</p>", updated_at="2026-08-01T00:00:00Z")])
    b = board_payload("greenhouse", [gh_record(1, "A", "<p>a</p>", updated_at="2026-08-09T00:00:00Z")])
    ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(0), a, registry_revision=rev))
    r = ing.ingest(make_manifest(store, "greenhouse", "anthropic", day(1), b, registry_revision=rev))
    pg.commit()
    assert r and r.changed == 0  # updated_at is metadata, not identity
    assert q(pg, "SELECT source_updated_at FROM postings")[0]["source_updated_at"] == datetime(2026, 8, 9, tzinfo=UTC)
    assert q(pg, "SELECT count(*) AS n FROM posting_versions")[0]["n"] == 1
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/store/test_lifecycle.py -q` → the new tests fail (`opened == 0`, no events).

- [ ] **Step 3: Implement** — replace the two stubs in `lifecycle.py`:

```python
    def _transitions(self, seen: dict[str, _Seen], m: AttemptManifest, res: AttemptResult) -> None:
        for s in seen.values():
            row = self.conn.execute(
                "SELECT status, current_version_hash FROM postings WHERE uid = %s FOR UPDATE", (s.uid,)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO postings (uid, source, board, source_id, status, current_version_hash, "
                    "version_count, reopen_count, first_seen_attempt, first_seen_at, last_seen_attempt, "
                    "last_seen_at, source_updated_at) VALUES (%s,%s,%s,%s,'open',%s,%s,0,%s,%s,%s,%s,%s)",
                    (s.uid, m.source, m.board, s.source_id, s.version_hash,
                     1 if s.version_hash else 0, m.attempt_id, m.started_at, m.attempt_id,
                     m.started_at, s.source_updated_at),
                )
                self._event("opened", s.uid, m, None, s.version_hash)
                res.opened += 1
                continue
            cur_vh = row["current_version_hash"]
            version_changed = s.version_hash is not None and s.version_hash != cur_vh
            if row["status"] == "closed":
                self.conn.execute(
                    "UPDATE postings SET status = 'open', reopen_count = reopen_count + 1, "
                    "closed_lower_at = NULL, closed_upper_at = NULL, closed_by_attempt = NULL, "
                    "last_seen_attempt = %s, last_seen_at = %s, "
                    "current_version_hash = COALESCE(%s, current_version_hash), "
                    "version_count = version_count + %s, "
                    "source_updated_at = COALESCE(%s, source_updated_at) WHERE uid = %s",
                    (m.attempt_id, m.started_at, s.version_hash, 1 if version_changed else 0,
                     s.source_updated_at, s.uid),
                )
                self._event("reopened", s.uid, m, cur_vh, s.version_hash or cur_vh)
                res.reopened += 1
            elif version_changed:
                self.conn.execute(
                    "UPDATE postings SET current_version_hash = %s, version_count = version_count + 1, "
                    "last_seen_attempt = %s, last_seen_at = %s, "
                    "source_updated_at = COALESCE(%s, source_updated_at) WHERE uid = %s",
                    (s.version_hash, m.attempt_id, m.started_at, s.source_updated_at, s.uid),
                )
                self._event("changed", s.uid, m, cur_vh, s.version_hash)
                res.changed += 1
            else:
                self.conn.execute(
                    "UPDATE postings SET last_seen_attempt = %s, last_seen_at = %s, "
                    "source_updated_at = COALESCE(%s, source_updated_at) WHERE uid = %s",
                    (m.attempt_id, m.started_at, s.source_updated_at, s.uid),
                )

    def _reconcile(self, m: AttemptManifest, res: AttemptResult) -> None:
        rows = self.conn.execute(
            "UPDATE postings SET status = 'closed', closed_lower_at = last_seen_at, "
            "closed_upper_at = %s, closed_by_attempt = %s "
            "WHERE source = %s AND board = %s AND status = 'open' "
            "AND uid NOT IN (SELECT uid FROM presence WHERE last_attempt = %s) "
            "RETURNING uid, current_version_hash, closed_lower_at, closed_upper_at",
            (m.started_at, m.attempt_id, m.source, m.board, m.attempt_id),
        ).fetchall()
        for r in sorted(rows, key=lambda r: str(r["uid"])):
            self.conn.execute(
                "INSERT INTO posting_events (uid, kind, attempt_id, at, from_version, to_version, "
                "closed_lower_at, closed_upper_at) VALUES (%s,'closed',%s,%s,%s,NULL,%s,%s)",
                (r["uid"], m.attempt_id, m.started_at, r["current_version_hash"],
                 r["closed_lower_at"], r["closed_upper_at"]),
            )
            res.closed += 1

    def _event(self, kind: str, uid: str, m: AttemptManifest, from_v: str | None, to_v: str | None) -> None:
        self.conn.execute(
            "INSERT INTO posting_events (uid, kind, attempt_id, at, from_version, to_version) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (uid, kind, m.attempt_id, m.started_at, from_v, to_v),
        )
```

Determinism note for the rebuild-equality test: `_transitions` iterates `seen` in payload order and `_reconcile` sorts by uid, so replaying the same manifests yields identical `event_id` order.

- [ ] **Step 4: Verify** — `uv run pytest tests/store -q && uv run ruff check . && uv run mypy` → pass.

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/store/lifecycle.py tests/store/test_lifecycle.py
git commit -m "feat(store): lifecycle transitions, interval-censored reconcile, posting events"
```

---

### Task 7: `fetch.run` ingests; `ingest` command; DB-unreachable path

**Files:**
- Modify: `src/jobhunter/fetch.py`, `src/jobhunter/cli.py`
- Create: `src/jobhunter/ingest.py`
- Test: `tests/test_fetch.py` (append), `tests/test_ingest.py`, `tests/test_cli.py` (append)

**Interfaces:**
- Produces:
  - `fetch.RunSummary` gains `ingested: int`, `db_error: str | None`, `lock_held: bool` (True if another run held the lock; nothing is fetched then). `to_dict()` includes them; `counts()` unchanged.
  - `fetch.run(..., ingest: bool = True)` — when `settings.database_url` is set: connect + `try_lock`; if the lock is held → return `RunSummary(lock_held=True, outcomes=[])`; else archive as before, then `Ingestor.ingest` each new manifest in `started_at` order; DB connect failure → still archive, set `db_error`, `ingested=0`. When `database_url` is `None` and `ingest=True` → `ConfigError` (spec: required). `ingest=False` is used by tests of the archive path only.
  - `ingest.replay_pending(conn, store, *, drop_ratio) -> ReplaySummary(ingested, skipped, last_attempt)` — all manifests with `started_at > last_ingested_at` (or all, when the store is empty), time order, idempotent.
  - CLI: `ingest [--json]` (lock; replay pending; exit 2 on DB/archive error); `fetch` prints `ingested N` and `db_error` and exits 2 when `db_error` is set (archive was still written).

- [ ] **Step 1: Failing tests** — append to `tests/test_fetch.py`:

```python
def test_run_ingests_into_db(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    settings = replace(_settings(tmp_path), database_url=TEST_DSN)
    t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    schema = pg.execute("SELECT current_schema() AS s").fetchone()["s"]
    summary = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, schema=schema)
    assert summary.db_error is None and summary.ingested == 3 and not summary.lock_held
    n = pg.execute("SELECT count(*) AS n FROM fetch_attempts").fetchone()["n"]
    assert n == 3
    healths = {r["board"]: r["health"] for r in pg.execute("SELECT board, health FROM fetch_attempts")}
    assert healths == {"anthropic": "ok", "ramp": "ok", "palantir": "error"}
    assert pg.execute("SELECT count(*) AS n FROM postings").fetchone()["n"] == 2


def test_run_archives_even_when_db_is_down(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), database_url="postgresql://nobody:x@127.0.0.1:1/none")
    t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    summary = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t)
    assert summary.db_error and summary.ingested == 0
    assert len(list(iter_manifests(LocalFS(tmp_path / "archive")))) == 3


def test_run_returns_lock_held_when_another_run_holds_it(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    from jobhunter.store import db as _db

    settings = replace(_settings(tmp_path), database_url=TEST_DSN)
    schema = pg.execute("SELECT current_schema() AS s").fetchone()["s"]
    assert _db.try_lock(pg)
    try:
        t = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
        summary = run(settings, fetcher=_fetcher(_fake_ats), now=lambda: t, schema=schema)
        assert summary.lock_held and summary.outcomes == []
    finally:
        _db.unlock(pg)
```

Add at the top of `tests/test_fetch.py`: `from dataclasses import replace`, `from typing import Any`, `import psycopg`, `from tests.conftest import TEST_DSN`.

`tests/test_ingest.py`:

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from jobhunter.archive.local import LocalFS
from jobhunter.ingest import replay_pending
from jobhunter.models import Board
from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry


def test_replay_pending_is_incremental_and_idempotent(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    store = LocalFS(tmp_path)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    body = board_payload("ashby", [ab_record("x", "T", "<p>t</p>")])
    make_manifest(store, "ashby", "ramp", t0, body, registry_revision=rev)
    make_manifest(store, "ashby", "ramp", t0 + timedelta(days=1), body, registry_revision=rev)
    s1 = replay_pending(pg, store)
    pg.commit()
    assert (s1.ingested, s1.skipped) == (2, 0)
    make_manifest(store, "ashby", "ramp", t0 + timedelta(days=2), body, registry_revision=rev)
    s2 = replay_pending(pg, store)
    pg.commit()
    assert (s2.ingested, s2.skipped) == (1, 0)
    s3 = replay_pending(pg, store)
    assert (s3.ingested, s3.skipped) == (0, 0)
    assert pg.execute("SELECT count(*) AS n FROM fetch_attempts").fetchone()["n"] == 3
```

Append to `tests/test_cli.py`:

```python
def test_fetch_requires_database_url_and_ingest_command(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOB_HUNTER_DATABASE_URL", raising=False)
    r = runner.invoke(cli.app, ["fetch"])
    assert r.exit_code == 2 and "JOB_HUNTER_DATABASE_URL" in r.stdout
    monkeypatch.setenv("JOB_HUNTER_DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none")
    r = runner.invoke(cli.app, ["fetch", "--json"])
    assert r.exit_code == 2
    assert json.loads(r.stdout)["db_error"]
    r = runner.invoke(cli.app, ["ingest"])
    assert r.exit_code == 2 and "database error" in r.stdout
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_fetch.py tests/test_ingest.py tests/test_cli.py -q` → failures/import errors.

- [ ] **Step 3: Implement**

`src/jobhunter/ingest.py`:

```python
"""Replay archive manifests newer than the last ingested one (repair path)."""

from __future__ import annotations

from dataclasses import dataclass

from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.manifests import all_sorted_by_time
from jobhunter.store import db
from jobhunter.store.db import Conn
from jobhunter.store.lifecycle import Ingestor
from jobhunter.timeutil import parse_iso


@dataclass(slots=True)
class ReplaySummary:
    ingested: int = 0
    skipped: int = 0
    last_attempt: str | None = None


def replay_pending(conn: Conn, store: ArchiveStore, *, drop_ratio: float = 0.5) -> ReplaySummary:
    last_at_raw = db.get_meta(conn, "last_ingested_at")
    last_at = parse_iso(last_at_raw) if last_at_raw else None
    ing = Ingestor(conn, store, drop_ratio=drop_ratio)
    out = ReplaySummary()
    for m in all_sorted_by_time(store):
        if last_at is not None and m.started_at < last_at:
            continue
        r = ing.ingest(m)
        if r is None:
            out.skipped += 1
        else:
            out.ingested += 1
            out.last_attempt = m.attempt_id
    return out
```

`src/jobhunter/fetch.py` — changes:

```python
# imports
import psycopg
from jobhunter.config import ConfigError, Settings
from jobhunter.store import db as _db
from jobhunter.store.lifecycle import Ingestor

# RunSummary: add fields (dataclass, keep frozen)
    ingested: int = 0
    db_error: str | None = None
    lock_held: bool = False
# to_dict(): add "ingested": self.ingested, "db_error": self.db_error, "lock_held": self.lock_held

# run(): new signature and body
def run(
    settings: Settings,
    *,
    store: ArchiveStore | None = None,
    fetcher: Fetcher | None = None,
    only: str | None = None,
    dry_run: bool = False,
    now: Callable[[], datetime] = utcnow,
    concurrency: int = 4,
    ingest: bool = True,
    schema: str = _db.SCHEMA,
) -> RunSummary:
    store = store or open_store(settings.archive_url)
    started = now()
    run_id = f"{iso(started).replace('-', '').replace(':', '')}-{secrets.token_hex(3)}"
    registry = load_registry(settings.registry_path)
    boards = [b for b in registry.boards if only is None or b.key == only]
    if only is not None and not boards:
        raise UnknownBoardError(f"board {only!r} is not in the registry")

    conn: _db.Conn | None = None
    db_error: str | None = None
    if ingest and not dry_run:
        dsn = settings.require_database_url()  # ConfigError propagates: the spec makes it required
        try:
            conn = _db.connect(dsn, schema=schema)
            if not _db.try_lock(conn):
                conn.close()
                return RunSummary(run_id, started, now(), registry.revision, [], lock_held=True)
            _db.init(conn, schema)
            conn.commit()
        except (psycopg.Error, OSError) as e:
            db_error = f"{type(e).__name__}: {e}"
            conn = None

    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher()
    if not dry_run:
        store.put(registry_key(registry.revision), registry.snapshot_json())

    def one(board: Board) -> BoardOutcome:
        return fetch_board(
            board, get_source(board.source), fetcher, store,
            run_id=run_id, registry_revision=registry.revision, now=now, dry_run=dry_run,
        )

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            outcomes = list(pool.map(one, boards))
    finally:
        if own_fetcher:
            fetcher.close()

    ingested = 0
    if conn is not None:
        try:
            ing = Ingestor(conn, store, drop_ratio=settings.drop_ratio)
            for o in sorted(outcomes, key=lambda o: (o.manifest.started_at, o.manifest.attempt_id)):
                if ing.ingest(o.manifest) is not None:
                    ingested += 1
            conn.commit()
        except (psycopg.Error, OSError) as e:
            conn.rollback()
            db_error = f"{type(e).__name__}: {e}"
        finally:
            try:
                _db.unlock(conn)
            finally:
                conn.close()
    return RunSummary(
        run_id=run_id, started_at=started, finished_at=now(), registry_revision=registry.revision,
        outcomes=outcomes, ingested=ingested, db_error=db_error,
    )
```

(`fetch_board` is unchanged. Existing tests that call `run(...)` without a DB must pass `ingest=False` — update `tests/test_fetch.py`'s earlier calls accordingly, and the `test_cli.py` `env` fixture sets `JOB_HUNTER_DATABASE_URL` to `TEST_DSN` plus a `monkeypatch.setattr(cli, "_schema", <fresh schema from a pg fixture>)` for the CLI fetch tests — see the CLI change below.)

`src/jobhunter/cli.py` — changes:

```python
# module-level indirection so tests can point commands at a throwaway schema
from jobhunter.store import db as _db
_schema: str = _db.SCHEMA

# fetch(): pass schema=_schema to fetch_run; catch ConfigError -> "config error: …" exit 2;
# after printing, exit 2 if summary.db_error or (boards and ok == 0);
# human output: append f"ingested {summary.ingested}" and, if db_error, f"db error: {summary.db_error}";
# if summary.lock_held: print "already running (advisory lock held); nothing fetched" and exit 0.

@app.command()
def ingest(as_json: bool = typer.Option(False, "--json")) -> None:
    """Replay archive manifests newer than the last ingested one into the store."""
    from jobhunter.ingest import replay_pending

    settings = _settings()
    store = _store(settings)
    conn = _conn(settings, schema=_schema)
    try:
        if not _db.try_lock(conn):
            typer.echo("already running (advisory lock held)")
            return
        _db.init(conn, _schema)
        conn.commit()
        s = replay_pending(conn, store, drop_ratio=settings.drop_ratio)
        conn.commit()
    except ArchiveError as e:
        typer.echo(f"archive error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    except Exception as e:  # psycopg errors, OutOfOrder
        typer.echo(f"database error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    finally:
        try:
            _db.unlock(conn)
        except Exception:
            pass
        conn.close()
    _emit({"ingested": s.ingested, "skipped": s.skipped, "last_attempt": s.last_attempt}, as_json,
          f"ingested {s.ingested}, skipped {s.skipped}, last {s.last_attempt or '-'}")
```

and `_conn(settings, schema=_db.SCHEMA)` gains the `schema` parameter passed to `_db.connect`. In `tests/test_cli.py`, extend the `env` fixture: request `pg`, read its schema (`SELECT current_schema()`), `monkeypatch.setattr(cli, "_schema", schema)`, `monkeypatch.setenv("JOB_HUNTER_DATABASE_URL", TEST_DSN)`; the existing `fetch` tests then ingest into the throwaway schema. Keep `test_fetch_requires_database_url_and_ingest_command` deleting the env var first.

- [ ] **Step 4: Verify** — `uv run pytest -q && uv run ruff check . && uv run mypy` → all pass (update the earlier fetch/CLI tests as described so they still hold).

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/fetch.py src/jobhunter/ingest.py src/jobhunter/cli.py tests/test_fetch.py tests/test_ingest.py tests/test_cli.py
git commit -m "feat: fetch ingests into Postgres under an advisory lock; ingest command replays pending manifests; DB outages still archive"
```

---

### Task 8: `rebuild`, `report`, `registry list`, `status` DB health

**Files:**
- Create: `src/jobhunter/rebuild.py`, `src/jobhunter/store/queries.py`, `tests/test_rebuild.py`, `tests/store/test_queries.py`
- Modify: `src/jobhunter/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `rebuild.rebuild(store, dsn, *, drop_ratio, schema=SCHEMA, work_schema=None) -> RebuildSummary(ingested, skipped, work_schema, swapped: bool)` — connects with `schema=work_schema` (default `f"{schema}_new"`), drops it if present, `init`, `replay_pending`, then `swap_schema(new=work_schema, target=schema, previous=f"{schema}_previous")`. Holds the advisory lock throughout; raises `RuntimeError("already running")` if held.
  - `store.queries.events_since(conn, since: datetime) -> list[dict]` (kind, uid, at, title, company, url, closed_lower_at, closed_upper_at, from/to versions; joined with `postings` and the current `posting_versions`), `store.queries.panel_rows(conn) -> list[dict]`, `store.queries.board_health(conn) -> dict[str, dict]` (latest attempt per board: `health`, `observed_count`, `started_at`), `store.queries.open_counts(conn) -> dict[str, int]` (open postings per `source:board`).
  - CLI: `rebuild [--json]`, `report [--since 24h] [--json]` (durations `Nh`, `Nd`, `Nm`), `registry list [--json]` (panel history), `status` gains `health`/`open` columns when a DB is configured and reachable (unchanged when not).

- [ ] **Step 1: Failing tests**

`tests/test_rebuild.py`:

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from jobhunter.archive.local import LocalFS
from jobhunter.models import Board
from jobhunter.rebuild import rebuild
from jobhunter.store import db
from tests.conftest import TEST_DSN
from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry


def test_rebuild_builds_in_work_schema_and_swaps(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    store = LocalFS(tmp_path)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    make_manifest(store, "ashby", "ramp", t0, board_payload("ashby", [ab_record("x", "T", "<p>t</p>")]),
                  registry_revision=rev)
    make_manifest(store, "ashby", "ramp", t0 + timedelta(days=1), board_payload("ashby", []),
                  registry_revision=rev)
    target = pg.execute("SELECT current_schema() AS s").fetchone()["s"]
    work, prev = f"{target}_new", f"{target}_previous"
    pg.execute("INSERT INTO schema_meta (key, value) VALUES ('marker', 'old')")
    pg.commit()
    s = rebuild(store, TEST_DSN, drop_ratio=0.5, schema=target, work_schema=work)
    assert s.swapped and s.ingested == 2 and s.work_schema == work
    check = db.connect(TEST_DSN, schema=target)
    try:
        assert check.execute("SELECT count(*) AS n FROM fetch_attempts").fetchone()["n"] == 2
        assert check.execute("SELECT value FROM schema_meta WHERE key='marker'").fetchone() is None
        assert db.schema_exists(check, prev) and not db.schema_exists(check, work)
        assert check.execute(f'SELECT value FROM "{prev}".schema_meta WHERE key=%s', ("marker",)).fetchone()["value"] == "old"
        check.execute(f'DROP SCHEMA "{prev}" CASCADE')
        check.commit()
    finally:
        check.close()
```

`tests/store/test_queries.py`:

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from jobhunter.archive.local import LocalFS
from jobhunter.models import Board
from jobhunter.store.lifecycle import Ingestor
from jobhunter.store.queries import board_health, events_since, open_counts, panel_rows
from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry


def test_queries(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    store = LocalFS(tmp_path)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    ing = Ingestor(pg, store)
    ing.ingest(make_manifest(store, "ashby", "ramp", t0,
                             board_payload("ashby", [ab_record("x", "T", "<p>t</p>"), ab_record("y", "U", "<p>u</p>")]),
                             registry_revision=rev))
    ing.ingest(make_manifest(store, "ashby", "ramp", t0 + timedelta(days=1),
                             board_payload("ashby", [ab_record("x", "T2", "<p>t</p>")]),
                             registry_revision=rev))
    pg.commit()
    ev = events_since(pg, t0 + timedelta(hours=1))
    assert [(e["kind"], e["uid"]) for e in ev] == [("changed", "ab:ramp:x"), ("closed", "ab:ramp:y")]
    assert ev[0]["title"] == "T2" and ev[0]["url"].endswith("/x")
    assert ev[1]["closed_lower_at"] == t0 and ev[1]["closed_upper_at"] == t0 + timedelta(days=1)
    assert panel_rows(pg)[0]["board"] == "ramp"
    h = board_health(pg)["ashby:ramp"]
    assert h["health"] == "ok" and h["observed_count"] == 1
    assert open_counts(pg) == {"ashby:ramp": 1}
```

Append to `tests/test_cli.py`:

```python
def test_report_and_registry_list_and_rebuild(env: Path) -> None:
    assert runner.invoke(cli.app, ["fetch"]).exit_code == 0
    r = runner.invoke(cli.app, ["report", "--since", "1d", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert data["counts"]["opened"] == 1 and data["events"][0]["kind"] == "opened"
    r = runner.invoke(cli.app, ["registry", "list", "--json"])
    assert r.exit_code == 0 and {row["board"] for row in json.loads(r.stdout)} == {"greenhouse:anthropic", "lever:palantir"}
    r = runner.invoke(cli.app, ["rebuild", "--json"])
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["swapped"] is True
    r = runner.invoke(cli.app, ["status", "--json"])
    rows = {row["board"]: row for row in json.loads(r.stdout)["boards"]}
    assert rows["greenhouse:anthropic"]["health"] == "ok" and rows["greenhouse:anthropic"]["open"] == 1


def test_report_since_parsing() -> None:
    from jobhunter.cli import _parse_since

    assert _parse_since("24h").total_seconds() == 86400
    assert _parse_since("2d").total_seconds() == 172800
    assert _parse_since("30m").total_seconds() == 1800
    with pytest.raises(typer.BadParameter):
        _parse_since("soon")
```

(add `import typer` at the top of `tests/test_cli.py`). The `env` fixture's `pg` schema is used by `rebuild` as `schema=_schema`; `rebuild` leaves a `<schema>_previous` behind — the `pg` fixture teardown drops only its own schema, so extend the fixture teardown to also `DROP SCHEMA IF EXISTS "<schema>_previous" CASCADE` and `"<schema>_new"`.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_rebuild.py tests/store/test_queries.py tests/test_cli.py -q` → import errors.

- [ ] **Step 3: Implement**

`src/jobhunter/rebuild.py`:

```python
"""Rebuild the store from the archive into a fresh schema, then swap it live."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import sql

from jobhunter.archive.base import ArchiveStore
from jobhunter.ingest import replay_pending
from jobhunter.store import db


@dataclass(slots=True)
class RebuildSummary:
    ingested: int
    skipped: int
    work_schema: str
    swapped: bool


def rebuild(
    store: ArchiveStore,
    dsn: str,
    *,
    drop_ratio: float = 0.5,
    schema: str = db.SCHEMA,
    work_schema: str | None = None,
) -> RebuildSummary:
    work = work_schema or f"{schema}_new"
    conn = db.connect(dsn, schema=work)
    try:
        if not db.try_lock(conn):
            raise RuntimeError("already running (advisory lock held)")
        try:
            with conn.transaction():
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(work)))
            db.init(conn, work)
            conn.commit()
            s = replay_pending(conn, store, drop_ratio=drop_ratio)
            conn.commit()
            db.swap_schema(conn, new=work, target=schema, previous=f"{schema}_previous")
            conn.commit()
        finally:
            db.unlock(conn)
    finally:
        conn.close()
    return RebuildSummary(ingested=s.ingested, skipped=s.skipped, work_schema=work, swapped=True)
```

`src/jobhunter/store/queries.py`:

```python
"""Read helpers for the CLI. Plain SQL over the derived tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jobhunter.store.db import Conn


def events_since(conn: Conn, since: datetime) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT e.event_id, e.kind, e.uid, e.at, e.from_version, e.to_version, "
        "e.closed_lower_at, e.closed_upper_at, v.title, v.company, v.url "
        "FROM posting_events e "
        "JOIN postings p ON p.uid = e.uid "
        "LEFT JOIN posting_versions v ON v.version_hash = COALESCE(e.to_version, p.current_version_hash) "
        "WHERE e.at >= %s ORDER BY e.event_id",
        (since,),
    ).fetchall()


def panel_rows(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT source, board, company, added_at, removed_at, registry_revision "
        "FROM panel ORDER BY source, board, added_at"
    ).fetchall()


def board_health(conn: Conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT DISTINCT ON (source, board) source, board, health, observed_count, started_at, error "
        "FROM fetch_attempts ORDER BY source, board, started_at DESC"
    ).fetchall()
    return {f"{r['source']}:{r['board']}": r for r in rows}


def open_counts(conn: Conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT source, board, count(*) AS n FROM postings WHERE status = 'open' GROUP BY source, board"
    ).fetchall()
    return {f"{r['source']}:{r['board']}": int(r["n"]) for r in rows}
```

`src/jobhunter/cli.py` — add:

```python
import re
from datetime import timedelta

_SINCE = re.compile(r"^(\d+)([mhd])$")


def _parse_since(value: str) -> timedelta:
    m = _SINCE.match(value.strip())
    if not m:
        raise typer.BadParameter("use Nm, Nh or Nd, e.g. 24h")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit == "h" else timedelta(days=n)


@app.command()
def rebuild(as_json: bool = typer.Option(False, "--json")) -> None:
    """Rebuild the store from the whole archive into a fresh schema and swap it live."""
    from jobhunter.rebuild import rebuild as _rebuild

    settings = _settings()
    store = _store(settings)
    try:
        s = _rebuild(store, settings.require_database_url(), drop_ratio=settings.drop_ratio,
                     schema=_schema)
    except ConfigError as e:
        typer.echo(f"config error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    except ArchiveError as e:
        typer.echo(f"archive error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    except Exception as e:
        typer.echo(f"database error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    _emit({"ingested": s.ingested, "skipped": s.skipped, "work_schema": s.work_schema, "swapped": s.swapped},
          as_json, f"rebuilt {s.ingested} attempts into {s.work_schema}; swapped live")


@app.command()
def report(
    since: str = typer.Option("24h", "--since", help="Window: Nm, Nh or Nd"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Opened / changed / closed / reopened postings in the window."""
    from jobhunter.store.queries import events_since

    settings = _settings()
    window = _parse_since(since)
    conn = _conn(settings, schema=_schema)
    try:
        events = events_since(conn, _now() - window)
    finally:
        conn.close()
    rows = [
        {"kind": e["kind"], "uid": e["uid"], "at": iso(e["at"]), "title": e["title"],
         "company": e["company"], "url": e["url"],
         "closed_between": [iso(e["closed_lower_at"]), iso(e["closed_upper_at"])]
         if e["closed_lower_at"] else None}
        for e in events
    ]
    counts = {k: sum(r["kind"] == k for r in rows) for k in ("opened", "changed", "closed", "reopened")}
    human = [f"since {since}: " + ", ".join(f"{v} {k}" for k, v in counts.items())]
    for r in rows:
        human.append(f"  {r['kind']:8} {r['company'] or '-':18} {r['title'] or '-'}  {r['url'] or ''}")
    _emit({"since": since, "counts": counts, "events": rows}, as_json, "\n".join(human))


@registry_app.command("list")
def registry_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """Board membership history (panel)."""
    from jobhunter.store.queries import panel_rows

    settings = _settings()
    conn = _conn(settings, schema=_schema)
    try:
        rows = panel_rows(conn)
    finally:
        conn.close()
    items = [{"board": f"{r['source']}:{r['board']}", "company": r["company"], "added_at": iso(r["added_at"]),
              "removed_at": iso(r["removed_at"]) if r["removed_at"] else None,
              "registry_revision": r["registry_revision"]} for r in rows]
    human = [f"{i['board']:32} {i['company']:20} {i['added_at']}  {i['removed_at'] or 'open'}" for i in items]
    _emit(items, as_json, "\n".join(human) or "(no panel rows)")
```

`status`: when `settings.database_url` is set, try `_db.connect`; on success merge `board_health()`/`open_counts()` into each row as `health`, `open`, `db_error: None`; on failure add `db_error` to the payload and leave rows without those keys. Never exit 2 for a DB failure in `status` (it is a report), but say so in the human output.

- [ ] **Step 4: Verify** — `uv run pytest -q && uv run ruff check . && uv run mypy` → pass.

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/rebuild.py src/jobhunter/store/queries.py src/jobhunter/cli.py tests/test_rebuild.py tests/store/test_queries.py tests/test_cli.py tests/conftest.py
git commit -m "feat: rebuild into a fresh schema + swap; report --since; registry list; status shows DB health"
```

---

### Task 9: Integration — three (four) days, rebuild == incremental

**Files:**
- Create: `tests/integration/__init__.py`, `tests/integration/test_three_days.py`

**Interfaces:**
- Consumes everything. Produces no new code — the scenario test from spec §9, driven through `fetch.run` with a day-switching `MockTransport` and a real Postgres.

- [ ] **Step 1: Write the test** — `tests/integration/test_three_days.py`:

```python
"""Spec §9 integration: four scripted days through fetch.run, then rebuild == incremental."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg

from jobhunter.archive.local import LocalFS
from jobhunter.config import Settings
from jobhunter.fetch import run
from jobhunter.http import Fetcher
from jobhunter.rebuild import rebuild
from jobhunter.store import db
from tests.conftest import TEST_DSN
from tests.store.helpers import ab_record, board_payload, gh_record, lv_record

REG = """
[[boards]]
company="Anthropic"
source="greenhouse"
board="anthropic"
[[boards]]
company="Palantir"
source="lever"
board="palantir"
[[boards]]
company="Ramp"
source="ashby"
board="ramp"
"""

T0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
GH = [gh_record(i, f"GH {i}", f"<p>gh {i}</p>") for i in range(4)]
LV = [lv_record(f"l{i}", f"LV {i}", f"<p>lv {i}</p>") for i in range(4)]
AB = [ab_record(f"a{i}", f"AB {i}", f"<p>ab {i}</p>") for i in range(2)]

DAYS: dict[int, dict[str, bytes | None]] = {
    0: {"greenhouse": board_payload("greenhouse", GH), "lever": board_payload("lever", LV),
        "ashby": board_payload("ashby", AB)},
    # day 1: GH edits one posting; Lever drops one (4->3 is not a >50% drop -> closes); Ashby unchanged
    1: {"greenhouse": board_payload("greenhouse", [gh_record(0, "GH 0 edited", "<p>gh 0</p>"), *GH[1:]]),
        "lever": board_payload("lever", LV[:3]), "ashby": board_payload("ashby", AB)},
    # day 2: Lever returns [] (suspect_drop, closes nothing); Ashby returns half (1 of 2 -> not < 50%: closes 1);
    # GH returns 500 (error attempt)
    2: {"greenhouse": None, "lever": board_payload("lever", []), "ashby": board_payload("ashby", AB[:1])},
    # day 3: Lever [] again (ok -> closes remaining 3 with lower bound day 1); GH back with 2 (2 < 0.5*4? no: 2 -> ok, closes 2)
    3: {"greenhouse": board_payload("greenhouse", GH[:2]), "lever": board_payload("lever", []),
        "ashby": board_payload("ashby", AB[:1])},
}


def _handler_for(day: int) -> Any:
    def h(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        src = "greenhouse" if "greenhouse" in host else "lever" if "lever" in host else "ashby"
        body = DAYS[day][src]
        return httpx.Response(500, content=b"down") if body is None else httpx.Response(200, content=body)
    return h


def _fetcher(day: int) -> Fetcher:
    return Fetcher(httpx.Client(transport=httpx.MockTransport(_handler_for(day))), sleep=lambda s: None)


TABLES = ["fetch_attempts", "posting_versions", "documents", "presence", "runs", "panel", "postings",
          "posting_events", "schema_meta"]


def _dump(conn: psycopg.Connection[dict[str, Any]], schema: str) -> dict[str, list[tuple[Any, ...]]]:
    out: dict[str, list[tuple[Any, ...]]] = {}
    for t in TABLES:
        cols = [r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
            "ORDER BY ordinal_position", (schema, t)).fetchall()]
        if t == "schema_meta":
            cols = ["key", "value"]
        rows = conn.execute(f'SELECT {", ".join(cols)} FROM "{schema}".{t}').fetchall()
        out[t] = sorted(tuple(str(r[c]) for c in cols) for r in rows)
    return out


def test_four_days_then_rebuild_matches(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    (tmp_path / "companies.toml").write_text(REG)
    settings = Settings(archive_url=f"file://{tmp_path / 'archive'}", registry_path=tmp_path / "companies.toml",
                        home=tmp_path, database_url=TEST_DSN, drop_ratio=0.5)
    schema = pg.execute("SELECT current_schema() AS s").fetchone()["s"]
    summaries = []
    for day in range(4):
        t = T0 + timedelta(days=day)
        summaries.append(run(settings, fetcher=_fetcher(day), now=lambda t=t: t, schema=schema))
    assert all(s.db_error is None for s in summaries)

    events = pg.execute("SELECT kind, uid, at FROM posting_events ORDER BY event_id").fetchall()
    kinds = [(e["kind"], e["uid"]) for e in events]
    assert kinds[:10] == [("opened", f"gh:anthropic:{i}") for i in range(4)] + \
        [("opened", f"lv:palantir:l{i}") for i in range(4)] + [("opened", "ab:ramp:a0"), ("opened", "ab:ramp:a1")] \
        or sorted(kinds[:10]) == sorted([("opened", f"gh:anthropic:{i}") for i in range(4)]
                                        + [("opened", f"lv:palantir:l{i}") for i in range(4)]
                                        + [("opened", "ab:ramp:a0"), ("opened", "ab:ramp:a1")])
    day1 = [k for k in kinds[10:] if k[0] in ("changed", "closed")][:2]
    assert ("changed", "gh:anthropic:0") in day1 and ("closed", "lv:palantir:l3") in day1
    healths = {(r["board"], r["started_at"].day - T0.day): r["health"] for r in
               pg.execute("SELECT board, started_at, health FROM fetch_attempts").fetchall()}
    assert healths[("palantir", 2)] == "suspect_drop" and healths[("palantir", 3)] == "ok"
    assert healths[("anthropic", 2)] == "error" and healths[("ramp", 2)] == "ok"
    lever_closed = pg.execute(
        "SELECT uid, closed_lower_at, closed_upper_at FROM postings WHERE source='lever' AND status='closed' ORDER BY uid"
    ).fetchall()
    assert len(lever_closed) == 4
    l0 = [r for r in lever_closed if r["uid"] == "lv:palantir:l0"][0]
    assert l0["closed_lower_at"] == T0 + timedelta(days=1) and l0["closed_upper_at"] == T0 + timedelta(days=3)
    assert pg.execute("SELECT count(*) AS n FROM postings WHERE status='open'").fetchone()["n"] == 3  # gh0, gh1, a0
    assert pg.execute("SELECT count(*) AS n FROM posting_versions").fetchone()["n"] == 11  # 4+4+2 + gh0 edit
    incremental = _dump(pg, schema)

    work = f"{schema}_new"
    s = rebuild(LocalFS(tmp_path / "archive"), TEST_DSN, drop_ratio=0.5, schema=schema, work_schema=work)
    assert s.swapped and s.ingested == 12
    check = db.connect(TEST_DSN, schema=schema)
    try:
        rebuilt = _dump(check, schema)
        check.execute(f'DROP SCHEMA IF EXISTS "{schema}_previous" CASCADE')
        check.commit()
    finally:
        check.close()
    for t in TABLES:
        assert rebuilt[t] == incremental[t], f"table {t} differs after rebuild"
```

- [ ] **Step 2: Run** — `uv run pytest tests/integration -q`. Expected: pass. If the event-order assertion is brittle because of thread scheduling across boards within a run, keep the per-board order assertions and drop the cross-board one; the rebuild-equality assertion is the one that matters and must hold exactly (ingest sorts attempts by `started_at, attempt_id`, so it is deterministic).

- [ ] **Step 3: Verify whole suite** — `uv run pytest -q && uv run ruff check . && uv run mypy`.

- [ ] **Step 4: Commit**

```bash
git add tests/integration
git commit -m "test: four-day lifecycle integration; rebuild reproduces the incremental store"
```

---

### Task 10: Deployment wiring and docs

**Files:**
- Modify: `.github/workflows/fetch.yml`, `docs/runbooks/2026-08-18-deploy-fetcher.md`, `README.md`, `docs/README.md`
- Create: none

- [ ] **Step 1: `fetch.yml`** — add `JOB_HUNTER_DATABASE_URL: ${{ secrets.JOB_HUNTER_DATABASE_URL }}` to the `env` of both the `fetch` and `status` steps (secrets remain step-scoped). Add a step after `fetch`:

```yaml
      - name: ingest pending (repair path if a previous run archived but could not ingest)
        if: always()
        env:
          JOB_HUNTER_ARCHIVE_URL: ${{ vars.JOB_HUNTER_ARCHIVE_URL }}
          AWS_ENDPOINT_URL: ${{ vars.R2_ENDPOINT_URL }}
          AWS_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
          AWS_DEFAULT_REGION: auto
          JOB_HUNTER_DATABASE_URL: ${{ secrets.JOB_HUNTER_DATABASE_URL }}
        run: uv run job-hunter ingest
```

- [ ] **Step 2: Runbook** — insert before "First run": a "Neon" step: create a Neon project (Postgres 17), one database `jobhunter`, copy the pooled connection string with `sslmode=require`; add it as the `JOB_HUNTER_DATABASE_URL` secret; run `uv run job-hunter db init` once locally against it (or let the first `fetch` do it); note the free-tier limits (0.5 GB, 100 CU-hours) and that `job-hunter status` reports DB health. Add a "Rebuild" section: `uv run job-hunter rebuild` (holds the lock; leaves `jobhunter_previous`, dropped on the next rebuild).

- [ ] **Step 3: README / docs index** — README "Running the fetcher": add `export JOB_HUNTER_DATABASE_URL=postgresql://…` and the `ingest`, `rebuild`, `report`, `registry list`, `db` commands. `docs/README.md`: add a line under "Prototype code" or a new "Code" heading pointing at `src/jobhunter/` and the two plans; note increment 2 shipped.

- [ ] **Step 4: Verify docs and everything**

```bash
prettier --parser markdown --prose-wrap always --print-width 80 --write docs/runbooks/2026-08-18-deploy-fetcher.md docs/README.md
python3 ~/.claude/skills/writing-markdown-docs/scripts/check_doc.py docs/runbooks/2026-08-18-deploy-fetcher.md
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/fetch.yml')); yaml.safe_load(open('.github/workflows/test.yml')); yaml.safe_load(open('compose.yaml'))"
uv run pytest -q && uv run ruff check . && uv run mypy
```

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/fetch.yml docs/runbooks/2026-08-18-deploy-fetcher.md README.md docs/README.md
git commit -m "chore: wire JOB_HUNTER_DATABASE_URL into the daily workflow, ingest repair step, Neon runbook, docs"
```

---

### Task 11: End-to-end verification and handoff

**Files:** none.

- [ ] **Step 1: Full suite against a fresh Postgres** — `docker compose down -v && docker compose up -d postgres && uv run pytest -q && uv run ruff check . && uv run mypy` → all pass; paste the summary lines.
- [ ] **Step 2: Local end-to-end with real boards (opt-in, network, read-only)** — with `JOB_HUNTER_ARCHIVE_URL=file:///tmp/jh-archive-2` and `JOB_HUNTER_DATABASE_URL=$TEST_DSN`: `uv run job-hunter fetch`, `uv run job-hunter status`, `uv run job-hunter report --since 1h` (expect ~900 `opened`), `uv run job-hunter rebuild`, `uv run job-hunter db version`. Then run `fetch` again immediately: `ingested 3`, `0 new blobs`, `report --since 1m` shows no events (nothing changed) — unless a board changed between the two runs, in which case a `changed` line is correct.
- [ ] **Step 3: Deploy (user-owned)** — Neon project + secret per the runbook; first `workflow_dispatch`; confirm `status` in the job log shows every board `ok` with `health ok`.

---

## Self-review against the spec

- §3.7 `version_hash` ✔ T2; `canonical_json`/`sha256_hex` from increment 1. §3.8 markdown ✔ T3 (custom stdlib converter, NFKC, goldens, text preservation). §3.9 store: `schema.sql`, `db.py` (`connect/init/schema_version/advisory_lock/swap_schema`), `lifecycle.py` ✔ T1, T5–6; `panel.py` (§5.5) ✔ T4; `queries.py` ✔ T8. §3.10 fetch: connect → lock → registry → archive → ingest → unlock ✔ T7 (registry snapshot/panel is applied by the ingestor from the manifest's revision, which also makes rebuild reproduce it). §4 steps 1, 4, 5 ✔ T7. §5.1 posting/version/document identities ✔ T2, T5. §5.2 `versions/<ab>/<hash>.html.gz` ✔ T5. §5.3 DDL ✔ T1; presence append-mostly ✔ T5. §5.4 all seven steps ✔ T5–6 (idempotence, OutOfOrder, error attempts, per-record isolation, health verdict, transitions, reconcile, runs, meta). §5.6 unchanged (extractions reserved). §6.1 `JOB_HUNTER_DATABASE_URL`, `JOB_HUNTER_DROP_RATIO` ✔ T1. §6.2 `ingest`, `rebuild`, `report`, `registry list`, `db init|version` ✔ T7–8; exit codes ✔. §6.3 Neon secret + runbook ✔ T10. §8 DB unreachable → archive still written, exit 2 ✔ T7; overlap → lock ✔ T7; out-of-order → `OutOfOrder` ✔ T5; rebuild ✔ T8. §9 store tests on real Postgres ✔ T1–T8; integration + rebuild == incremental ✔ T9; live smoke ✔ T11. §10 item 2 ✔.
- Placeholder scan: the only intentional placeholder is `"GOLDEN"` in T2, replaced in the same task by the printed value (a plan step, not a TODO); no other TBD/TODO.
- Type consistency: `Ingestor(conn, store, *, drop_ratio, normalizer_version, to_markdown)` used identically in T5–T9; `AttemptResult` fields used in T5–T6 tests; `db.connect(dsn, schema=…)`, `db.try_lock/unlock`, `db.init(conn, schema)`, `db.swap_schema(conn, new, target, previous)` used identically in T1, T7, T8, T9; `replay_pending(conn, store, *, drop_ratio)` in T7–T8; `run(..., ingest=..., schema=...)` in T7 and T9; helper names `make_manifest/board_payload/gh_record/lv_record/ab_record/write_registry` in T5–T9.
