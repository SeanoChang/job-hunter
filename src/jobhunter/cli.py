"""job-hunter command line.

Every command speaks the contract in `cli_output`: one JSON envelope when
stdout is piped, human text on a TTY, `-o/--output` to force either. Exit codes
are the typed table in `cli_output.Exit` (0 ok, 1 verify findings, 2 usage,
3 config, 4 not found, 5 backend, 6 systemic). `skill` is the sole exception:
its payload is a file, so piping it writes markdown unless `-o json` is given.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import typer
from typer.core import TyperGroup
from typer.main import get_command

from jobhunter import __version__
from jobhunter.archive import ArchiveError, ArchiveStore, open_store
from jobhunter.archive.manifests import iter_manifests, latest_per_board
from jobhunter.cli_output import Exit, emit, fail, output_option, use_json
from jobhunter.cli_q import MAX_LIMIT, _clamp, q_app
from jobhunter.config import ConfigError, Settings, env_snapshot
from jobhunter.cursors import Watermark, read_cursor, write_cursor
from jobhunter.fetch import RunSummary, UnknownBoardError, is_healthy
from jobhunter.fetch import run as fetch_run
from jobhunter.http import Fetcher
from jobhunter.pulse import build_pulse
from jobhunter.registry import RegistryError
from jobhunter.registry import load as load_registry
from jobhunter.store import db as _db
from jobhunter.timeutil import iso, parse_iso, utcnow

_SINCE = re.compile(r"^(\d+)([mhd])$")

app = typer.Typer(no_args_is_help=True, add_completion=False, help="job-hunter ingestion")
archive_app = typer.Typer(help="Inspect the raw archive")
registry_app = typer.Typer(help="Inspect companies.toml")
db_app = typer.Typer(help="Postgres store")
app.add_typer(archive_app, name="archive")
app.add_typer(registry_app, name="registry")
app.add_typer(db_app, name="db")
app.add_typer(q_app, name="q")


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
             hint="run: job-hunter doctor — it checks every variable and names the fix")


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


def _parse_since(value: str, output: str | None = None) -> timedelta:
    m = _SINCE.match(value.strip())
    if not m:
        # fail, not typer.BadParameter: click renders BadParameter as a usage
        # box on stderr with an empty stdout, breaking the envelope contract
        fail("usage", f"--since must be a window like Nm, Nh or Nd: {value!r}",
             code=Exit.USAGE, output=output, hint="e.g. 30m, 24h or 7d")
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


def _fetch_once(
    settings: Settings,
    store: ArchiveStore,
    output: str | None,
    *,
    only: str | None = None,
    dry_run: bool = False,
) -> RunSummary:
    """One fetch run with the contract's error mapping (`fetch`, `sync`)."""
    fetcher = _make_fetcher()
    try:
        return fetch_run(settings, store=store, fetcher=fetcher, only=only,
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


def _fetch_failed(summary: RunSummary) -> bool:
    """A run that archived nothing usable, or could not reach the store."""
    counts = summary.counts()
    return bool(summary.db_error) or bool(counts["boards"] and counts["ok"] == 0)


def _fetch_human(summary: RunSummary, dry_run: bool) -> str:
    if summary.lock_held:
        return "already running (advisory lock held); nothing fetched"
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
    return "\n".join(lines)


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
    summary = _fetch_once(settings, store, output, only=board, dry_run=dry_run)
    emit(summary.to_dict(), human=_fetch_human(summary, dry_run), output=output)
    if _fetch_failed(summary):
        raise typer.Exit(int(Exit.SYSTEMIC))


_GAP_HINT = (
    "archive has manifests behind the watermark that are missing from the store; "
    "run `job-hunter rebuild` to repair"
)


def _ingest_once(
    settings: Settings, store: ArchiveStore, output: str | None
) -> dict[str, Any]:
    """One drain of the pending manifests under the ingest lock (`ingest`, `sync`)."""
    from jobhunter.ingest import replay_pending

    conn = _conn(settings, schema=_schema, output=output)
    try:
        if not _db.try_lock(conn):
            return {"lock_held": True, "ingested": 0, "skipped": 0, "last_attempt": None}
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
    return {"ingested": s.ingested, "skipped": s.skipped, "last_attempt": s.last_attempt,
            "gaps": s.gaps, "hint": _GAP_HINT if s.gaps else None}


def _ingest_human(data: dict[str, Any]) -> str:
    if data.get("lock_held"):
        return "already running (advisory lock held); nothing ingested"
    gaps = data["gaps"]
    return (
        f"ingested {data['ingested']}, skipped {data['skipped']}, "
        f"last {data['last_attempt'] or '-'}"
        + (f"\nGAPS: {len(gaps)} manifest(s) missing from the store — {_GAP_HINT}" if gaps else "")
    )


@app.command()
def ingest(output: str | None = output_option()) -> None:
    """Replay archive manifests newer than the last ingested one into the store."""
    settings = _settings(output)
    store = _store(settings, output)
    data = _ingest_once(settings, store, output)
    emit(data, human=_ingest_human(data), output=output, hint=data.get("hint"))
    if data.get("gaps"):
        raise typer.Exit(int(Exit.SYSTEMIC))


def _sync_human(data: dict[str, Any], summary: RunSummary | None) -> str:
    # `text` is a thunk: a skipped or failed phase has no summary to render.
    def phase(name: str, block: dict[str, Any], text: Callable[[], str]) -> str:
        if block.get("skipped_reason"):
            return f"{name}: skipped ({block['skipped_reason']})"
        if block.get("error"):
            return f"{name}: {block['error']}"
        return f"{name}: {text()}"

    return "\n".join([
        phase("ingest", data["ingest"], lambda: _ingest_human(data["ingest"])),
        phase("fetch", data["fetch"], lambda: _fetch_human(summary, False) if summary else ""),
        phase("extract", data["extract"], lambda: _extract_human(data["extract"], False)),
    ])


@app.command()
def sync(
    no_extract: bool = typer.Option(False, "--no-extract", help="Skip the extraction pass"),
    extract_max_docs: int | None = typer.Option(
        None, "--extract-max-docs", help="Documents to extract this run (default: config)"
    ),
    output: str | None = output_option(),
) -> None:
    """Drain pending manifests, fetch every board, extract within budget: one operator run."""
    settings = _settings(output)
    store = _store(settings, output)
    summary: RunSummary | None = None
    code = Exit.OK
    ingest_data = _ingest_once(settings, store, output)
    if ingest_data.get("gaps"):
        # The store is behind the archive; fetching would advance the watermark
        # past the missing manifests. The CI step order stops here for this reason.
        code = Exit.SYSTEMIC
        fetch_data: dict[str, Any] = {"skipped_reason": "ingest gaps"}
        extract_data: dict[str, Any] = {"skipped_reason": "ingest gaps"}
    else:
        summary = _fetch_once(settings, store, output)
        fetch_data = summary.to_dict()
        if _fetch_failed(summary):
            code = Exit.SYSTEMIC
            extract_data = {"skipped_reason": "collection failed"}
        elif no_extract:
            extract_data = {"skipped_reason": "--no-extract"}
        elif not settings.l2_model_candidates:
            extract_data = {"skipped_reason": "no JOB_HUNTER_L2_MODEL_CANDIDATES"}
        else:
            try:
                extract_data = _extract_once(settings, store, extract_max_docs, None)
            except _ExtractFailure as e:
                # Collection is irreplaceable and extraction recomputable, so a bad
                # engine day is reported, never fatal (the reasoning fetch.yml's
                # continue-on-error carried).
                extract_data = {"error": e.message}
            else:
                if _extract_stalled(extract_data):
                    code = Exit.SYSTEMIC
    data = {"ingest": ingest_data, "fetch": fetch_data, "extract": extract_data}
    emit(data, human=_sync_human(data, summary), output=output,
         hint=ingest_data.get("hint") or "job-hunter pulse to read what changed")
    if code is not Exit.OK:
        raise typer.Exit(int(code))


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


def _pulse_since(value: str, output: str | None) -> str:
    """`--since` accepts what the rest of the CLI accepts: a relative window, or
    an absolute timestamp. Returns it as the ISO instant a watermark stores."""
    if _SINCE.match(value.strip()):
        return (_now() - _parse_since(value)).isoformat()
    try:
        return parse_iso(value).isoformat()
    except ValueError:
        fail("usage", f"--since is not a timestamp or a window: {value!r}", code=Exit.USAGE,
             output=output, hint="e.g. 2026-09-01T00:00:00Z, or 24h")


def _pulse_human(payload: dict[str, Any], truncated: bool) -> str:
    events = payload["events"]
    kinds = ("opened", "changed", "closed", "reopened")
    counts = {k: sum(e["kind"] == k for e in events) for k in kinds}
    window = payload["window"]
    lines = [
        f"{window['from']} .. {window['to']}"
        + ("  (first run: last 24h)" if payload["first_run"] else ""),
        ", ".join(f"{v} {k}" for k, v in counts.items() if v) or "nothing new",
    ]
    for e in events:
        line = f"  {e['kind']:8} {(e['company'] or '-'):18} {e['title'] or '-'}"
        if e["closed_between"]:
            line += f"  (closed between {e['closed_between'][0]} and {e['closed_between'][1]})"
        lines.append(line)
        summary = e.get("profile")
        if summary:
            areas = ", ".join(f"{a['name']} [{a['importance']}]" for a in summary["areas"][:3])
            lines.append(f"      {areas}" if areas else "      (no areas)")
            if summary["mentions"]:
                lines.append(f"      mentions: {', '.join(summary['mentions'])}")
    if truncated:
        lines.append("  ... truncated: call again to continue")
    attention = payload["attention"]
    for b in attention["unhealthy_boards"]:
        lines.append(f"attention  {b['board']:32} {b['health']:12} {b['error'] or ''}".rstrip())
    x = attention["extraction"]
    if x:
        lines.append(
            f"attention  extraction queue {x['queue_depth']}, "
            f"spend today ${x['spend_today_usd']:.2f}"
        )
    return "\n".join(lines)


@app.command()
def pulse(
    cursor: str = typer.Option("default", "--cursor", help="Named watermark in the state dir"),
    since: str | None = typer.Option(
        None, "--since", help="ISO timestamp or Nm/Nh/Nd; reports without touching the cursor"
    ),
    boards: str | None = typer.Option(None, "--boards", help="Comma list of source:board"),
    peek: bool = typer.Option(False, "--peek", help="Report without advancing the cursor"),
    limit: int = typer.Option(200, "--limit", help=f"1-{MAX_LIMIT}"),
    output: str | None = output_option(),
) -> None:
    """Everything new since the last pulse: events, profiles, attention. One call."""
    settings = _settings(output)
    only = tuple(b.strip() for b in boards.split(",") if b.strip()) if boards else None
    for b in only or ():
        _split_board(b, output)  # a typo in one entry must not silently match nothing
    wm = (
        Watermark(_pulse_since(since, output), ())
        if since is not None
        else read_cursor(settings.state_dir, cursor)
    )
    conn = _conn(settings, schema=_schema, output=output)
    try:
        payload, new_wm = build_pulse(
            conn, settings, wm=wm, limit=_clamp(limit), boards=only, now=_now()
        )
    except typer.Exit:
        raise
    except Exception as e:
        fail("backend", f"database error: {e}", code=Exit.BACKEND, output=output)
    finally:
        conn.close()
    truncated = bool(payload.pop("_truncated"))
    events = payload["events"]
    hint = None
    if events:
        hint = f"q posting {events[0]['uid']} for lifecycle detail"
        docs = [e["document_hash"] for e in events if e.get("document_hash")]
        if docs:
            hint += f"; q profile --doc {docs[0][:12]} for what it demands"
    emit(payload, human=_pulse_human(payload, truncated), output=output, count=len(events),
         truncated=truncated, hint=hint,
         extra_meta={"cursor": None if since is not None else cursor,
                     "first_run": payload["first_run"]})
    if not peek and since is None and new_wm is not None:
        try:
            # After the envelope is flushed, never before: a crash between the
            # two re-reports one window, which is the harmless direction.
            write_cursor(settings.state_dir, cursor, new_wm)
        except OSError as e:
            typer.echo(f"error: cursor {cursor!r} not advanced: {e}", err=True)
            raise typer.Exit(int(Exit.BACKEND)) from e


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


# -- doctor ----------------------------------------------------------------

_AWS_VARS = ("AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
             "AWS_DEFAULT_REGION")
# A failing check named here means the environment is wrong (exit 3); every other
# check is a live probe, which fails with exit 5 once the configuration is sound.
_CONFIG_CHECKS = frozenset({"archive_url", "aws_credentials", "database_url", "l2"})
_ARCHIVE_HINT = "export JOB_HUNTER_ARCHIVE_URL=s3://bucket/prefix (or file:///path)"
_DSN_HINT = "export JOB_HUNTER_DATABASE_URL=postgresql://user:pass@host:5432/db"
_PROBE_KEY = "attempts/.doctor-probe"  # never written; the read is the point


def _check(name: str, ok: bool, detail: str, hint: str | None = None) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "hint": hint}


def _not_run(names: tuple[str, ...], reason: str, hint: str) -> list[dict[str, Any]]:
    """Probes whose prerequisite failed still report themselves: the check list an
    agent parses has the same names every run."""
    return [_check(n, False, f"not run: {reason}", hint) for n in names]


def _doctor_archive(env: Mapping[str, str]) -> list[dict[str, Any]]:
    """The archive URL, the R2 variables it implies, and one live read."""
    url = env.get("JOB_HUNTER_ARCHIVE_URL")
    store: ArchiveStore | None = None
    if not url:
        checks = [_check("archive_url", False, "JOB_HUNTER_ARCHIVE_URL is not set", _ARCHIVE_HINT)]
    else:
        try:
            store = open_store(url)
            checks = [_check("archive_url", True, url)]
        except Exception as e:  # unsupported scheme, unusable root, boto3 client setup
            checks = [_check("archive_url", False, f"{type(e).__name__}: {e}", _ARCHIVE_HINT)]
    if url and url.startswith("s3://"):
        missing = [v for v in _AWS_VARS if not env.get(v)]  # presence only; never the values
        checks.append(_check(
            "aws_credentials", not missing,
            "set" if not missing else f"missing: {', '.join(missing)}",
            None if not missing else "s3/R2 needs endpoint, key id, secret and region in the env",
        ))
    if store is None:
        checks.append(_check("archive_probe", False, "not run: no usable archive URL",
                             _ARCHIVE_HINT))
        return checks
    try:
        store.exists(_PROBE_KEY)  # the cheapest call that still reaches the backend
    except Exception as e:
        checks.append(_check(
            "archive_probe", False, f"{type(e).__name__}: {e}",
            "check the archive credentials and endpoint; `archive ls` uses the same path",
        ))
    else:
        checks.append(_check("archive_probe", True, "reachable"))
    return checks


def _doctor_schema(conn: _db.Conn) -> dict[str, Any]:
    try:
        if not _db.schema_exists(conn, _schema):
            return _check("schema_version", False, f"schema {_schema} does not exist",
                          "run: job-hunter db init")
        stored = _db.stored_schema_version(conn)
    except psycopg.Error as e:  # half-created schema: no schema_meta yet
        conn.rollback()
        return _check("schema_version", False, f"{type(e).__name__}: {e}",
                      "run: job-hunter rebuild")
    if stored == _db.SCHEMA_VERSION:
        return _check("schema_version", True, f"{stored} (code {_db.SCHEMA_VERSION})")
    return _check("schema_version", False,
                  f"database {stored or 'absent'} != code {_db.SCHEMA_VERSION}",
                  "run: job-hunter rebuild")


def _doctor_role(conn: _db.Conn) -> dict[str, Any]:
    """Write authority is not an error — it is more than `q`/`pulse` need."""
    try:
        row = conn.execute(
            "SELECT current_user AS who,"
            " has_table_privilege(current_user, 'postings', 'INSERT') AS writer"
        ).fetchone() or {}
    except psycopg.Error as e:  # no postings table under this search_path
        conn.rollback()
        return _check("role", False, f"{type(e).__name__}: {e}", "run: job-hunter db init")
    who = row.get("who", "?")
    if row.get("writer"):
        return _check("role", True, f"{who}: writer DSN — fine for operators;"
                                    " use a read-only role on agent machines")
    return _check("role", True, f"{who}: read-only")


def _doctor_database(env: Mapping[str, str]) -> list[dict[str, Any]]:
    dsn = env.get("JOB_HUNTER_DATABASE_URL")
    if not dsn:
        return [_check("database_url", False, "JOB_HUNTER_DATABASE_URL is not set", _DSN_HINT),
                *_not_run(("database_probe", "schema_version", "role"), "no DSN", _DSN_HINT)]
    checks = [_check("database_url", True, "set")]  # a DSN carries a password; never echo it
    try:
        # not `_conn`: that one reports through the envelope and exits, and doctor
        # owes the caller the remaining checks
        conn = _db.connect(dsn, schema=_schema)
    except Exception as e:
        checks.append(_check(
            "database_probe", False, f"{type(e).__name__}: {e}",
            "is Postgres reachable? `docker compose up -d postgres` runs one locally"))
        return checks + _not_run(("schema_version", "role"), "no connection",
                                 "fix database_probe first")
    try:
        conn.execute("SELECT 1")
        checks.append(_check("database_probe", True, f"connected, search_path {_schema}"))
        checks.append(_doctor_schema(conn))
        checks.append(_doctor_role(conn))
    finally:
        conn.close()
    return checks


def _doctor_l2(env: Mapping[str, str]) -> dict[str, Any]:
    try:
        settings = Settings.load(env)
    except ConfigError as e:
        # the archive/database checks above already name what is broken
        return _check("l2", False, f"not run: {e}", "fix the configuration failures above")
    if not settings.l2_model_candidates:
        return _check("l2", True, "extraction not configured (optional)")
    try:
        settings.require_l2()
    except ConfigError as e:
        return _check("l2", False, str(e),
                      "set JOB_HUNTER_L2_BASE_URL, or JOB_HUNTER_L2_ENGINE=claude-cli|codex-cli")
    return _check("l2", True,
                  f"{settings.l2_engine}: {', '.join(settings.l2_model_candidates)}")


@app.command()
def doctor(output: str | None = output_option()) -> None:
    """Check config, connectivity, schema and role. Every check runs; each failure names its fix."""
    env = env_snapshot()
    checks = [*_doctor_archive(env), *_doctor_database(env), _doctor_l2(env)]
    failed = [c for c in checks if not c["ok"]]
    human: list[str] = []
    for c in checks:
        human.append(f"{'ok  ' if c['ok'] else 'FAIL'}  {c['name']:16} {c['detail']}")
        if not c["ok"] and c["hint"]:
            human.append(f"      hint: {c['hint']}")
    emit({"checks": checks}, human="\n".join(human), output=output, count=len(checks),
         hint=f"{len(failed)} check(s) failed; each carries its fix" if failed else None)
    if failed:
        # configuration first: a probe cannot succeed while the variables it reads are wrong
        raise typer.Exit(int(
            Exit.CONFIG if any(c["name"] in _CONFIG_CHECKS for c in failed) else Exit.BACKEND
        ))


# -- introspection ---------------------------------------------------------

_EXIT_HELP = {
    "OK": "success",
    "FINDINGS": "verify ran and its findings failed",
    "USAGE": "usage or validation error: read error.valid and fix the flag",
    "CONFIG": "configuration missing or invalid: run job-hunter doctor",
    "NOT_FOUND": "unknown or ambiguous identifier: lengthen the prefix, or re-list",
    "BACKEND": "backend unavailable: database, archive or network",
    "SYSTEMIC": "systemic failure an operator must act on",
}

# The success and error shapes `cli_output` emits, as a schema a reader can
# validate against instead of inferring from examples.
_ENVELOPE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "job-hunter CLI envelope",
    "description": "Exactly one of these on stdout per run; diagnostics go to stderr.",
    "oneOf": [
        {
            "type": "object",
            "required": ["ok", "data", "meta"],
            "additionalProperties": False,
            "properties": {
                "ok": {"const": True},
                "data": {"description": "verb-specific payload"},
                "meta": {
                    "type": "object",
                    "required": ["truncated"],
                    "properties": {
                        "count": {"type": "integer"},
                        "truncated": {"type": "boolean"},
                        "next_cursor": {"type": ["string", "null"]},
                        "hint": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "object",
            "required": ["ok", "error"],
            "additionalProperties": False,
            "properties": {
                "ok": {"const": False},
                "error": {
                    "type": "object",
                    "required": ["kind", "message", "hint", "valid"],
                    "properties": {
                        "kind": {"enum": ["usage", "config", "not_found", "backend", "systemic"]},
                        "message": {"type": "string"},
                        "hint": {"type": ["string", "null"]},
                        "valid": {"type": ["array", "null"]},
                    },
                },
            },
        },
    ],
}


def _param_spec(param: Any) -> dict[str, Any]:
    """One flag as a caller needs it: what to type, what it takes, what it defaults to."""
    choices = getattr(param.type, "choices", None)
    return {
        "name": param.name,
        "opts": list(param.opts),
        "type": param.type.name,
        "default": param.default,
        "choices": [str(c) for c in choices] if choices else None,
    }


def _walk_commands(cmd: Any, path: str) -> list[dict[str, Any]]:
    """The leaf verbs of the live click tree.

    Generated, never hand-written: a command or flag added anywhere shows up
    here without anyone remembering to write it down. Groups are skipped —
    they take no arguments and cannot be invoked. (`click` lives inside typer
    as a vendored package, so the tree is walked structurally.)
    """
    if isinstance(cmd, TyperGroup):
        return [
            row
            for name, sub in sorted(cmd.commands.items())
            for row in _walk_commands(sub, f"{path} {name}".strip())
        ]
    return [{
        "path": path,
        "help": (cmd.help or "").strip().splitlines()[0] if cmd.help else "",
        "params": [_param_spec(p) for p in cmd.params],
    }]


@app.command()
def schema(output: str | None = output_option()) -> None:
    """The machine catalog: envelope schema, exit codes, versions, every command and flag."""
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.markdown import NORMALIZER_VERSION

    exit_codes = {str(int(e)): _EXIT_HELP[e.name] for e in Exit}
    versions = {"cli": __version__, "schema_version": _db.SCHEMA_VERSION,
                "normalizer": NORMALIZER_VERSION, "prompt": PROMPT_VERSION,
                "validator": VALIDATOR_VERSION}
    commands = _walk_commands(get_command(app), "")
    human = [
        "  ".join(f"{k} {v}" for k, v in versions.items()),
        "exit codes: " + ", ".join(f"{k} {v.split(':')[0]}" for k, v in exit_codes.items()),
        "",
    ]
    for c in commands:
        human.append(f"  {c['path']:26} {c['help']}")
        human.append(f"      {' '.join(p['opts'][0] for p in c['params'])}")
    emit({"contract": {"envelope": _ENVELOPE_SCHEMA, "exit_codes": exit_codes},
          "versions": versions, "commands": commands},
         human="\n".join(human), output=output, count=len(commands))


@app.command()
def skill(output: str | None = output_option()) -> None:
    """Print the shipped agent guide: the pulse loop, error recovery, token economy."""
    from importlib import resources

    text = resources.files("jobhunter.skill_data").joinpath("SKILL.md").read_text(encoding="utf-8")
    # The one verb whose payload IS a file: `skill > SKILL.md` is the documented
    # install, so piping must write markdown, not an envelope. Only -o json wraps it.
    as_json = output is not None and use_json(output)  # use_json still validates the value
    emit({"markdown": text}, human=text.rstrip("\n"), output="json" if as_json else "table",
         hint="install it: job-hunter skill > ~/.claude/skills/job-hunter-cli/SKILL.md")


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


class _ExtractFailure(Exception):
    """An extraction pass that could not run, carrying its contract error fields.

    `extract run` reports it through `fail`; `sync` records it in the envelope and
    keeps going — collection is irreplaceable, extraction is recomputable.
    """

    def __init__(self, kind: str, message: str, code: Exit, hint: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.code = code
        self.hint = hint


def _extract_once(
    settings: Settings,
    store: ArchiveStore,
    max_docs: int | None,
    max_usd: float | None,
    *,
    doc: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One extraction batch under the extract lock (`extract run`, `sync`).

    Failures raise `_ExtractFailure` rather than printing: the two callers report
    them differently, and only one of them owns the envelope.
    """
    from jobhunter.l2 import runner as l2_runner
    from jobhunter.l2.engines import EngineFatalError

    try:
        settings.require_l2()
        conn = _db.connect(settings.require_database_url(), schema=_schema)
    except ConfigError as e:
        raise _ExtractFailure("config", f"config error: {e}", Exit.CONFIG) from e
    except Exception as e:  # psycopg.OperationalError and friends
        raise _ExtractFailure("backend", f"database error: {e}", Exit.BACKEND,
                              "is Postgres reachable? check JOB_HUNTER_DATABASE_URL") from e
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
        raise _ExtractFailure("backend", f"archive error: {e}", Exit.BACKEND) from e
    except (psycopg.Error, _db.SchemaMismatch, ValueError) as e:
        raise _ExtractFailure("backend", f"database error: {e}", Exit.BACKEND) from e
    except EngineFatalError as e:
        raise _ExtractFailure("systemic", f"engine error: {e}", Exit.SYSTEMIC) from e
    finally:
        conn.close()
    return summary.to_dict()


def _extract_stalled(data: dict[str, Any]) -> bool:
    """A run that did nothing and cannot: the breaker tripped, or every call was throttled."""
    return bool(data["breaker_abort"] or (data["throttled"] and data["validated"] == 0))


def _extract_human(data: dict[str, Any], dry_run: bool) -> str:
    if data.get("lock_held"):
        return "already running (extract lock held); nothing done"
    if dry_run:
        return f"queue ({len(data['queued'])}):\n" + "\n".join(data["queued"])
    human = (
        f"run {data['run_id']}: {data['validated']} validated, "
        f"{data['quarantined']} quarantined, {data['pending']} pending, "
        f"{data['replayed']} replayed, ${data['spend_usd']:.2f}"
    )
    if data["throttled"]:
        human += "  THROTTLED (batch stopped)"
    if data["breaker_abort"]:
        human += "  BREAKER: 5 consecutive model rejections"
    return human


@extract_app.command("run")
def extract_run(
    max_docs: int | None = typer.Option(None, "--max-docs"),
    max_usd: float | None = typer.Option(None, "--max-usd"),
    doc: str | None = typer.Option(None, "--doc", help="Extract exactly this document_hash"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the queue, write nothing"),
    output: str | None = output_option(),
) -> None:
    """Drain the extraction queue under the caps (harness spec §4.6)."""
    settings = _settings(output)
    store = _store(settings, output)
    try:
        data = _extract_once(settings, store, max_docs, max_usd, doc=doc, dry_run=dry_run)
    except _ExtractFailure as e:
        fail(e.kind, e.message, code=e.code, output=output, hint=e.hint)
    emit(data, human=_extract_human(data, dry_run), output=output)
    if _extract_stalled(data):
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
