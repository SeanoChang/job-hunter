"""job-hunter command line. Every command accepts --json; exit 0 normal, 2 systemic."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import typer

from jobhunter import __version__
from jobhunter.archive import ArchiveError, ArchiveStore, open_store
from jobhunter.archive.manifests import iter_manifests, latest_per_board
from jobhunter.config import ConfigError, Settings
from jobhunter.fetch import run as fetch_run
from jobhunter.http import Fetcher
from jobhunter.registry import RegistryError
from jobhunter.registry import load as load_registry
from jobhunter.timeutil import iso, utcnow

EXIT_SYSTEMIC = 2

app = typer.Typer(no_args_is_help=True, add_completion=False, help="job-hunter ingestion")
archive_app = typer.Typer(help="Inspect the raw archive")
registry_app = typer.Typer(help="Inspect companies.toml")
app.add_typer(archive_app, name="archive")
app.add_typer(registry_app, name="registry")


# Indirections so tests can substitute a mock transport and a fixed clock.
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


def _store(settings: Settings) -> ArchiveStore:
    try:
        return open_store(settings.archive_url)
    except (ValueError, ArchiveError) as e:
        typer.echo(f"archive error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e


def _emit(obj: Any, as_json: bool, human: str) -> None:
    typer.echo(json.dumps(obj, indent=None) if as_json else human)


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
                            dry_run=dry_run, now=_now)
    except RegistryError as e:
        typer.echo(f"registry error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    except ArchiveError as e:
        typer.echo(f"archive error: {e}")
        raise typer.Exit(EXIT_SYSTEMIC) from e
    finally:
        fetcher.close()
    counts = summary.counts()
    lines = [f"run {summary.run_id} — {counts['ok']}/{counts['boards']} boards ok, "
             f"{counts['new_blobs']} new blobs" + (" (dry run)" if dry_run else "")]
    for o in summary.outcomes:
        m = o.manifest
        detail = f"{m.record_count} records" if m.record_count is not None else (m.error or "")
        lines.append(f"  {o.board.key:32} {m.transport:11} {m.http_status or '-':>4}  {detail}")
    _emit(summary.to_dict(), as_json, "\n".join(lines))
    if counts["boards"] and counts["ok"] == 0:
        raise typer.Exit(EXIT_SYSTEMIC)


@app.command()
def status(as_json: bool = typer.Option(False, "--json")) -> None:
    """Per-board fetch health from the archive."""
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
    human = ["board                            last attempt          transport   status  records"]
    for r in rows:
        human.append(
            f"{r['board']:32} {r['last_attempt'] or 'never':21} "
            f"{r['last_transport'] or '-':11} {r['http_status'] or '-':>6}  "
            f"{r['record_count'] if r['record_count'] is not None else '-'}"
            + (f"  {r['error']}" if r["error"] else "")
        )
    if untracked:
        human.append(f"not in registry but present in archive: {', '.join(untracked)}")
    _emit({"boards": rows, "untracked": untracked}, as_json, "\n".join(human))


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


if __name__ == "__main__":
    app()
