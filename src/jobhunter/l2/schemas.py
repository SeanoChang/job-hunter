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


# --- strict-mode compatibility (validator/5, 2026-09-06) --------------------
# OpenAI's strict json_schema mode enforces the schema AT GENERATION — but it
# requires every property listed in `required` and rejects true optionals.
# strict_schema() converts: every property becomes required, formerly-optional
# ones become nullable. normalize_emit() strips those forced nulls back out,
# guided by the ORIGINAL schema, so local validation and assembly see the same
# emit a null-free model would have produced. The pair let strict mode kill
# the schema_invalid failure class (~690 attempts across two drain steps)
# without touching the canonical schema on disk.


def _nullable(node: dict[str, Any]) -> dict[str, Any]:
    if "anyOf" in node:
        variants = list(node["anyOf"])
        if not any(v.get("type") == "null" for v in variants if isinstance(v, dict)):
            variants.append({"type": "null"})
        return {**node, "anyOf": variants}
    t = node.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t):
        return node
    return {"anyOf": [node, {"type": "null"}]}


def _is_objectish(node: dict[str, Any]) -> bool:
    t = node.get("type")
    return t == "object" or (isinstance(t, list) and "object" in t)


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    if not isinstance(node, dict):
        return node
    if _is_objectish(node) and "properties" not in node:
        # strict mode cannot express "any object" (probe 34061138858, HTTP 400
        # on claim.threshold): bridge it as a JSON string; normalize_emit
        # parses it back per the ORIGINAL schema
        return {"anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "JSON object, serialized as a string"}
    out = {k: _strictify(v) for k, v in node.items()}
    if out.get("type") == "object" and "properties" in out:
        required = set(out.get("required", []))
        out["properties"] = {
            k: (v if k in required else _nullable(v)) for k, v in out["properties"].items()
        }
        out["required"] = sorted(out["properties"])
        out["additionalProperties"] = False
    return out


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A strict-mode variant of `schema`; the input is left untouched."""
    result: dict[str, Any] = _strictify(copy.deepcopy(schema))
    return result


def _resolve(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = root["$defs"][ref.removeprefix("#/$defs/")]
        return target if isinstance(target, dict) else node
    return node


def _normalize(value: Any, node: dict[str, Any], root: dict[str, Any]) -> Any:
    node = _resolve(node, root)
    if "anyOf" in node and isinstance(value, dict):
        for variant in node["anyOf"]:
            if isinstance(variant, dict) and _resolve(variant, root).get("type") == "object":
                return _normalize(value, variant, root)
    if isinstance(value, dict) and node.get("type") == "object" and "properties" in node:
        required = set(node.get("required", []))
        out = {}
        for k, v in value.items():
            child = node["properties"].get(k)
            if v is None and k not in required and child is not None:
                continue  # a strict-mode forced null on an optional key
            if (
                isinstance(v, str)
                and isinstance(child, dict)
                and _is_objectish(child)
                and "properties" not in child
            ):
                # the free-form-object string bridge, parsed back (or null
                # over guess when the string is not a JSON object)
                try:
                    parsed = json.loads(v)
                except ValueError:
                    parsed = None
                out[k] = parsed if isinstance(parsed, dict) else None
                continue
            out[k] = _normalize(v, child, root) if isinstance(child, dict) else v
        return out
    if isinstance(value, list) and isinstance(node.get("items"), dict):
        return [_normalize(v, node["items"], root) for v in value]
    return value


def normalize_emit(emit: dict[str, Any], version: str) -> dict[str, Any]:
    """Strip strict-mode nulls from optional keys, per the ORIGINAL schema."""
    schema = _load(version, "emit")
    result = _normalize(emit, schema, schema)
    assert isinstance(result, dict)
    return result
