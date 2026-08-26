"""Versioned L1 fact transforms: the LLM points at a span, code computes the value.

Facts are re-derived from anchor text and compared structurally — never checked as
literal numbers (harness spec §3.3 facts_rederive; parsing-direction review
finding 6: "0-2 YOE" never contains "24"). The grammar below is frozen as part of
VALIDATOR_VERSION. Null-over-guess governs every branch: unparseable, ambiguous
(two dates, two year-tokens without range syntax, an inverted money range), or
unstated (currency/period without an explicit marker) all derive None/null.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date

VALIDATOR_VERSION = "1"

_RANGE = re.compile(r"(\d+)\s*(?:-|–|—|to|and)\s*(\d+)\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)
_FLOOR = re.compile(r"(\d+)\s*\+\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)
_EXACT = re.compile(r"(\d+)\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)

_MONEY = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})*|\d+)\s*(k)?\s*(?:-|–|—|to)\s*"
    r"\$\s*(\d{1,3}(?:,\d{3})*|\d+)\s*(k)?",
    re.IGNORECASE,
)
_HOURLY = re.compile(r"(?:/\s*(?:hr|hour)|per\s+hour)\b", re.IGNORECASE)
_YEARLY = re.compile(r"(?:/\s*(?:yr|year)|per\s+(?:year|annum)|annually|annual)\b", re.IGNORECASE)
_CURRENCY = re.compile(r"\b(USD|CAD|AUD|NZD|SGD|HKD|EUR|GBP)\b")  # explicit codes only

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]  # explicit, not calendar.month_name: that table is locale-dependent
_MONTHS = {name: i + 1 for i, name in enumerate(_MONTH_NAMES)}
_MONTHS.update({name[:3]: i + 1 for i, name in enumerate(_MONTH_NAMES)})
_DATE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})")


def parse_experience_months(text: str) -> dict[str, object] | None:
    if m := _RANGE.search(text):
        return {"min": int(m.group(1)) * 12, "max": int(m.group(2)) * 12}
    if m := _FLOOR.search(text):
        return {"min": int(m.group(1)) * 12, "max": None}
    exacts = _EXACT.findall(text)
    if len(exacts) == 1:
        months = int(exacts[0]) * 12
        return {"min": months, "max": months}
    return None  # zero tokens, or several without range syntax: ambiguous


def _amount(digits: str, k_suffix: str | None) -> int:
    value = int(digits.replace(",", ""))
    return value * 1000 if k_suffix else value


def parse_compensation(text: str) -> dict[str, object] | None:
    m = _MONEY.search(text)
    if not m:
        return None
    lo = _amount(m.group(1), m.group(2))
    hi = _amount(m.group(3), m.group(4))
    if m.group(4) and not m.group(2) and lo < 1000:
        lo *= 1000  # "$130 - $150K": the trailing K covers both bounds
    if lo > hi:
        return None  # inverted range: ambiguous
    currency = _CURRENCY.search(text)
    period = "hour" if _HOURLY.search(text) else "year" if _YEARLY.search(text) else None
    return {
        "min": lo,
        "max": hi,
        "currency": currency.group(1) if currency else None,
        "period": period,
    }


def parse_deadline(text: str) -> dict[str, object] | None:
    found: list[str] = []
    for m in _DATE.finditer(text):
        month = _MONTHS.get(m.group(1).lower())
        if month is None:
            continue
        try:
            parsed = date(int(m.group(3)), month, int(m.group(2)))
        except ValueError:
            return None  # impossible calendar date in the anchor
        found.append(parsed.isoformat())
    if len(found) != 1:
        return None  # zero dates, or several: ambiguous
    return {"date": found[0]}


TRANSFORMS: dict[str, dict[str, Callable[[str], dict[str, object] | None]]] = {
    "1": {
        "experience_months": parse_experience_months,
        "compensation": parse_compensation,
        "deadline": parse_deadline,
    }
}
