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

# validator/7: the possible_omission completeness warning (verify._check_omissions)
# validator/8: "at least N years" is a floor; omission scan skips boilerplate lines
# validator/9: "minimum (of) N", "more than N", "over N" are floors (audit 2026-09-06
# defect 1, C01: 72 validated rows held a false exact interval). The v1 record
# cannot express a strict floor, so "more than N" maps to the inclusive
# {"min": N*12, "max": null} — conservative, no invented upper bound; v2's
# comparison operators represent gt exactly. Negated phrases ("no more than",
# "not more than") are ceilings, not floors, and keep the exact fallback.
# validator/11 (10 is v2's): a single stated amount with a currency signal
# (symbol or code) is a point value {min == max} — the quarantine audit
# 2026-09-09 found 344 docs whose only failure was a one-figure salary the
# range grammar refused. "up to"/"at most" wordings become {min: null},
# "from"/"starting at"/"at least" become {max: null}; a bare number with no
# currency signal, or several amounts without range syntax, stays None.
# validator/12 (2026-09-10, plan docs/superpowers/plans/
# 2026-09-10-quarantine-recovery-and-drain-automation.md Task 1):
# international compensation forms. 91 quarantined anchors were
# unparseable: multi-character currency signs (CA$, zł, Kč, RM — "kr" is
# matched as a sign but never implies a currency, DKK/SEK/NOK ambiguity
# resolves only through an explicit code), space-thousands and
# decimal-comma amounts, the Unicode minus family as a range separator, and
# an inline "/hour"-style period fragment inside a bound. A bare numeric
# range with no currency signal at all is accepted only when an explicit
# period marker (annually, /hour, ...) elsewhere in the text supplies the
# compensation evidence — 11's shapes are otherwise unchanged.
VALIDATOR_VERSION = "12"

_RANGE = re.compile(r"(\d+)\s*(?:-|–|—|to|and)\s*(\d+)\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)
_FLOOR = re.compile(
    r"(?:(\d+)\s*(?:\+|or\s+more)"
    r"|\b(?:at\s+least|a\s+minimum\s+of|minimum\s+of|minimum"
    r"|(?<!\bno\s)(?<!\bnot\s)more\s+than|(?<!\bno\s)(?<!\bnot\s)over)\s+(\d+))"
    r"\s*(?:years?|yrs?|yoe)\b",
    re.IGNORECASE,
)  # the leading \b keeps "turnover 5 years" from matching the "over" branch
# validator/8: "at least 5 years" is a floor, not an exact (omission-sample review)
_EXACT = re.compile(r"(\d+)\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)

# validator/2: currency is retained as written, never converted. A symbol
# implies a currency only when it maps to exactly one — £ and € do; $ (USD,
# CAD, AUD, SGD, HKD, NZD) and ¥ (JPY, CNY) do not, so those stay null unless
# the posting writes a code. Both sides of a range must use the same symbol.
_SYMBOL_CURRENCY = {"£": "GBP", "€": "EUR"}
# validator/12: multi-character currency signs, each mapping to exactly one
# currency. "kr" is deliberately absent — DKK/SEK/NOK ambiguity resolves
# only through an explicit code (null-over-guess), never a guess at "the
# most common kr country". Keyed lowercase; looked up case-insensitively.
_PREFIX_CURRENCY = {
    "ca$": "CAD", "a$": "AUD", "nz$": "NZD", "s$": "SGD", "hk$": "HKD",
    "r$": "BRL", "zł": "PLN", "kč": "CZK", "rm": "MYR",
}
# validator/12: the multi-character signs are letter-led, so each one takes
# a (?<![A-Za-z]) guard — "okr" donates no "kr", "firm" donates no "RM".
# The single-character symbols ($£€¥) need no guard: they are never a
# substring of an ordinary word. One capturing group overall (design: "the
# sign alternation stays one group").
_SIGN = r"(?:(?<![A-Za-z])(?:CA\$|NZ\$|HK\$|A\$|S\$|R\$|zł|Kč|RM|kr)|[$£€¥])"


def _sign_currency(sign: str) -> str | None:
    return _SYMBOL_CURRENCY.get(sign) or _PREFIX_CURRENCY.get(sign.lower())


# validator/4: decimal cents appear on Workday ("$169,100.00"); the cents are
# matched and discarded — amounts stay whole units. validator/6: European
# dot-thousands ("71.000" is 71000; three digits after a dot are never cents).
# validator/12: space-thousands ("210 300") and a decimal-comma tail
# ("130.600,00" — the comma tail is discarded exactly like a cents dot).
_AMOUNT = r"(\d{1,3}(?:[, ]\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(k)?"
# validator/12: range separators, including the Unicode minus family
# (− U+2212, in addition to the hyphen/en dash/em dash already supported).
_SEP = r"(?:--?|–|—|−|to)"
# validator/12: an optional period fragment may sit inside a bound, before
# an optional trailing code — "$40/hour to $65/hour", "200,000/year SGD".
_PERIOD_FRAG = r"(?:\s*/\s*(?:hr|hour|yr|year|mo|month))?"
# validator/4: an optional currency code may trail EACH amount even when a
# symbol leads it ("$123,900 USD - $222,000 USD", step-1 review 2026-09-06).
# validator/6: the second symbol may be omitted ("$179,500 - 269,300").
_MONEY = re.compile(
    r"(" + _SIGN + r")\s*" + _AMOUNT + _PERIOD_FRAG
    + r"(?:\s*[A-Z]{3})?\s*" + _SEP + r"\s*"
    + r"(" + _SIGN + r"?)\s*" + _AMOUNT + _PERIOD_FRAG,
    re.IGNORECASE,
)
# validator/3: Workday postings write the code after the amount with no symbol
# at all — "136,000 USD - 218,500 USD for Level 3" (NVIDIA canary, SEA-186).
# The trailing code names the currency; a leading one, when present, must
# match. validator/12 grows the list with DKK/NOK/PLN/CZK/MYR/BRL/MXN/ILS.
_CODE = (
    r"(USD|CAD|AUD|NZD|SGD|HKD|EUR|GBP|JPY|CNY|CHF|SEK|INR|TWD|KRW"
    r"|DKK|NOK|PLN|CZK|MYR|BRL|MXN|ILS)"
)
_MONEY_CODE = re.compile(
    _AMOUNT + _PERIOD_FRAG + r"\s*" + _CODE + r"?\s*" + _SEP + r"\s*"
    + _AMOUNT + _PERIOD_FRAG + r"\s*" + _CODE + r"\b",
    re.IGNORECASE,
)
# validator/6: the code may LEAD each amount ("EUR 71.000 to EUR 95.000").
_MONEY_CODE_LEAD = re.compile(
    _CODE + r"\s*" + _AMOUNT + _PERIOD_FRAG + r"\s*" + _SEP + r"\s*"
    + _CODE + r"\s*" + _AMOUNT + _PERIOD_FRAG,
    re.IGNORECASE,
)
# validator/12: a bare numeric range with no currency signal at all is
# money evidence only when an explicit period marker (annually, /hour, ...)
# appears elsewhere in the text — "65,000−87,500 OTE annually". Without a
# period marker a bare range stays a correct refusal (validator/3: "not
# evidently money"); the gate lives in parse_compensation, not here.
_MONEY_BARE = re.compile(
    _AMOUNT + _PERIOD_FRAG + r"\s*" + _SEP + r"\s*" + _AMOUNT + _PERIOD_FRAG,
    re.IGNORECASE,
)
# validator/11: single-amount forms, tried only after every range form fails.
_MONEY_TOKEN = re.compile(
    _SIGN + r"\s*" + _AMOUNT + _PERIOD_FRAG + r"|"
    + _AMOUNT + _PERIOD_FRAG + r"\s*" + _CODE + r"\b|"
    + _CODE + r"\s*" + _AMOUNT + _PERIOD_FRAG,
    re.IGNORECASE,
)
_MONEY_ONE = re.compile(r"(" + _SIGN + r")\s*" + _AMOUNT + _PERIOD_FRAG, re.IGNORECASE)
_MONEY_ONE_CODE = re.compile(
    _AMOUNT + _PERIOD_FRAG + r"\s*" + _CODE + r"\b", re.IGNORECASE
)
_MONEY_ONE_CODE_LEAD = re.compile(
    _CODE + r"\s*" + _AMOUNT + _PERIOD_FRAG, re.IGNORECASE
)
_CEILING_WORDS = re.compile(
    r"\b(?:up\s+to|at\s+most|maximum\s+of|no\s+more\s+than|not\s+to\s+exceed)\b", re.IGNORECASE
)
_FLOOR_WORDS = re.compile(
    r"\b(?:starting\s+(?:at|from)|from|at\s+least|minimum\s+of)\b", re.IGNORECASE
)
_HOURLY = re.compile(r"(?:/\s*(?:hr|hour)|per\s+hour)\b", re.IGNORECASE)
_YEARLY = re.compile(r"(?:/\s*(?:yr|year)|per\s+(?:year|annum)|annually|annual)\b", re.IGNORECASE)
_CURRENCY = re.compile(
    r"\b(USD|CAD|AUD|NZD|SGD|HKD|EUR|GBP|JPY|CNY|CHF|SEK|INR|TWD|KRW"
    r"|DKK|NOK|PLN|CZK|MYR|BRL|MXN|ILS)\b",
    re.IGNORECASE,
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
        return {"min": int(m.group(1) or m.group(2)) * 12, "max": None}
    exacts = _EXACT.findall(text)
    if len(exacts) == 1:
        months = int(exacts[0]) * 12
        return {"min": months, "max": months}
    return None  # zero tokens, or several without range syntax: ambiguous


def _amount(digits: str, k_suffix: str | None) -> int:
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", digits):
        digits = digits.replace(".", "")  # European thousands separator
    value = int(digits.replace(",", "").replace(" ", ""))  # validator/12: space-thousands
    return value * 1000 if k_suffix else value


def parse_compensation(text: str) -> dict[str, object] | None:
    currency: str | None
    if m := _MONEY.search(text):
        sym_lo, lo_digits, lo_k, sym_hi, hi_digits, hi_k = m.groups()
        if sym_hi and sym_lo != sym_hi:
            return None  # "£100,000 - €120,000" is not a range
        code = _CURRENCY.search(text)
        currency = code.group(1).upper() if code else _sign_currency(sym_lo)
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
    elif (m := _MONEY_BARE.search(text)) and (_HOURLY.search(text) or _YEARLY.search(text)):
        # validator/12: no symbol, no code — only a period marker (elsewhere
        # in the text) makes a bare numeric range evidently compensation.
        lo_digits, lo_k, hi_digits, hi_k = m.groups()
        currency = None
    else:
        return _single_amount(text)
    lo = _amount(lo_digits, lo_k)
    hi = _amount(hi_digits, hi_k)
    if hi_k and not lo_k and lo < 1000:
        lo *= 1000  # "$130 - $150K": the trailing K covers both bounds
    if lo > hi:
        return None  # inverted range: ambiguous
    period = "hour" if _HOURLY.search(text) else "year" if _YEARLY.search(text) else None
    return {"min": lo, "max": hi, "currency": currency, "period": period}


def _single_amount(text: str) -> dict[str, object] | None:
    """validator/11: one amount with a currency signal is a point value."""
    if len(_MONEY_TOKEN.findall(text)) != 1:
        return None  # zero signals, or several amounts without range syntax
    currency: str | None
    if m := _MONEY_ONE.search(text):
        symbol, digits, k = m.groups()
        code = _CURRENCY.search(text)
        currency = code.group(1).upper() if code else _sign_currency(symbol)
    elif m := _MONEY_ONE_CODE.search(text):
        digits, k, code_txt = m.groups()
        currency = code_txt.upper()
    elif m := _MONEY_ONE_CODE_LEAD.search(text):
        code_txt, digits, k = m.groups()
        currency = code_txt.upper()
    else:
        return None
    value = _amount(digits, k)
    period = "hour" if _HOURLY.search(text) else "year" if _YEARLY.search(text) else None
    lo: int | None = value
    hi: int | None = value
    if _CEILING_WORDS.search(text):
        lo = None  # "up to $180,000": a ceiling, no invented floor
    elif _FLOOR_WORDS.search(text):
        hi = None  # "starting at $140,000": a floor, no invented ceiling
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
