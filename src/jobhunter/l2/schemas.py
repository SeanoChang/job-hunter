"""Loader for packaged, versioned JSON Schemas (harness spec §3.3: checked in, archived)."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

import jsonschema


@cache
def _load(version: str, name: str) -> dict[str, Any]:
    root = resources.files("jobhunter.l2.schemas_data")
    path = root / version / f"{name}.schema.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KeyError(f"unknown schema version: {version}") from None
    data: dict[str, Any] = json.loads(raw)
    return data


def record_schema(version: str) -> dict[str, Any]:
    return _load(version, "record")


def emit_schema(version: str) -> dict[str, Any]:
    return _load(version, "emit")


def validate_record(extraction: dict[str, Any], version: str) -> list[str]:
    validator = jsonschema.Draft202012Validator(record_schema(version))
    errors = sorted(
        validator.iter_errors(extraction), key=lambda e: [str(p) for p in e.absolute_path]
    )
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors]
