"""Connection, schema lifecycle, advisory lock, schema swap. No business logic."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any, LiteralString

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


class SchemaMismatch(RuntimeError):
    """The database's stored schema_version differs from the code's; run `rebuild`."""


SCHEMA = "jobhunter"
SCHEMA_VERSION = "4"
# stored -> code versions where schema.sql's idempotent DDL is the whole
# migration (purely additive changes); anything else still demands `rebuild`
_ADDITIVE_UPGRADES = {
    ("1", "2"), ("2", "3"), ("1", "3"),
    ("3", "4"), ("2", "4"), ("1", "4"),
}
LOCK_KEY = 0x6A6F6268  # "jobh" — ingestion writer
EXTRACT_LOCK_KEY = 0x6A6F6232  # "job2" — extraction writer (harness spec §4.6)

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
        if (stored, SCHEMA_VERSION) in _ADDITIVE_UPGRADES:
            conn.execute(
                sql.SQL(
                    "UPDATE {}.schema_meta SET value = %s WHERE key = 'schema_version'"
                ).format(sql.Identifier(schema)),
                (SCHEMA_VERSION,),
            )
            return
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


# Privilege names are keywords pasted into the GRANT, not parameters bound to it,
# so the set the catalog is allowed to name is closed by hand.
_PRIVILEGE_NAMES: tuple[LiteralString, ...] = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER",
    "MAINTAIN", "USAGE", "CREATE",
)
_PRIVILEGES: dict[str, sql.SQL] = {name: sql.SQL(name) for name in _PRIVILEGE_NAMES}


@dataclass(frozen=True, slots=True)
class Grant:
    """One role's privileges on a schema (`table is None`) or on one of its
    relations, in a form that can be replayed onto a different schema.

    Refuses a privilege this module cannot spell, so a rebuild that could not
    reproduce the live schema's access says so while reading it, rather than
    swapping a half-privileged schema into place.
    """

    grantee: str  # the empty string is PUBLIC
    privileges: tuple[str, ...]
    table: str | None

    def __post_init__(self) -> None:
        unknown = [p for p in self.privileges if p not in _PRIVILEGES]
        if unknown:
            raise ValueError(
                f"privilege(s) {', '.join(unknown)} on {self.table or 'the schema'} "
                "cannot be carried across a rebuild; grant them by hand afterwards"
            )


_SCHEMA_ACL_SQL = """
SELECT CASE WHEN a.grantee = 0 THEN '' ELSE pg_get_userbyid(a.grantee) END AS grantee,
       a.privilege_type AS privilege
  FROM pg_namespace n, aclexplode(n.nspacl) a
 WHERE n.nspname = %s AND a.grantee <> n.nspowner
"""

_RELATION_ACL_SQL = """
SELECT c.relname AS relation,
       CASE WHEN a.grantee = 0 THEN '' ELSE pg_get_userbyid(a.grantee) END AS grantee,
       a.privilege_type AS privilege
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a
 WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
   AND a.grantee <> c.relowner
"""


def capture_grants(conn: Conn, schema: str) -> list[Grant]:
    """Every privilege `schema` and its relations hand to someone other than the owner.

    `rebuild` swaps in a schema built from scratch, and a fresh schema carries no
    ACL at all — without replaying these, `jobhunter_ro` and `jobhunter_mcp` lose
    even USAGE and every read answers `permission denied` (spec 2026-09-02 §3).
    Empty for a schema that does not exist, or that only its owner can touch.

    Default privileges (`ALTER DEFAULT PRIVILEGES`) are keyed to the schema object
    rather than its name and are deliberately not carried: every relation the store
    has is granted explicitly here instead.
    """
    grouped: dict[tuple[str | None, str], set[str]] = defaultdict(set)
    for row in conn.execute(_SCHEMA_ACL_SQL, (schema,)).fetchall():
        grouped[(None, str(row["grantee"]))].add(str(row["privilege"]))
    for row in conn.execute(_RELATION_ACL_SQL, (schema,)).fetchall():
        grouped[(str(row["relation"]), str(row["grantee"]))].add(str(row["privilege"]))
    return [
        Grant(grantee=grantee, privileges=tuple(sorted(privileges)), table=table)
        for (table, grantee), privileges in sorted(
            grouped.items(), key=lambda item: (item[0][0] or "", item[0][1])
        )
    ]


def apply_grants(conn: Conn, schema: str, grants: Sequence[Grant]) -> int:
    """Replay `grants` onto `schema`; returns how many were applied.

    Grants naming a relation the schema no longer has are skipped — schema versions
    retire tables as well as add them, and a stale grant must not fail a rebuild.
    Joins the caller's transaction; the caller commits.
    """
    present = {
        str(row["relname"])
        for row in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s",
            (schema,),
        ).fetchall()
    }
    applied = 0
    for grant in grants:
        if grant.table is not None and grant.table not in present:
            continue
        target = (
            sql.SQL("SCHEMA {}").format(sql.Identifier(schema))
            if grant.table is None
            else sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(grant.table))
        )
        conn.execute(
            sql.SQL("GRANT {} ON {} TO {}").format(
                sql.SQL(", ").join(_PRIVILEGES[p] for p in grant.privileges),
                target,
                sql.SQL("PUBLIC") if grant.grantee == "" else sql.Identifier(grant.grantee),
            )
        )
        applied += 1
    return applied


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
