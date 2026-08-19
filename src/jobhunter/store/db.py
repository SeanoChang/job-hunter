"""Connection, schema lifecycle, advisory lock, schema swap. No business logic."""

from __future__ import annotations

from importlib import resources
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


class SchemaMismatch(RuntimeError):
    """The database's stored schema_version differs from the code's; run `rebuild`."""


SCHEMA = "jobhunter"
SCHEMA_VERSION = "1"
LOCK_KEY = 0x6A6F6268  # "jobh"

Conn = psycopg.Connection[dict[str, Any]]


def connect(dsn: str, *, schema: str = SCHEMA) -> Conn:
    conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row, connect_timeout=30)
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
    """Create the schema and all tables if absent; verify schema_version. Idempotent.

    Raises SchemaMismatch when the schema already stores a different version — write
    paths must refuse rather than silently extend an old shape (spec §6.2). On DDL
    failure the ORIGINAL error propagates (the search_path restore is shielded so it
    cannot mask it with InFailedSqlTransaction). Like all store helpers, the work
    joins the caller's transaction; the caller commits.
    """
    import contextlib

    row = conn.execute("SHOW search_path").fetchone()
    previous_path = str(row["search_path"]) if row else "public"
    stored: str | None = None
    ok = False
    try:
        with conn.transaction():
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
            conn.execute(load_schema_sql())
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', %s) "
                "ON CONFLICT (key) DO NOTHING",
                (SCHEMA_VERSION,),
            )
            stored_row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            stored = str(stored_row["value"]) if stored_row else None
        ok = True
    finally:
        if not ok:
            # Clear the aborted transaction so the restore (and the caller's next
            # statement) is not answered with InFailedSqlTransaction.
            with contextlib.suppress(psycopg.Error):
                conn.rollback()
        with contextlib.suppress(psycopg.Error):
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.SQL(previous_path)))
    if stored != SCHEMA_VERSION:
        raise SchemaMismatch(
            f"database schema_version {stored!r} != code {SCHEMA_VERSION!r}; "
            "run `job-hunter rebuild`"
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


def swap_schema(
    conn: Conn, new: str, target: str = SCHEMA, previous: str = "jobhunter_previous"
) -> None:
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
            sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                sql.Identifier(new), sql.Identifier(target)
            )
        )
