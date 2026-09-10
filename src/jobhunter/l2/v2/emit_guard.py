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


def engine_emit_schema() -> dict[str, Any]:
    schema = copy.deepcopy(emit_schema("2"))
    statement = schema["$defs"]["statement"]
    all_kinds: list[str] = list(statement["properties"]["kind"]["enum"])
    ruled = [k for k in all_kinds if k in IMPORTANCE_KINDS]
    unruled = [k for k in all_kinds if k not in IMPORTANCE_KINDS]

    with_importance = copy.deepcopy(statement)
    with_importance["properties"]["kind"] = {"enum": ruled}
    with_importance["properties"]["importance"] = {"$ref": "#/$defs/importance"}

    without_importance = copy.deepcopy(statement)
    without_importance["properties"]["kind"] = {"enum": unruled}
    without_importance["properties"]["importance"] = {"type": "null"}

    schema["$defs"]["statement"] = {"anyOf": [with_importance, without_importance]}
    return schema
