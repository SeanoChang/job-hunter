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
    FINDINGS = 1  # verify: checks ran, findings failed
    USAGE = 2  # bad flag/argument shape
    CONFIG = 3  # missing/invalid JOB_HUNTER_* configuration
    NOT_FOUND = 4  # unknown or ambiguous identifier
    BACKEND = 5  # DB/archive/network unavailable
    SYSTEMIC = 6  # everything the old exit 2 meant


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
