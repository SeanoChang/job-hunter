"""Versioned v2 derivation grammars: the model cites spans, code computes values.

VALIDATOR_VERSION freezes this module together with v2/verify.py's checks; any
grammar or threshold change bumps it (identifier 9 was claimed by the v1 floor
repair, spec [A3]). Null-over-guess governs every branch.
"""

from __future__ import annotations

import re
from datetime import date as _date
from typing import Any

# validator/13 (11/12 are v1's): casefolded mention grounding (aliases/1
# consistency) and code-derived presence reconciliation in assemble.
# validator/14: typographic-tier binding (parsing-rules/5), F1 gate 0.70
# for v2 statement granularity, transport-retried sample slots.
VALIDATOR_VERSION = "14"

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
    # letter-bounded, not \b: "5yrs" must still parse (a \b fails between digit
    # and letter), while "more"/"most"/"percentage" never donate a prefix
    r"(?<![A-Za-z])(?:(?P<years>years?|yrs?|yoe)|(?P<months>months?|mos?)"
    r"|(?P<pct>%|percent)|times?\s+per\s+(?P<per>week|month|day))(?![A-Za-z])",
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


_SYMBOL_CURRENCY = {"£": "GBP", "€": "EUR"}  # single-currency symbols only; $ and ¥ stay null
_CODE = re.compile(
    r"\b(USD|CAD|AUD|NZD|SGD|HKD|EUR|GBP|JPY|CNY|CHF|SEK|INR|TWD|KRW)\b", re.IGNORECASE
)
_AMT = (
    r"([$£€¥]?)\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{1,3}(?:\.\d{3})+|\d+(?:\.\d{1,2})?)"
    r"\s*([kK])?"
)
_MONEY_RANGE = re.compile(_AMT + r"\s*(?:--?|–|—|to)\s*" + _AMT)
_MONEY_ONE = re.compile(_AMT)
_PERIOD = re.compile(
    r"(?P<hour>/\s*(?:hr|hour)|per\s+hour|hourly)|(?P<year>/\s*(?:yr|year)|per\s+(?:year|annum)"
    r"|annually|annual|yearly)|(?P<month>per\s+month|monthly)|(?P<week>per\s+week|weekly)"
    r"|(?P<day>per\s+day|daily)",
    re.IGNORECASE,
)


def _decimal(sym: str, digits: str, k: str | None) -> str | None:
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", digits):
        digits = digits.replace(".", "")  # European thousands separator
    digits = digits.replace(",", "")
    if k:
        if "." in digits:
            return None  # "1.5K" cents-vs-thousands: ambiguous
        digits = str(int(digits) * 1000)
    return digits


def _period(period_text: str | None) -> str | None:
    if period_text is None:
        return None
    m = _PERIOD.search(period_text)
    if m is None:
        return None
    return next(name for name in ("hour", "year", "month", "week", "day") if m.group(name))


def derive_money(value_text: str, comparison_text: str | None,
                 currency_text: str | None, period_text: str | None) -> dict[str, Any] | None:
    op = _comparison(comparison_text)
    if op == "?":
        return None
    currency: str | None = None
    code = _CODE.search(currency_text or "") or _CODE.search(value_text)
    if code:
        currency = code.group(1).upper()
    lo: str | None
    hi: str | None
    if m := _MONEY_RANGE.fullmatch(value_text.strip()):
        s1, d1, k1, s2, d2, k2 = m.groups()
        if s1 and s2 and s1 != s2:
            return None  # mixed symbols is not a range
        lo, hi = _decimal(s1, d1, k1), _decimal(s2, d2, k2)
        if lo is None or hi is None:
            return None
        if k2 and not k1 and float(lo) < 1000:
            lo = str(int(float(lo) * 1000))  # "$130 - $150K": trailing K covers both
        if float(lo) > float(hi):
            return None  # inverted: ambiguous
        if op is None:
            op = "range"
        else:
            return None  # comparison phrase over a range: ambiguous
        sym = s1 or s2
    elif m := _MONEY_ONE.fullmatch(value_text.strip()):
        sym, digits, k = m.groups()
        v = _decimal(sym, digits, k)
        if v is None:
            return None
        if op is None:
            op, lo, hi = "unstated", v, v
        elif op in ("gte", "gt"):
            lo, hi = v, None
        else:
            lo, hi = None, v
    else:
        return None  # malformed ("$228, 781") or absent amount: unparsed, kept
    if currency is None and sym:
        currency = _SYMBOL_CURRENCY.get(sym)
    return {"comparison": op, "min_amount": lo, "max_amount": hi,
            "currency": currency, "period": _period(period_text)}


_MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december"]
_MONTHS = {name: i + 1 for i, name in enumerate(_MONTH_NAMES)}
_MONTHS.update({name[:3]: i + 1 for i, name in enumerate(_MONTH_NAMES)})
_DATE_NAMED = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})")
_DATE_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}(?:\d{2})?)\b")


def derive_date(value_text: str) -> dict[str, Any] | None:
    for m in _DATE_NAMED.finditer(value_text):
        month = _MONTHS.get(m.group(1).lower())
        if month is None:
            continue
        try:
            return {"date": _date(int(m.group(3)), month, int(m.group(2))).isoformat(),
                    "candidates": None}
        except ValueError:
            return None
    if nm := _DATE_NUMERIC.search(value_text):
        a, b, yy = int(nm.group(1)), int(nm.group(2)), int(nm.group(3))
        year = yy + 2000 if yy < 100 else yy
        readings: list[str] = []
        for mm, dd in ((a, b), (b, a)):
            try:
                iso = _date(year, mm, dd).isoformat()
            except ValueError:
                continue
            if iso not in readings:
                readings.append(iso)
        if not readings:
            return None
        if len(readings) == 1:
            return {"date": readings[0], "candidates": None}
        return {"date": None, "candidates": sorted(readings)}  # locale-ambiguous
    return None
