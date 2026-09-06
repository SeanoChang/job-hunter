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

VALIDATOR_VERSION = "6"

_RANGE = re.compile(r"(\d+)\s*(?:-|–|—|to|and)\s*(\d+)\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)
_FLOOR = re.compile(
    r"(\d+)\s*(?:\+|or\s+more)\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE
)  # validator/4: Workday writes "5 or more years" (step-1 review, 2026-09-06)
_EXACT = re.compile(r"(\d+)\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)

# validator/2: currency is retained as written, never converted. A symbol
# implies a currency only when it maps to exactly one — £ and € do; $ (USD,
# CAD, AUD, SGD, HKD, NZD) and ¥ (JPY, CNY) do not, so those stay null unless
# the posting writes a code. Both sides of a range must use the same symbol.
_SYMBOL_CURRENCY = {"£": "GBP", "€": "EUR"}
# validator/4: decimal cents appear on Workday ("$169,100.00"); the cents are
# matched and discarded — amounts stay whole units. validator/6: European
# dot-thousands ("71.000" is 71000; three digits after a dot are never cents).
_AMOUNT = r"(\d{1,3}(?:,\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)(?:\.\d{1,2})?\s*(k)?"
# validator/4: an optional currency code may trail EACH amount even when a
# symbol leads it ("$123,900 USD - $222,000 USD", step-1 review 2026-09-06).
# validator/6: the second symbol may be omitted ("$179,500 - 269,300").
_MONEY = re.compile(
    r"([$£€¥])\s*" + _AMOUNT + r"(?:\s*[A-Z]{3})?\s*(?:--?|–|—|to)\s*"
    r"([$£€¥]?)\s*" + _AMOUNT,
    re.IGNORECASE,
)
# validator/3: Workday postings write the code after the amount with no symbol
# at all — "136,000 USD - 218,500 USD for Level 3" (NVIDIA canary, SEA-186).
# The trailing code names the currency; a leading one, when present, must match.
_CODE = r"(USD|CAD|AUD|NZD|SGD|HKD|EUR|GBP|JPY|CNY|CHF|SEK|INR|TWD|KRW)"
_MONEY_CODE = re.compile(
    _AMOUNT + r"\s*" + _CODE + r"?\s*(?:--?|–|—|to)\s*"
    + _AMOUNT + r"\s+" + _CODE + r"\b",
    re.IGNORECASE,
)
# validator/6: the code may LEAD each amount ("EUR 71.000 to EUR 95.000").
_MONEY_CODE_LEAD = re.compile(
    _CODE + r"\s*" + _AMOUNT + r"\s*(?:--?|–|—|to)\s*"
    + _CODE + r"\s*" + _AMOUNT,
    re.IGNORECASE,
)
_HOURLY = re.compile(r"(?:/\s*(?:hr|hour)|per\s+hour)\b", re.IGNORECASE)
_YEARLY = re.compile(r"(?:/\s*(?:yr|year)|per\s+(?:year|annum)|annually|annual)\b", re.IGNORECASE)
_CURRENCY = re.compile(
    r"\b(USD|CAD|AUD|NZD|SGD|HKD|EUR|GBP|JPY|CNY|CHF|SEK|INR|TWD|KRW)\b", re.IGNORECASE
)  # explicit codes

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]  # explicit, not calendar.month_name: that table is locale-dependent
_MONTHS = {name: i + 1 for i, name in enumerate(_MONTH_NAMES)}
_MONTHS.update({name[:3]: i + 1 for i, name in enumerate(_MONTH_NAMES)})
_DATE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})")
# validator/4: Workday deadlines also come numeric ("09/11/26", "12/01/2026")
_DATE_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}(?:\d{2})?)\b")


def parse_experience_months(text: str) -> dict[str, object] | None:
    if m := _RANGE.search(text):
        lo, hi = int(m.group(1)) * 12, int(m.group(2)) * 12
        if lo > hi:
            return None  # descending range: ambiguous
        return {"min": lo, "max": hi}
    if m := _FLOOR.search(text):
        return {"min": int(m.group(1)) * 12, "max": None}
    exacts = _EXACT.findall(text)
    if len(exacts) == 1:
        months = int(exacts[0]) * 12
        return {"min": months, "max": months}
    return None  # zero tokens, or several without range syntax: ambiguous


def _amount(digits: str, k_suffix: str | None) -> int:
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", digits):
        digits = digits.replace(".", "")  # European thousands separator
    value = int(digits.replace(",", ""))
    return value * 1000 if k_suffix else value


def parse_compensation(text: str) -> dict[str, object] | None:
    currency: str | None
    if m := _MONEY.search(text):
        sym_lo, lo_digits, lo_k, sym_hi, hi_digits, hi_k = m.groups()
        if sym_hi and sym_lo != sym_hi:
            return None  # "£100,000 - €120,000" is not a range
        code = _CURRENCY.search(text)
        currency = code.group(1).upper() if code else _SYMBOL_CURRENCY.get(sym_lo)
    elif m := _MONEY_CODE_LEAD.search(text):
        code_lo, lo_digits, lo_k, code_hi, hi_digits, hi_k = m.groups()
        if code_lo.upper() != code_hi.upper():
            return None
        currency = code_hi.upper()
    elif m := _MONEY_CODE.search(text):
        lo_digits, lo_k, code_lo, hi_digits, hi_k, code_hi = m.groups()
        if code_lo and code_lo.upper() != code_hi.upper():
            return None  # "100,000 USD - 120,000 EUR" is not a range
        currency = code_hi.upper()
    else:
        return None
    lo = _amount(lo_digits, lo_k)
    hi = _amount(hi_digits, hi_k)
    if hi_k and not lo_k and lo < 1000:
        lo *= 1000  # "$130 - $150K": the trailing K covers both bounds
    if lo > hi:
        return None  # inverted range: ambiguous
    period = "hour" if _HOURLY.search(text) else "year" if _YEARLY.search(text) else None
    return {"min": lo, "max": hi, "currency": currency, "period": period}


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
    for m in _DATE_NUMERIC.finditer(text):
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = yy + 2000 if yy < 100 else yy  # postings never cite the 1900s
        try:
            parsed = date(year, mm, dd)
        except ValueError:
            return None  # impossible calendar date in the anchor
        found.append(parsed.isoformat())
    if len(found) != 1:
        return None  # zero dates, or several: ambiguous
    return {"date": found[0]}


# keyed off the constant: a version bump that forgot to re-key this table
# raised KeyError at verify time (caught by tests, 2026-08-28)
TRANSFORMS: dict[str, dict[str, Callable[[str], dict[str, object] | None]]] = {
    VALIDATOR_VERSION: {
        "experience_months": parse_experience_months,
        "compensation": parse_compensation,
        "deadline": parse_deadline,
    }
}
