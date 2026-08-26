"""Versioned L1 fact transforms: the LLM points at a span, code computes the value.

Facts are re-derived from anchor text and compared structurally — never checked as
literal numbers (harness spec §3.3 facts_rederive; parsing-direction review
finding 6: "0-2 YOE" never contains "24"). The grammar below is frozen as part of
VALIDATOR_VERSION; unparseable text returns None (null-over-guess).
"""

from __future__ import annotations

import re
from collections.abc import Callable

VALIDATOR_VERSION = "1"

_RANGE = re.compile(r"(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)
_FLOOR = re.compile(r"(\d+)\s*\+\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)
_EXACT = re.compile(r"(\d+)\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)

_MONEY = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})*|\d+)\s*(k)?\s*(?:-|–|—|to)\s*"
    r"\$\s*(\d{1,3}(?:,\d{3})*|\d+)\s*(k)?",
    re.IGNORECASE,
)
_HOURLY = re.compile(r"(?:/\s*(?:hr|hour)|per\s+hour)\b", re.IGNORECASE)

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ]  # explicit, not calendar.month_name: that table is locale-dependent
    )
}
_DATE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")


def parse_experience_months(text: str) -> dict[str, object] | None:
    if m := _RANGE.search(text):
        return {"min": int(m.group(1)) * 12, "max": int(m.group(2)) * 12}
    if m := _FLOOR.search(text):
        return {"min": int(m.group(1)) * 12, "max": None}
    if m := _EXACT.search(text):
        months = int(m.group(1)) * 12
        return {"min": months, "max": months}
    return None


def _amount(digits: str, k_suffix: str | None) -> int:
    value = int(digits.replace(",", ""))
    return value * 1000 if k_suffix else value


def parse_compensation(text: str) -> dict[str, object] | None:
    m = _MONEY.search(text)
    if not m:
        return None
    period = "hour" if _HOURLY.search(text) else "year"
    return {
        "min": _amount(m.group(1), m.group(2)),
        "max": _amount(m.group(3), m.group(4)),
        "currency": "USD",
        "period": period,
    }


def parse_deadline(text: str) -> dict[str, object] | None:
    m = _DATE.search(text)
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if month is None:
        return None
    return {"date": f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"}


TRANSFORMS: dict[str, dict[str, Callable[[str], dict[str, object] | None]]] = {
    "1": {
        "experience_months": parse_experience_months,
        "compensation": parse_compensation,
        "deadline": parse_deadline,
    }
}
