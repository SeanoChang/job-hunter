"""job-hunter command line.

Every command speaks the contract in `cli_output`: one JSON envelope when
stdout is piped, human text on a TTY, `-o/--output` to force either. Exit codes
are the typed table in `cli_output.Exit` (0 ok, 1 verify findings, 2 usage,
3 config, 4 not found, 5 backend, 6 systemic).
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import typer

from jobhunter import __version__
from jobhunter.archive import ArchiveError, ArchiveStore, open_store
from jobhunter.archive.manifests import iter_manifests, latest_per_board
from jobhunter.cli_output import Exit, emit, fail, output_option, use_json
from jobhunter.config import ConfigError, Settings
from jobhunter.fetch import UnknownBoardError, is_healthy
from jobhunter.fetch import run as fetch_run
from jobhunter.http import Fetcher
from jobhunter.registry import RegistryError
from jobhunter.registry import load as load_registry
from jobhunter.store import db as _db
from jobhunter.timeutil import iso, utcnow

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


def _settings(output: str | None) -> Settings:
    try:
        return Settings.load()
    except ConfigError as e:
        fail("config", f"config error: {e}", code=Exit.CONFIG, output=output,
             hint="run `job-hunter doctor` once it lands; env vars are listed in README.md")


def _conn(settings: Settings, schema: str = _db.SCHEMA, *, output: str | None = None) -> _db.Conn:
    try:
        return _db.connect(settings.require_database_url(), schema=schema)
    except ConfigError as e:
        fail("config", f"config error: {e}", code=Exit.CONFIG, output=output)
    except Exception as e:  # psycopg.OperationalError and friends
        fail("backend", f"database error: {e}", code=Exit.BACKEND, output=output,
             hint="is Postgres reachable? check JOB_HUNTER_DATABASE_URL")


def _store(settings: Settings, output: str | None = None) -> ArchiveStore:
    try:
        return open_store(settings.archive_url)
    except (ValueError, ArchiveError) as e:
        fail("backend", f"archive error: {e}", code=Exit.BACKEND, output=output)


def _parse_since(value: str) -> timedelta:
    m = _SINCE.match(value.strip())
    if not m:
        raise typer.BadParameter("use Nm, Nh or Nd, e.g. 24h")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "m":
        return timedelta(minutes=n)
    return timedelta(hours=n) if unit == "h" else timedelta(days=n)


def _split_board(value: str | None, output: str | None = None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    if ":" not in value:
        fail("usage", f"--board must look like source:board, got {value!r}",
             code=Exit.USAGE, output=output, hint="e.g. greenhouse:anthropic")
    src, brd = value.split(":", 1)
    return src, brd


@app.command()
def version(output: str | None = output_option()) -> None:
    """Print the job-hunter version."""
    emit({"version": __version__}, human=__version__, output=output)


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _resolve_doc(conn: _db.Conn, prefix: str, output: str | None = None) -> str:
    """Accept any unambiguous document_hash prefix.

    Listings print a 12-char prefix, so the CLI must accept back what it
    prints; requiring all 64 characters made every printed id unusable.
    """
    if _HEX64.match(prefix):
        return prefix
    if not re.fullmatch(r"[0-9a-f]{4,64}", prefix):
        fail("usage", f"{prefix!r} is not a document_hash or hex prefix",
             code=Exit.USAGE, output=output, hint="give 4-64 lowercase hex characters")
    rows = conn.execute(
        "SELECT DISTINCT document_hash FROM documents WHERE document_hash LIKE %s LIMIT 10",
        (prefix + "%",),
    ).fetchall()
    if not rows:
        fail("not_found", f"no document matches {prefix!r}", code=Exit.NOT_FOUND, output=output)
    if len(rows) > 1:
        fail("not_found", f"{prefix!r} is ambiguous ({len(rows)} documents)",
             code=Exit.NOT_FOUND, output=output, hint="lengthen the prefix")
    return str(rows[0]["document_hash"])


def _load_stored_extraction(document_hash: str, output: str | None) -> tuple[dict[str, Any], str]:
    """Store-addressed verify: markdown from documents, record from the chosen
    attempt's archive object."""
    from jobhunter.l2.attempts import from_bytes
    from jobhunter.markdown import NORMALIZER_VERSION
    from jobhunter.store import extraction as xstore

    settings = _settings(output)
    store = _store(settings, output)
    conn = _conn(settings, schema=_schema, output=output)
    try:
        document_hash = _resolve_doc(conn, document_hash, output)
        markdown = xstore.markdown_for(conn, document_hash, NORMALIZER_VERSION)
        if markdown is None:
            fail("not_found", f"no document {document_hash} under {NORMALIZER_VERSION}",
                 code=Exit.NOT_FOUND, output=output)
        row = conn.execute(
            "SELECT chosen_attempt FROM extractions WHERE document_hash=%s"
            " AND chosen_attempt IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
            (document_hash,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        fail("not_found", f"no extraction with a chosen attempt for {document_hash}",
             code=Exit.NOT_FOUND, output=output,
             hint=f"run: job-hunter extract run --doc {document_hash}")
    try:
        attempt = from_bytes(store.get(row["chosen_attempt"]))
    except (KeyError, OSError, ValueError, EOFError, ArchiveError) as exc:
        # missing object, unreachable backend, or corrupt gzip/JSON: systemic
        fail("systemic", f"cannot load {row['chosen_attempt']}: {exc!r}",
             code=Exit.SYSTEMIC, output=output)
    if attempt.record is None:
        fail("systemic", "chosen attempt carries no record", code=Exit.SYSTEMIC, output=output)
    return attempt.record, markdown


def _verify_output(report: Any, markdown: str, output: str | None) -> None:
    from jobhunter.l2.quotes import line_col

    def _clip(s: str, n: int = 120) -> str:
        return s if len(s) <= n else s[:n] + "…"

    lines: list[str] = []
    for f in report.findings:
        loc = ""
        span = f.detail.get("span")
        if isinstance(span, list):
            line, col = line_col(markdown, int(span[0]))
            loc = f"  line {line}:{col}"
        lines.append(f"{f.severity.upper()} {f.check}:{f.code} {f.path}{loc}")
        expected, found = f.detail.get("expected"), f.detail.get("found")
        if isinstance(expected, str) and isinstance(found, str):
            lines.append(f"  expected: {_clip(expected)!r}")
            lines.append(f"  found:    {_clip(found)!r}")
        divergence = f.detail.get("divergence")
        if isinstance(divergence, str):
            lines.append(f"  {divergence}")
    lines.append(f"{report.status}  ({len(report.findings)} findings)")
    emit(report.to_json(), human="\n".join(lines), output=output)
    if report.status == "fail":
        raise typer.Exit(int(Exit.FINDINGS))


@app.command()
def verify(
    extraction_file: str = typer.Argument(
        ..., help="Extraction record JSON file, or a 64-hex document_hash"
    ),
    document_file: str | None = typer.Argument(None, help="Canonical markdown document"),
    output: str | None = output_option(),
) -> None:
    """Re-run every validator check over an extraction against its document.

    Exit 0: all checks pass. Exit 1: ran fine, findings failed. Exit 6: systemic.
    """
    from jobhunter.l2 import verify as l2_verify

    looks_like_hash = re.fullmatch(r"[0-9a-f]{4,64}", extraction_file) is not None
    if document_file is None and looks_like_hash:
        extraction, markdown = _load_stored_extraction(extraction_file, output)
    else:
        if document_file is None:
            fail("usage",
                 "give a document_hash (or unambiguous hex prefix), "
                 "or an extraction file plus its DOCUMENT_FILE",
                 code=Exit.USAGE, output=output)
        try:
            extraction = json.loads(Path(extraction_file).read_text(encoding="utf-8"))
            markdown = Path(document_file).read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            fail("systemic", f"{exc}", code=Exit.SYSTEMIC, output=output)
    try:
        report = l2_verify(extraction, markdown)
    except (KeyError, TypeError, AttributeError, RecursionError) as exc:
        # unknown schema version, a top level that is not the record shape, or
        # pathological nesting that outruns the interpreter before any check
        fail("systemic", f"{exc!r}", code=Exit.SYSTEMIC, output=output)
    _verify_output(report, markdown, output)


@app.command()
def fetch(
    board: str | None = typer.Option(None, "--board", help="Only this board, as source:board"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch but write nothing"),
    output: str | None = output_option(),
) -> None:
    """Fetch every registered board and archive manifests + blobs."""
    settings = _settings(output)
    store = _store(settings, output)
    _split_board(board, output)  # validates the source:board shape; exits 2 otherwise
    fetcher = _make_fetcher()
    try:
        summary = fetch_run(settings, store=store, fetcher=fetcher, only=board,
                            dry_run=dry_run, now=_now, schema=_schema)
    except ConfigError as e:
        fail("config", f"config error: {e}", code=Exit.CONFIG, output=output)
    except RegistryError as e:
        fail("systemic", f"registry error: {e}", code=Exit.SYSTEMIC, output=output)
    except UnknownBoardError as e:
        fail("systemic", str(e), code=Exit.SYSTEMIC, output=output,
             hint="list registered boards with: job-hunter registry check")
    except ArchiveError as e:
        fail("backend", f"archive error: {e}", code=Exit.BACKEND, output=output)
    finally:
        fetcher.close()
    if summary.lock_held:
        emit(summary.to_dict(), output=output,
             human="already running (advisory lock held); nothing fetched")
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
    emit(summary.to_dict(), human="\n".join(lines), output=output)
    if summary.db_error or (counts["boards"] and counts["ok"] == 0):
        raise typer.Exit(int(Exit.SYSTEMIC))


@app.command()
def ingest(output: str | None = output_option()) -> None:
    """Replay archive manifests newer than the last ingested one into the store."""
    from jobhunter.ingest import replay_pending

    settings = _settings(output)
    store = _store(settings, output)
    conn = _conn(settings, schema=_schema, output=output)
    try:
        if not _db.try_lock(conn):
            emit({"lock_held": True, "ingested": 0, "skipped": 0, "last_attempt": None},
                 human="already running (advisory lock held); nothing ingested", output=output)
            return
        _db.init(conn, _schema)
        conn.commit()
        s = replay_pending(conn, store, drop_ratio=settings.drop_ratio)
        conn.commit()
    except ArchiveError as e:
        fail("backend", f"archive error: {e}", code=Exit.BACKEND, output=output)
    except Exception as e:  # psycopg errors, OutOfOrder
        fail("backend", f"database error: {e}", code=Exit.BACKEND, output=output)
    finally:
        # Unlocking a dead connection must not mask the error that killed it.
        with contextlib.suppress(Exception):
            _db.unlock(conn)
        conn.close()
    hint = (
        "archive has manifests behind the watermark that are missing from the store; "
        "run `job-hunter rebuild` to repair"
    ) if s.gaps else None
    emit(
        {"ingested": s.ingested, "skipped": s.skipped, "last_attempt": s.last_attempt,
         "gaps": s.gaps, "hint": hint},
        human=f"ingested {s.ingested}, skipped {s.skipped}, last {s.last_attempt or '-'}"
        + (f"\nGAPS: {len(s.gaps)} manifest(s) missing from the store — {hint}" if s.gaps else ""),
        output=output,
        hint=hint,
    )
    if s.gaps:
        raise typer.Exit(int(Exit.SYSTEMIC))


@app.command()
def rebuild(
    yes: bool = typer.Option(False, "--yes", help="Confirm; required when stdin is not a TTY"),
    output: str | None = output_option(),
) -> None:
    """Rebuild the store from the whole archive into a fresh schema and swap it live."""
    from jobhunter.rebuild import LockHeld
    from jobhunter.rebuild import rebuild as _rebuild

    if not yes and not sys.stdin.isatty():
        fail("usage", "rebuild replaces the live schema",
             hint="re-run with --yes to confirm non-interactively",
             code=Exit.USAGE, output=output)
    settings = _settings(output)
    store = _store(settings, output)
    try:
        s = _rebuild(store, settings.require_database_url(), l2_globs=settings.l2_models,
                     drop_ratio=settings.drop_ratio,
                     schema=_schema)
    except ConfigError as e:
        fail("config", f"config error: {e}", code=Exit.CONFIG, output=output)
    except ArchiveError as e:
        fail("backend", f"archive error: {e}", code=Exit.BACKEND, output=output)
    except LockHeld as e:  # another writer holds the advisory lock; not an error
        emit({"lock_held": True, "ingested": 0, "skipped": 0, "swapped": False},
             human=f"{e}; nothing rebuilt", output=output)
        return
    except Exception as e:
        fail("backend", f"database error: {e}", code=Exit.BACKEND, output=output)
    emit({"ingested": s.ingested, "skipped": s.skipped, "work_schema": s.work_schema,
          "swapped": s.swapped},
         human=f"rebuilt {s.ingested} attempts into {s.work_schema}; swapped live",
         output=output)


@app.command()
def report(
    since: str = typer.Option("24h", "--since", help="Window: Nm, Nh or Nd"),
    output: str | None = output_option(),
) -> None:
    """Opened / changed / closed / reopened postings in the window."""
    from jobhunter.store.queries import events_since

    settings = _settings(output)
    window = _parse_since(since)
    conn = _conn(settings, schema=_schema, output=output)
    try:
        events = events_since(conn, _now() - window)
    except Exception as e:
        fail("backend", f"database error: {e}", code=Exit.BACKEND, output=output)
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
    emit({"since": since, "counts": counts, "events": rows},
         human="\n".join(human), output=output)


@app.command()
def status(output: str | None = output_option()) -> None:
    """Per-board fetch health from the archive, plus store health when a DB is configured."""
    settings = _settings(output)
    store = _store(settings, output)
    try:
        registry = load_registry(settings.registry_path)
        latest = latest_per_board(store)
    except RegistryError as e:
        fail("systemic", f"registry error: {e}", code=Exit.SYSTEMIC, output=output)
    except ArchiveError as e:
        fail("backend", f"archive error: {e}", code=Exit.BACKEND, output=output)
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
    if show_db and (xblock := _extraction_block(settings)) is not None:
        payload["extraction"] = xblock
        counts = ", ".join(f"{k} {v}" for k, v in sorted(xblock["by_status"].items())) or "none"
        human.append(
            f"extraction: queue {xblock['queue_depth']}; {counts}; "
            f"spend today ${xblock['spend_today_usd']:.2f}; "
            f"models 7d: {', '.join(xblock['observed_models_7d']) or '-'}"
        )
    if untracked:
        human.append(f"not in registry but present in archive: {', '.join(untracked)}")
    emit(payload, human="\n".join(human), output=output)


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
    output: str | None = output_option(),
) -> None:
    """List attempts (manifests) in the archive."""
    settings = _settings(output)
    store = _store(settings, output)
    src, brd = _split_board(board, output)
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
        fail("backend", f"archive error: {e}", code=Exit.BACKEND, output=output)
    human = [f"{i['started_at']}  {i['board']:28} {i['transport']:11} "
             f"{i['payload_bytes']:>9}B  "
             f"{i['record_count'] if i['record_count'] is not None else '-'}"
             for i in items]
    emit(items, human="\n".join(human) or "(no attempts)", output=output)


@registry_app.command("check")
def registry_check(output: str | None = output_option()) -> None:
    """Validate companies.toml and print its revision."""
    settings = _settings(output)
    try:
        reg = load_registry(settings.registry_path)
    except (RegistryError, OSError) as e:
        fail("systemic", f"registry error: {e}", code=Exit.SYSTEMIC, output=output)
    emit(
        {"boards": [b.key for b in reg.boards], "revision": reg.revision},
        human=f"ok: {len(reg.boards)} boards, revision {reg.revision[:12]}",
        output=output,
    )


@registry_app.command("list")
def registry_list(output: str | None = output_option()) -> None:
    """Board membership history (panel)."""
    from jobhunter.store.queries import panel_rows

    settings = _settings(output)
    conn = _conn(settings, schema=_schema, output=output)
    try:
        rows = panel_rows(conn)
    except Exception as e:
        fail("backend", f"database error: {e}", code=Exit.BACKEND, output=output)
    finally:
        conn.close()
    items = [{"board": f"{r['source']}:{r['board']}", "company": r["company"],
              "added_at": iso(r["added_at"]),
              "removed_at": iso(r["removed_at"]) if r["removed_at"] else None,
              "registry_revision": r["registry_revision"]} for r in rows]
    human = [f"{i['board']:32} {i['company']:20} {i['added_at']}  {i['removed_at'] or 'open'}"
             for i in items]
    emit(items, human="\n".join(human) or "(no panel rows)", output=output)


@db_app.command("init")
def db_init(output: str | None = output_option()) -> None:
    """Create the jobhunter schema and tables (idempotent)."""
    settings = _settings(output)
    conn = _conn(settings, schema=_schema, output=output)
    try:
        try:
            _db.init(conn, _schema)
        except _db.SchemaMismatch as e:
            fail("systemic", f"schema error: {e}", code=Exit.SYSTEMIC, output=output)
        conn.commit()
        payload = {"schema": _schema, "schema_version": _db.stored_schema_version(conn)}
    finally:
        conn.close()
    emit(
        payload,
        human=f"schema {payload['schema']} ready, version {payload['schema_version']}",
        output=output,
    )


@db_app.command("version")
def db_version(output: str | None = output_option()) -> None:
    """Print the code's schema version and the database's; exit 6 on mismatch."""
    settings = _settings(output)
    conn = _conn(settings, schema=_schema, output=output)
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
    emit(payload, human=f"code {payload['code']}  db {stored or 'absent'}", output=output,
         hint=None if payload["match"] else "run: job-hunter rebuild")
    if not payload["match"]:
        raise typer.Exit(int(Exit.SYSTEMIC))


if __name__ == "__main__":
    app()


# -- L2 extraction ---------------------------------------------------------

extract_app = typer.Typer(help="L2 demand-profile extraction")
review_app = typer.Typer(help="Human review verbs (archive event first, then the row)")
extract_app.add_typer(review_app, name="review")
app.add_typer(extract_app, name="extract")


def _extraction_block(settings: Settings) -> dict[str, Any] | None:
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.runner import SCHEMA_VERSION
    from jobhunter.l2.state import globs_to_regex
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.markdown import NORMALIZER_VERSION
    from jobhunter.store.queries import extraction_status

    try:
        conn = _db.connect(settings.require_database_url(), schema=_schema)
    except Exception:
        return None
    try:
        return extraction_status(
            conn, prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
            validator_version=VALIDATOR_VERSION,
            model_regex=globs_to_regex(settings.l2_models),
            normalizer_version=NORMALIZER_VERSION,
        )
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _make_engine(settings: Settings) -> Any:
    """Indirection so tests can substitute a scripted fake engine."""
    from jobhunter.l2.engines import ClaudeCli, CodexCli, OpenAICompat

    if settings.l2_engine == "claude-cli":
        return ClaudeCli()
    if settings.l2_engine == "codex-cli":
        return CodexCli(
            reasoning_effort=settings.l2_reasoning_effort,
            trust_requested_model=settings.l2_trust_requested_model,
        )
    assert settings.l2_base_url is not None  # require_l2 ran
    return OpenAICompat(settings.l2_base_url, settings.l2_api_key, prices=settings.l2_price)


@extract_app.command("run")
def extract_run(
    max_docs: int | None = typer.Option(None, "--max-docs"),
    max_usd: float | None = typer.Option(None, "--max-usd"),
    doc: str | None = typer.Option(None, "--doc", help="Extract exactly this document_hash"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the queue, write nothing"),
    output: str | None = output_option(),
) -> None:
    """Drain the extraction queue under the caps (harness spec §4.6)."""
    from jobhunter.l2 import runner as l2_runner
    from jobhunter.l2.engines import EngineFatalError

    settings = _settings(output)
    try:
        settings.require_l2()
    except ConfigError as e:
        fail("config", f"config error: {e}", code=Exit.CONFIG, output=output)
    store = _store(settings, output)
    conn = _conn(settings, schema=_schema, output=output)
    try:
        _db.init(conn, _schema)
        conn.commit()
        summary = l2_runner.run(
            settings, conn, store,
            engine=_make_engine(settings),
            max_docs=max_docs if max_docs is not None else settings.l2_max_docs,
            max_usd=max_usd if max_usd is not None else settings.l2_max_usd,
            only_doc=doc, dry_run=dry_run,
        )
        conn.commit()
    except ArchiveError as e:
        fail("backend", f"archive error: {e}", code=Exit.BACKEND, output=output)
    except (psycopg.Error, _db.SchemaMismatch, ValueError) as e:
        fail("backend", f"database error: {e}", code=Exit.BACKEND, output=output)
    except EngineFatalError as e:
        fail("systemic", f"engine error: {e}", code=Exit.SYSTEMIC, output=output)
    finally:
        conn.close()
    if summary.lock_held:
        emit(summary.to_dict(), human="already running (extract lock held); nothing done",
             output=output)
        return
    if dry_run:
        human = f"queue ({len(summary.queued)}):\n" + "\n".join(summary.queued)
    else:
        human = (
            f"run {summary.run_id}: {summary.validated} validated, "
            f"{summary.quarantined} quarantined, {summary.pending} pending, "
            f"{summary.replayed} replayed, ${summary.spend_usd:.2f}"
        )
        if summary.throttled:
            human += "  THROTTLED (batch stopped)"
        if summary.breaker_abort:
            human += "  BREAKER: 5 consecutive model rejections"
    emit(summary.to_dict(), human=human, output=output)
    if summary.breaker_abort or (summary.throttled and summary.validated == 0):
        # a scheduled run that did nothing must not report success
        raise typer.Exit(int(Exit.SYSTEMIC))


@extract_app.command("rebuild")
def extract_rebuild(output: str | None = output_option()) -> None:
    """Truncate the extraction surface and replay it from the archive. No LLM."""
    from jobhunter.l2.rebuild import rebuild_extractions

    settings = _settings(output)
    store = _store(settings, output)
    conn = _conn(settings, schema=_schema, output=output)
    try:
        if not _db.try_lock(conn, _db.EXTRACT_LOCK_KEY):
            emit({"lock_held": True}, human="extract lock held; try again later", output=output)
            return
        attempts, reviews = rebuild_extractions(conn, store, settings.l2_models)
        conn.commit()
    except Exception as e:  # archive errors, corrupt objects, psycopg failures
        with contextlib.suppress(Exception):
            conn.rollback()  # replay is all-or-nothing: discard TRUNCATE + partial rows
        fail("systemic", f"rebuild error: {e}", code=Exit.SYSTEMIC, output=output)
    finally:
        with contextlib.suppress(Exception):
            _db.unlock(conn, _db.EXTRACT_LOCK_KEY)
        conn.close()
    emit(
        {"attempts": attempts, "reviews": reviews},
        human=f"replayed {attempts} attempts, {reviews} review events",
        output=output,
    )


def _review_verb(verb: str, doc: str, note: str | None, output: str | None) -> None:
    from jobhunter.archive.keys import x_review_key
    from jobhunter.store import extraction as xstore
    from jobhunter.timeutil import utcnow_precise

    settings = _settings(output)
    store = _store(settings, output)
    conn = _conn(settings, schema=_schema, output=output)
    try:
        if not _db.try_lock(conn, _db.EXTRACT_LOCK_KEY):
            fail("systemic", "extract lock held (a run or another review is active)",
                 code=Exit.SYSTEMIC, output=output, hint="try again in a moment")
        doc = _resolve_doc(conn, doc, output)
        row = conn.execute(
            "SELECT * FROM extractions WHERE document_hash=%s ORDER BY updated_at DESC LIMIT 1",
            (doc,),
        ).fetchone()
        if row is None:
            fail("not_found", f"no extraction row for {doc}", code=Exit.NOT_FOUND, output=output,
                 hint=f"run: job-hunter extract run --doc {doc}")
        at = utcnow_precise()  # the fold orders review verbs against attempts
        n = conn.execute(
            "SELECT count(*) AS n FROM extraction_reviews WHERE document_hash=%s", (doc,)
        ).fetchone()
        seq = int(n["n"] if n else 0) + 1
        event: dict[str, Any] = {
            "review_key": x_review_key(at, doc, verb, seq),
            "document_hash": doc,
            "model": row["model"],
            "prompt_version": row["prompt_version"],
            "schema_version": row["schema_version"],
            "validator_version": row["validator_version"],
            "verb": verb,
            "payload": {"note": note} if note else None,
            "actor": "human",
            "at": at.isoformat(),
        }
        store.put(event["review_key"], json.dumps(event, ensure_ascii=False).encode("utf-8"))
        xstore.record_review(conn, **event)
        from jobhunter.l2.runner import settle

        state = settle(
            conn, store, doc, settings.l2_models, iso(_now()),
            prompt_version=row["prompt_version"], schema_version=row["schema_version"],
            validator_version=row["validator_version"],
        )
        conn.commit()
    finally:
        with contextlib.suppress(Exception):
            _db.unlock(conn, _db.EXTRACT_LOCK_KEY)
        conn.close()
    emit(
        {"document_hash": doc, "verb": verb, "status": state.status},
        human=f"{doc[:12]} {verb} -> {state.status or 'pending'}",
        output=output,
    )


@review_app.command("list")
def review_list(output: str | None = output_option()) -> None:
    """The inbox: needs_review and quarantined, oldest first."""
    from jobhunter.l2.prompt import PROMPT_VERSION

    settings = _settings(output)
    conn = _conn(settings, schema=_schema, output=output)
    try:
        # a row under an OLD config that has since been validated under the
        # current one is history, not work: show it as superseded so a stale
        # quarantine cannot nag forever after a prompt bump fixed it
        rows = conn.execute(
            """
            SELECT e.document_hash, e.status, e.model, e.prompt_version, e.updated_at,
                   EXISTS (
                     SELECT 1 FROM extractions cur
                     WHERE cur.document_hash = e.document_hash
                       AND cur.prompt_version = %s AND cur.status = 'validated'
                   ) AS superseded
            FROM extractions e
            WHERE e.status IN ('needs_review', 'quarantined')
            ORDER BY superseded, e.updated_at
            """,
            (PROMPT_VERSION,),
        ).fetchall()
    finally:
        conn.close()
    payload = [
        {"document_hash": r["document_hash"], "status": r["status"], "model": r["model"],
         "prompt_version": r["prompt_version"], "superseded": r["superseded"],
         "updated_at": iso(r["updated_at"])}
        for r in rows
    ]
    open_items = [r for r in payload if not r["superseded"]]
    lines = [
        f"{r['status']:13} {r['document_hash'][:12]}  {r['prompt_version']:20} {r['model']}"
        + ("   (superseded: validated under " + PROMPT_VERSION + ")" if r["superseded"] else "")
        for r in payload
    ]
    human = "\n".join(lines) or "inbox empty"
    if payload:
        n_super = len(payload) - len(open_items)
        human += f"\n\n{len(open_items)} needing attention, {n_super} superseded"
    emit({"inbox": payload}, human=human, output=output)


@review_app.command("show")
def review_show(
    doc: str = typer.Argument(..., help="document_hash"),
    output: str | None = output_option(),
) -> None:
    """The dossier: state row + attempt history with per-attempt errors."""
    settings = _settings(output)
    conn = _conn(settings, schema=_schema, output=output)
    try:
        doc = _resolve_doc(conn, doc, output)
        row = conn.execute(
            "SELECT * FROM extractions WHERE document_hash=%s ORDER BY updated_at DESC LIMIT 1",
            (doc,),
        ).fetchone()
        attempts = conn.execute(
            "SELECT attempt_no, requested_model, observed_model, outcome, error_detail,"
            " started_at FROM extraction_attempts WHERE document_hash=%s"
            " ORDER BY started_at, attempt_no",
            (doc,),
        ).fetchall()
    finally:
        conn.close()
    payload = {
        "state": {k: (iso(v) if k == "updated_at" else v) for k, v in row.items()}
        if row else None,
        "attempts": [
            {**a, "started_at": iso(a["started_at"])} for a in attempts
        ],
    }
    lines = [f"status: {row['status'] if row else 'pending'}"]
    for a in attempts:
        detail = a["error_detail"] or {}
        errs = "; ".join(detail.get("errors", [])[:2]) if isinstance(detail, dict) else ""
        lines.append(
            f"  a{a['attempt_no']} {a['outcome']:19} {a['requested_model']}"
            f" -> {a['observed_model'] or '-'}  {errs}"
        )
    emit(payload, human="\n".join(lines), output=output)


@review_app.command("accept")
def review_accept(doc: str, output: str | None = output_option()) -> None:
    """Promote needs_review -> validated. Human-only by design."""
    _review_verb("accept", doc, None, output)


@review_app.command("reject")
def review_reject(
    doc: str,
    note: str = typer.Option(..., "--note", help="Why (required; rejection reasons are eval data)"),
    output: str | None = output_option(),
) -> None:
    _review_verb("reject", doc, note, output)


@review_app.command("retry")
def review_retry(doc: str, output: str | None = output_option()) -> None:
    """Clear the row back to pending; the next run grants fresh attempts."""
    _review_verb("retry", doc, None, output)


@review_app.command("flag")
def review_flag(doc: str, output: str | None = output_option()) -> None:
    _review_verb("flag", doc, None, output)


@extract_app.command("show")
def extract_show(
    doc: str = typer.Argument(..., help="document_hash or unambiguous hex prefix"),
    output: str | None = output_option(),
) -> None:
    """Read the extracted demand profile: facts, areas, claims, evidence."""
    from jobhunter.l2.quotes import line_col
    from jobhunter.markdown import NORMALIZER_VERSION
    from jobhunter.store import extraction as xstore

    settings = _settings(output)
    conn = _conn(settings, schema=_schema, output=output)
    try:
        doc = _resolve_doc(conn, doc, output)
        row = conn.execute(
            "SELECT e.*, v.title, v.company, v.url FROM extractions e"
            " LEFT JOIN documents d ON d.document_hash = e.document_hash"
            " LEFT JOIN posting_versions v ON v.version_hash = d.version_hash"
            " WHERE e.document_hash = %s ORDER BY e.updated_at DESC LIMIT 1",
            (doc,),
        ).fetchone()
        markdown = xstore.markdown_for(conn, doc, NORMALIZER_VERSION) or ""
    finally:
        conn.close()
    if row is None:
        fail("not_found", f"no extraction for {doc[:12]}", code=Exit.NOT_FOUND, output=output,
             hint=f"run: job-hunter extract run --doc {doc}")
    profile = row["profile"]
    payload = {"document_hash": doc, "status": row["status"], "model": row["model"],
               "prompt_version": row["prompt_version"], "profile": profile}
    if use_json(output):
        emit(payload, human="", output=output)
        return
    if profile is None:
        fail("not_found", f"{row['status']}: no profile stored", code=Exit.NOT_FOUND,
             output=output, hint=f"job-hunter extract review show {doc[:12]}")

    def at(quote: dict[str, Any]) -> str:
        line, col = line_col(markdown, int(quote["span"][0]))
        return f"line {line}:{col}"

    out: list[str] = [
        f"{row['title'] or '?'} — {row['company'] or '?'}",
        f"{doc[:12]}  {row['status']}  {row['model']}  {row['prompt_version']}",
        "",
        "facts",
    ]
    facts = profile.get("facts") or {}
    exp = facts.get("experience_months")
    if exp:
        hi = exp["max"] if exp["max"] is not None else "+"
        out.append(f"  experience    {exp['min']}–{hi} months ({exp.get('scope') or 'unscoped'})")
        out.append(f"                {exp['anchor']['text'][:70]!r}  {at(exp['anchor'])}")
    for comp in facts.get("compensation") or []:
        span = f"{comp['min']:,}–{comp['max']:,}" if comp.get("min") else "?"
        # currency/period are null when the posting never states them; say so
        # rather than printing a bare "/?" that reads like a parse failure
        unit = " ".join(
            x for x in (comp.get("currency"), f"per {comp['period']}" if comp.get("period") else "")
            if x
        )
        unstated = [k for k in ("currency", "period") if not comp.get(k)]
        note = f"  ({', '.join(unstated)} not stated)" if unstated else ""
        out.append(f"  compensation  {span} {unit}{note}  {at(comp['anchor'])}")
    dl = facts.get("deadline")
    out.append(f"  deadline      {dl['date'] if dl else '— (none stated)'}")
    out.append(f"  boilerplate   {len(facts.get('boilerplate_spans') or [])} spans excluded")

    areas = (profile.get("demand_profile") or {}).get("areas") or []
    n_claims = sum(len(a["claims"]) for a in areas)
    out += ["", f"areas ({len(areas)})  claims ({n_claims})"]
    for area in areas:
        level = f"/{area['level']}" if area.get("level") else ""
        out.append(f"\n  [{area['kind']}] {area['name']}  — {area['importance']}{level}")
        for claim in area["claims"]:
            lvl = f"/{claim['level']}" if claim.get("level") else ""
            out.append(f"    · {claim['importance']}{lvl}  {at(claim['quote'])}")
            out.append(f"      {claim['quote']['text'][:96]!r}")
            if claim.get("level_evidence"):
                out.append(f"      evidence: {claim['level_evidence']!r}")
        if area.get("structure"):
            out.append(f"    structure: {json.dumps(area['structure'])}")
        if area.get("mentions"):
            out.append(f"    mentions: {', '.join(area['mentions'][:8])}")
    emit(payload, human="\n".join(out), output=output)
