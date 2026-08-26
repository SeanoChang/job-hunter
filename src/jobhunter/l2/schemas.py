"""Loader for packaged, versioned JSON Schemas (harness spec §3.3: checked in, archived)."""

from __future__ import annotations

import copy
import json
from functools import cache
from importlib import resources
from typing import Any

import jsonschema


@cache
def _versions() -> frozenset[str]:
    root = resources.files("jobhunter.l2.schemas_data")
    return frozenset(entry.name for entry in root.iterdir() if entry.is_dir())


@cache
def _load(version: str, name: str) -> dict[str, Any]:
    if version not in _versions():
        # allowlist before any path join: "1/../1" or an absolute value must
        # never resolve to a packaged (or arbitrary) schema file
        raise KeyError(f"unknown schema version: {version}")
    root = resources.files("jobhunter.l2.schemas_data")
    path = root / version / f"{name}.schema.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, ValueError):
        raise KeyError(f"unknown schema version: {version}") from None
    data: dict[str, Any] = json.loads(raw)
    return data


@cache
def _validator(version: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_load(version, "record"))


@cache
def _emit_validator(version: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_load(version, "emit"))


def validate_emit(emit: dict[str, Any], version: str) -> list[str]:
    errors = sorted(
        _emit_validator(version).iter_errors(emit),
        key=lambda e: [str(p) for p in e.absolute_path],
    )
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors]


def record_schema(version: str) -> dict[str, Any]:
    return copy.deepcopy(_load(version, "record"))  # copies: the cached dict must stay pristine


def emit_schema(version: str) -> dict[str, Any]:
    return copy.deepcopy(_load(version, "emit"))


def validate_record(extraction: dict[str, Any], version: str) -> list[str]:
    errors = sorted(
        _validator(version).iter_errors(extraction), key=lambda e: [str(p) for p in e.absolute_path]
    )
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors]
