"""Engine-facing tightening of emit schema 2 (frozen bytes stay frozen).

The verifier enforces a kind↔importance pairing (types.IMPORTANCE_KINDS): a
qualification/employment_constraint/hiring_policy statement carries a non-null
importance, every other kind must not. The first live v6 run failed 240/240
attempts overwhelmingly on exactly this, because nothing CONSTRAINED the model
— the stored contract allows null importance on any kind (assembly cannot
invent one), so the rule can only bind at emit time. This module rewrites the
statement definition the ENGINE receives into a discriminated union, making
the violation inexpressible. `validate_emit` still checks against the frozen
schema 2, so anything this guard admits remains contract-valid.
"""

from __future__ import annotations

import copy
from typing import Any

from jobhunter.l2.schemas import emit_schema
from jobhunter.l2.v2.types import IMPORTANCE_KINDS

_EVIDENCED_IMPORTANCE = ["required", "preferred", "not_required", "ambiguous"]


def _statement_variants(statement: dict[str, Any]) -> list[dict[str, Any]]:
    """Every conditional the verifier enforces on a statement, as a union.

    Axes: kind↔importance (IMPORTANCE_KINDS carry one, others must not),
    evidenced importance ⇒ importance_evidence present, and
    proficiency ⇒ proficiency_evidence present. 3 × 2 = 6 variants.
    """
    all_kinds: list[str] = list(statement["properties"]["kind"]["enum"])
    ruled = [k for k in all_kinds if k in IMPORTANCE_KINDS]
    unruled = [k for k in all_kinds if k not in IMPORTANCE_KINDS]
    evidence_present = {"$ref": "#/$defs/evidence"}

    # every union member carries an explicit "type": the OpenAI strict
    # validator rejects bare const/enum nodes ("schema must have a 'type' key")
    importance_axis = [
        {"kind": {"type": "string", "enum": ruled},
         "importance": {"type": "string", "enum": _EVIDENCED_IMPORTANCE},
         "importance_evidence": evidence_present},
        {"kind": {"type": "string", "enum": ruled},
         "importance": {"type": "string", "enum": ["unstated"]}},
        {"kind": {"type": "string", "enum": unruled},
         "importance": {"type": "null"},
         "importance_evidence": {"type": "null"}},
    ]
    proficiency_axis = [
        {"proficiency": {"type": "null"}, "proficiency_evidence": {"type": "null"}},
        {"proficiency": statement["properties"]["proficiency"],
         "proficiency_evidence": evidence_present},
    ]
    variants = []
    for imp in importance_axis:
        for prof in proficiency_axis:
            v = copy.deepcopy(statement)
            v["properties"].update(copy.deepcopy(imp))
            v["properties"].update(copy.deepcopy(prof))
            variants.append(v)
    return variants


def _fact_entry_variants(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-family field rules: date_kind ⇔ date; component ⇒ compensation;
    scope ⇒ experience/quantity (verify._check_facts)."""
    null = {"type": "null"}
    date_kinds = [k for k in entry["properties"]["date_kind"]["enum"] if k is not None]
    shapes = [
        {"family": {"type": "string", "enum": ["date"]},
         "date_kind": {"type": "string", "enum": date_kinds},
         "component": null, "scope": null},
        {"family": {"type": "string", "enum": ["compensation"]}, "date_kind": null,
         "component": entry["properties"]["component"], "scope": null},
        {"family": {"type": "string", "enum": ["experience", "quantity"]},
         "date_kind": null,
         "component": null, "scope": entry["properties"]["scope"]},
    ]
    variants = []
    for shape in shapes:
        v = copy.deepcopy(entry)
        v["properties"].update(copy.deepcopy(shape))
        variants.append(v)
    return variants


def _accounting_variants(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """excluded ⇒ exclusion_reason present; statements/facts ⇒ ref_ids
    non-empty; context/unresolved ⇒ reason null (verify accounting checks)."""
    null = {"type": "null"}
    reasons = [r for r in entry["properties"]["exclusion_reason"]["enum"] if r is not None]
    ref_ids = entry["properties"]["ref_ids"]
    shapes = [
        {"disposition": {"type": "string", "enum": ["excluded"]},
         "exclusion_reason": {"type": "string", "enum": reasons},
         "ref_ids": ref_ids},
        {"disposition": {"type": "string", "enum": ["statements", "facts"]},
         "exclusion_reason": null,
         "ref_ids": dict(copy.deepcopy(ref_ids), minItems=1)},
        {"disposition": {"type": "string", "enum": ["context", "unresolved"]},
         "exclusion_reason": null,
         "ref_ids": ref_ids},
    ]
    variants = []
    for shape in shapes:
        v = copy.deepcopy(entry)
        v["properties"].update(copy.deepcopy(shape))
        variants.append(v)
    return variants


def engine_emit_schema() -> dict[str, Any]:
    schema = copy.deepcopy(emit_schema("2"))
    schema["$defs"]["statement"] = {
        "anyOf": _statement_variants(schema["$defs"]["statement"])
    }
    schema["$defs"]["fact_entry"] = {
        "anyOf": _fact_entry_variants(schema["$defs"]["fact_entry"])
    }
    schema["$defs"]["accounting_entry"] = {
        "anyOf": _accounting_variants(schema["$defs"]["accounting_entry"])
    }
    return schema
