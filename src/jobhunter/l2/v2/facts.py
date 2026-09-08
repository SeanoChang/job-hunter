"""Versioned v2 derivation grammars: the model cites spans, code computes values.

VALIDATOR_VERSION freezes this module together with v2/verify.py's checks; any
grammar or threshold change bumps it (identifier 9 was claimed by the v1 floor
repair, spec [A3]). Null-over-guess governs every branch.
"""

from __future__ import annotations

import re
from typing import Any

VALIDATOR_VERSION = "10"

_CMP_PHRASES: list[tuple[str, str]] = [
    (r"at\s+least|a\s+minimum\s+of|minimum\s+of|minimum|no\s+less\s+than", "gte"),
    (r"more\s+than|over|greater\s+than", "gt"),
    (r"up\s+to|at\s+most|no\s+more\s+than|maximum\s+of|maximum", "lte"),
    (r"less\s+than|fewer\s+than|under", "lt"),
]

_NUM = r"(\d+(?:\.\d+)?)"
_RANGE = re.compile(_NUM + r"\s*(?:-|–|—|to)\s*" + _NUM)
_PLUS = re.compile(_NUM + r"\s*\+")
_SINGLE = re.compile(_NUM)
_UNIT = re.compile(
    r"(?P<years>years?|yrs?|yoe)|(?P<months>months?|mos?)|(?P<pct>%|percent)"
    r"|(?:times?\s+per\s+(?P<per>week|month|day))",
    re.IGNORECASE,
)
_HAS_ALPHA = re.compile(r"[A-Za-z]")


def _comparison(comparison_text: str | None) -> str | None:
    if comparison_text is None:
        return None
    text = comparison_text.strip()
    for pattern, op in _CMP_PHRASES:
        # fullmatch, not search: a phrase must consume the whole cited span.
        # "no more than" is one of the lte alternatives below and fullmatches
        # it outright; "not more than" or "greater than or equal to" merely
        # *contain* a gt/lt alternative as a substring and must NOT match it
        # (null-over-guess: unknown comparison grammar is unparsed, never a
        # guessed operator borrowed from an unrelated phrase it happens to
        # embed).
        if re.fullmatch(pattern, text, re.IGNORECASE):
            return op
    return "?"  # comparison evidence present but not in the grammar: unparsed


def derive_quantity(value_text: str, comparison_text: str | None) -> dict[str, Any] | None:
    op = _comparison(comparison_text)
    if op == "?":
        return None
    unit_m = _UNIT.search(value_text)
    if unit_m and unit_m.group("years"):
        dimension, unit, scale = "duration", "month", 12.0
    elif unit_m and unit_m.group("months"):
        dimension, unit, scale = "duration", "month", 1.0
    elif unit_m and unit_m.group("pct"):
        dimension, unit, scale = "percentage", "percent", 1.0
    elif unit_m and unit_m.group("per"):
        dimension, unit, scale = "frequency", f"per_{unit_m.group('per').lower()}", 1.0
    elif _HAS_ALPHA.search(value_text):
        return None  # unit word outside the known grammar: a gap, never a guessed count
    else:
        dimension, unit, scale = "count", None, 1.0

    def num(raw: str) -> float | int:
        v = float(raw) * scale
        return int(v) if v == int(v) else v

    lo: float | int | None
    hi: float | int | None
    if m := _RANGE.search(value_text):
        lo, hi = num(m.group(1)), num(m.group(2))
        if op is None:
            if lo > hi:
                return None  # descending: ambiguous
            return _q(dimension, "range", lo, hi, True, True, unit)
        return None  # a comparison phrase over a range: ambiguous, unparsed
    if m := _PLUS.search(value_text):
        if op is None:
            return _q(dimension, "gte", num(m.group(1)), None, True, None, unit)
        return None
    singles = _SINGLE.findall(value_text)
    if len(singles) != 1:
        return None  # zero numbers, or several without range syntax
    v = num(singles[0])
    if op is None:
        return _q(dimension, "unstated", v, v, None, None, unit)
    if op in ("gte", "gt"):
        return _q(dimension, op, v, None, op == "gte", None, unit)
    return _q(dimension, op, None, v, None, op == "lte", unit)


def _q(dimension: str, comparison: str, lo: float | int | None, hi: float | int | None,
       inc_lo: bool | None, inc_hi: bool | None, unit: str | None) -> dict[str, Any]:
    return {"dimension": dimension, "comparison": comparison, "min_value": lo,
            "max_value": hi, "inclusive_min": inc_lo, "inclusive_max": inc_hi,
            "unit": unit}
