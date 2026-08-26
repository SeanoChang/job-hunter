"""job-hunter command line. Every command accepts --json; exit 0 normal, 2 systemic."""

from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import typer

from jobhunter import __version__
from jobhunter.archive import ArchiveError, ArchiveStore, open_store
from jobhunter.archive.manifests import iter_manifests, latest_per_board
from jobhunter.config import ConfigError, Settings
from jobhunter.fetch import UnknownBoardError, is_healthy
from jobhunter.fetch import run as fetch_run
from jobhunter.http import Fetcher
from jobhunter.registry import RegistryError
from jobhunter.registry import load as load_registry
from jobhunter.store import db as _db
from jobhunter.timeutil import iso, utcnow

EXIT_SYSTEMIC = 2

_SINCE = re.compile(r"^(\d+)([mhd])$")

app = typer.Typer(no_args_is_help=True, add_completion=False, help="job-hunter ingestion")
archive_app = typer.Typer(help="Inspect the raw archive")
registry_app = typer.Typer(help="Inspect companies.toml")
db_app = typer.Typer(help="Postgres store")
app.add_typer(archive_app, name="archive")
app.add_typer(registry_app, name="registry")
app.add_typer(db_app, name="db")


# Indirections so tests can substitute a mock transport, a fixed clock and a throwaway schema.
_schema: str = _db.SCHEMA


def _make_fetcher() -> Fetcher:
    return Fetcher()


def _now() -> datetime:
    return utcnow()


def _settings() -> Settings:
    try:
        return Settings.load()
    except ConfigError as e:
        typer.echo(f"config error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e


def _conn(settings: Settings, schema: str = _db.SCHEMA) -> _db.Conn:
    try:
        return _db.connect(settings.require_database_url(), schema=schema)
    except ConfigError as e:
        typer.echo(f"config error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    except Exception as e:  # psycopg.OperationalError and friends
        typer.echo(f"database error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e


def _store(settings: Settings) -> ArchiveStore:
    try:
        return open_store(settings.archive_url)
    except (ValueError, ArchiveError) as e:
        typer.echo(f"archive error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e


def _emit(obj: Any, as_json: bool, human: str) -> None:
    typer.echo(json.dumps(obj, indent=None) if as_json else human)


def _parse_since(value: str) -> timedelta:
    m = _SINCE.match(value.strip())
    if not m:
        raise typer.BadParameter("use Nm, Nh or Nd, e.g. 24h")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "m":
        return timedelta(minutes=n)
    return timedelta(hours=n) if unit == "h" else timedelta(days=n)


def _split_board(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    if ":" not in value:
        typer.echo("--board must look like source:board, e.g. greenhouse:anthropic")
        raise typer.Exit(EXIT_SYSTEMIC)
    src, brd = value.split(":", 1)
    return src, brd


@app.command()
def version(as_json: bool = typer.Option(False, "--json")) -> None:
    """Print the job-hunter version."""
    _emit({"version": __version__}, as_json, __version__)


@app.command()
def verify(
    extraction_file: str = typer.Argument(..., help="Extraction record JSON"),
    document_file: str = typer.Argument(..., help="Canonical markdown document"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Re-run every validator/1 check over an extraction against its document.

    Exit 0: all checks pass. Exit 1: ran fine, findings failed. Exit 2: systemic.
    """
    from jobhunter.l2 import verify as l2_verify
    from jobhunter.l2.quotes import line_col

    try:
        extraction = json.loads(Path(extraction_file).read_text(encoding="utf-8"))
        markdown = Path(document_file).read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_SYSTEMIC) from exc
    try:
        report = l2_verify(extraction, markdown)
    except (KeyError, TypeError, AttributeError, RecursionError) as exc:
        # unknown schema version, a top level that is not the record shape, or
        # pathological nesting that outruns the interpreter before any check
        typer.echo(f"error: {exc!r}", err=True)
        raise typer.Exit(EXIT_SYSTEMIC) from exc

    def _clip(s: str, n: int = 120) -> str:
        return s if len(s) <= n else s[:n] + "…"

    if as_json:
        typer.echo(json.dumps(report.to_json(), ensure_ascii=False))
    else:
        for f in report.findings:
            loc = ""
            span = f.detail.get("span")
            if isinstance(span, list):
                line, col = line_col(markdown, int(span[0]))
                loc = f"  line {line}:{col}"
            typer.echo(f"{f.severity.upper()} {f.check}:{f.code} {f.path}{loc}")
            expected, found = f.detail.get("expected"), f.detail.get("found")
            if isinstance(expected, str) and isinstance(found, str):
                typer.echo(f"  expected: {_clip(expected)!r}")
                typer.echo(f"  found:    {_clip(found)!r}")
            prefix = f.detail.get("longest_prefix")
            if isinstance(prefix, int):
                typer.echo(f"  longest matching prefix: {prefix} codepoints")
        typer.echo(f"{report.status}  ({len(report.findings)} findings)")
    if report.status == "fail":
        raise typer.Exit(1)


@app.command()
def fetch(
    board: str | None = typer.Option(None, "--board", help="Only this board, as source:board"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch but write nothing"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Fetch every registered board and archive manifests + blobs."""
    settings = _settings()
    store = _store(settings)
    _split_board(board)  # validates the source:board shape; exits 2 otherwise
    fetcher = _make_fetcher()
    try:
        summary = fetch_run(settings, store=store, fetcher=fetcher, only=board,
                            dry_run=dry_run, now=_now, schema=_schema)
    except ConfigError as e:
        typer.echo(f"config error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    except RegistryError as e:
        typer.echo(f"registry error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    except UnknownBoardError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    except ArchiveError as e:
        typer.echo(f"archive error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    finally:
        fetcher.close()
    if summary.lock_held:
        _emit(summary.to_dict(), as_json, "already running (advisory lock held); nothing fetched")
        return
    counts = summary.counts()
    lines = [f"run {summary.run_id} — {counts['ok']}/{counts['boards']} boards ok, "
             f"{counts['new_blobs']} new blobs, {summary.ingested} ingested"
             + (f", {summary.replayed} replayed" if summary.replayed else "")
             + (f", GAPS: {len(summary.gaps)} (run rebuild)" if summary.gaps else "")
             + (" (dry run)" if dry_run else "")]
    for o in summary.outcomes:
        m = o.manifest
        detail = f"{m.record_count} records" if is_healthy(m) else (m.error or "")
        lines.append(f"  {o.board.key:32} {m.transport:11} {m.http_status or '-':>4}  {detail}")
    if summary.db_error:
        lines.append(f"db error: {summary.db_error} (the archive was still written)")
    _emit(summary.to_dict(), as_json, "\n".join(lines))
    if summary.db_error or (counts["boards"] and counts["ok"] == 0):
        raise typer.Exit(EXIT_SYSTEMIC)


@app.command()
def ingest(as_json: bool = typer.Option(False, "--json")) -> None:
    """Replay archive manifests newer than the last ingested one into the store."""
    from jobhunter.ingest import replay_pending

    settings = _settings()
    store = _store(settings)
    conn = _conn(settings, schema=_schema)
    try:
        if not _db.try_lock(conn):
            _emit({"lock_held": True, "ingested": 0, "skipped": 0, "last_attempt": None}, as_json,
                  "already running (advisory lock held); nothing ingested")
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
        # Unlocking a dead connection must not mask the error that killed it.
        with contextlib.suppress(Exception):
            _db.unlock(conn)
        conn.close()
    hint = (
        "archive has manifests behind the watermark that are missing from the store; "
        "run `job-hunter rebuild` to repair"
    ) if s.gaps else None
    _emit(
        {"ingested": s.ingested, "skipped": s.skipped, "last_attempt": s.last_attempt,
         "gaps": s.gaps, "hint": hint},
        as_json,
        f"ingested {s.ingested}, skipped {s.skipped}, last {s.last_attempt or '-'}"
        + (f"\nGAPS: {len(s.gaps)} manifest(s) missing from the store — {hint}" if s.gaps else ""),
    )
    if s.gaps:
        raise typer.Exit(EXIT_SYSTEMIC)


@app.command()
def rebuild(as_json: bool = typer.Option(False, "--json")) -> None:
    """Rebuild the store from the whole archive into a fresh schema and swap it live."""
    from jobhunter.rebuild import LockHeld
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
    except LockHeld as e:  # another writer holds the advisory lock; not an error
        _emit({"lock_held": True, "ingested": 0, "skipped": 0, "swapped": False}, as_json,
              f"{e}; nothing rebuilt")
        return
    except Exception as e:
        typer.echo(f"database error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    _emit({"ingested": s.ingested, "skipped": s.skipped, "work_schema": s.work_schema,
           "swapped": s.swapped},
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
    except Exception as e:
        typer.echo(f"database error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    finally:
        conn.close()
    rows = [
        {"kind": e["kind"], "uid": e["uid"], "at": iso(e["at"]), "title": e["title"],
         "company": e["company"], "url": e["url"],
         "closed_between": [iso(e["closed_lower_at"]), iso(e["closed_upper_at"])]
         if e["closed_lower_at"] else None}
        for e in events
    ]
    counts = {k: sum(r["kind"] == k for r in rows) for k in ("opened", "changed", "closed",
                                                            "reopened")}
    human = [f"since {since}: " + ", ".join(f"{v} {k}" for k, v in counts.items())]
    for r in rows:
        human.append(
            f"  {r['kind']:8} {r['company'] or '-':18} {r['title'] or '-'}  {r['url'] or ''}"
        )
    _emit({"since": since, "counts": counts, "events": rows}, as_json, "\n".join(human))


@app.command()
def status(as_json: bool = typer.Option(False, "--json")) -> None:
    """Per-board fetch health from the archive, plus store health when a DB is configured."""
    settings = _settings()
    store = _store(settings)
    try:
        registry = load_registry(settings.registry_path)
        latest = latest_per_board(store)
    except RegistryError as e:
        typer.echo(f"registry error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    except ArchiveError as e:
        typer.echo(f"archive error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    rows: list[dict[str, Any]] = []
    for b in registry.boards:
        m = latest.get(b.key)
        rows.append({
            "board": b.key,
            "last_attempt": iso(m.started_at) if m else None,
            "last_transport": m.transport if m else None,
            "http_status": m.http_status if m else None,
            "record_count": m.record_count if m else None,
            "error": m.error if m else None,
        })
    untracked = sorted(set(latest) - {b.key for b in registry.boards})
    payload: dict[str, Any] = {"boards": rows, "untracked": untracked}
    if settings.database_url:
        db_error, db_size = _merge_store_health(settings, rows)
        payload["db_error"] = db_error
        if db_size is not None:
            payload["db_size_bytes"] = db_size
    else:
        db_error = None
    show_db = settings.database_url is not None and db_error is None
    header = "board                            last attempt          transport   status  records"
    human = [header + ("  health    open" if show_db else "")]
    for r in rows:
        line = (
            f"{r['board']:32} {r['last_attempt'] or 'never':21} "
            f"{r['last_transport'] or '-':11} {r['http_status'] or '-':>6}  "
            f"{r['record_count'] if r['record_count'] is not None else '-':>7}"
        )
        if show_db:
            line += f"  {r['health'] or '-':8} {r['open']:>5}"
        if r["error"]:
            line += f"  {r['error']}"
        human.append(line)
    if db_error:
        human.append(f"db error: {db_error} (archive health above is still current)")
    elif settings.database_url and payload.get("db_size_bytes") is not None:
        human.append(f"db size: {payload['db_size_bytes'] / 1e6:.1f} MB")
    if untracked:
        human.append(f"not in registry but present in archive: {', '.join(untracked)}")
    _emit(payload, as_json, "\n".join(human))


def _merge_store_health(
    settings: Settings, rows: list[dict[str, Any]]
) -> tuple[str | None, int | None]:
    """Add `health`/`open` to each row from the store.

    Returns the error if the DB is unreachable, else `(None, database size in bytes)`."""
    from jobhunter.store.queries import board_health, database_size, open_counts

    try:
        conn = _db.connect(settings.require_database_url(), schema=_schema)
    except Exception as e:  # status is a report: an unreachable DB is noted, never fatal
        return f"{type(e).__name__}: {e}", None
    try:
        health = board_health(conn)
        opens = open_counts(conn)
        size = database_size(conn)
    except Exception as e:
        return f"{type(e).__name__}: {e}", None
    finally:
        conn.close()
    for r in rows:
        h = health.get(r["board"])
        r["health"] = h["health"] if h else None
        r["open"] = opens.get(r["board"], 0)
    return None, size


@archive_app.command("ls")
def archive_ls(
    board: str | None = typer.Option(None, "--board", help="Filter, as source:board"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List attempts (manifests) in the archive."""
    settings = _settings()
    store = _store(settings)
    src, brd = _split_board(board)
    try:
        items = [
            {
                "attempt_id": m.attempt_id, "board": f"{m.source}:{m.board}",
                "started_at": iso(m.started_at), "transport": m.transport,
                "http_status": m.http_status, "payload_bytes": m.payload_bytes,
                "record_count": m.record_count, "blob_sha256": m.blob_sha256,
            }
            for m in iter_manifests(store, src, brd)
        ]
    except ArchiveError as e:
        typer.echo(f"archive error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    human = [f"{i['started_at']}  {i['board']:28} {i['transport']:11} "
             f"{i['payload_bytes']:>9}B  "
             f"{i['record_count'] if i['record_count'] is not None else '-'}"
             for i in items]
    _emit(items, as_json, "\n".join(human) or "(no attempts)")


@registry_app.command("check")
def registry_check(as_json: bool = typer.Option(False, "--json")) -> None:
    """Validate companies.toml and print its revision."""
    settings = _settings()
    try:
        reg = load_registry(settings.registry_path)
    except (RegistryError, OSError) as e:
        _emit({"ok": False, "error": str(e)}, as_json, f"registry error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    _emit(
        {"ok": True, "boards": [b.key for b in reg.boards], "revision": reg.revision},
        as_json,
        f"ok: {len(reg.boards)} boards, revision {reg.revision[:12]}",
    )


@registry_app.command("list")
def registry_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """Board membership history (panel)."""
    from jobhunter.store.queries import panel_rows

    settings = _settings()
    conn = _conn(settings, schema=_schema)
    try:
        rows = panel_rows(conn)
    except Exception as e:
        typer.echo(f"database error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    finally:
        conn.close()
    items = [{"board": f"{r['source']}:{r['board']}", "company": r["company"],
              "added_at": iso(r["added_at"]),
              "removed_at": iso(r["removed_at"]) if r["removed_at"] else None,
              "registry_revision": r["registry_revision"]} for r in rows]
    human = [f"{i['board']:32} {i['company']:20} {i['added_at']}  {i['removed_at'] or 'open'}"
             for i in items]
    _emit(items, as_json, "\n".join(human) or "(no panel rows)")


@db_app.command("init")
def db_init(as_json: bool = typer.Option(False, "--json")) -> None:
    """Create the jobhunter schema and tables (idempotent)."""
    settings = _settings()
    conn = _conn(settings, schema=_schema)
    try:
        _db.init(conn, _schema)
        conn.commit()
        payload = {"schema": _schema, "schema_version": _db.stored_schema_version(conn)}
    finally:
        conn.close()
    _emit(
        payload,
        as_json,
        f"schema {payload['schema']} ready, version {payload['schema_version']}",
    )


@db_app.command("version")
def db_version(as_json: bool = typer.Option(False, "--json")) -> None:
    """Print the code's schema version and the database's; exit 2 on mismatch."""
    settings = _settings()
    conn = _conn(settings, schema=_schema)
    try:
        stored = None
        if _db.schema_exists(conn, _schema):
            try:
                stored = _db.stored_schema_version(conn)
            except psycopg.errors.UndefinedTable:  # half-created schema: no schema_meta yet
                conn.rollback()
                stored = None
    finally:
        conn.close()
    payload = {"code": _db.SCHEMA_VERSION, "db": stored, "match": stored == _db.SCHEMA_VERSION}
    _emit(payload, as_json, f"code {payload['code']}  db {stored or 'absent'}")
    if not payload["match"]:
        raise typer.Exit(EXIT_SYSTEMIC)


if __name__ == "__main__":
    app()
