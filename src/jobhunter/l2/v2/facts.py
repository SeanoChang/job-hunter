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
# validator/14: typographic-tier binding (parsing-rules/5), transport-retried
# sample slots. validator/15: emphasis-fold binding and code-owned occurrence
# (parsing-rules/6), sample content-repair budget; the v2 F1 gate returns to
# 0.80 pending an adjudicated comparator calibration (2026-09-11 analysis).
# validator/16: settlement reads `semantic-audit/v1`. A complete cohort that
# disagrees but whose audited medoid carries no blocking findings settles
# `validated` with sampling `adjudicated`; the audit dimensions and blocking
# count gate `search_eligible` without demoting a structurally valid record.
# Publication follows settlement: only a `validated` record can be eligible,
# so a clean audit never publishes a candidate a reviewer parked or rejected.
# validator/17: assemble rejects control characters (other than \n\t) in any
# emitted string — codex smuggled a NUL into a statement topic (2026-09-12,
# doc 3ad988f4) and the record crossed every check to die at the jsonb
# boundary; now it is a content error the retry loop hands back to the model.
# validator/18: settlement gets the DISPUTE SET (`agreement.dispute_set`) and
# adjudicates against it. A complete cohort that disagrees settles `validated`
# /`adjudicated` when its audit's blocking items all land off what the samples
# split over — the medoid statements no sibling aligns, the blocks only a
# sibling cites, and the finding codes of the failed gate's own dimension
# (`state.GATE_DIMENSION_CODES`). An INCOMPLETE cohort adjudicates only on the
# whole-record question validator/16 asked, and a reopened adjudication goes
# back to the cohort verdict it settled. Off-dispute findings keep gating
# `search_eligible` exactly as they do for a cohort that agreed: adjudication
# moves the lifecycle, never the eligibility rules. The method is the
# 2026-09-13 adversarial verifier's — naive statement-id overlap between
# samples was refuted there at 4 false clears in 9 documents.
# validator/19: `derive_quantity` reads the UNIT ANCHOR the emit cites as its
# own span, in assembly and in verification alike — an emit citing value "12+"
# and unit "years" derived a dimensionless count of 12, and no check could see
# it because both call sites made the same argument-short call (2026-09-15
# external review, finding 1). An anchor outside the grammar forces
# `present_unparsed`, never a guessed dimension. Accounting gains
# `coverage_unevidenced`: a `statements` disposition asserts a block was
# extracted INTO the objects it names, so at least one of them must cite that
# block — 14 duty bullets "accounted" to statements evidenced from the intro
# paragraph is the laundering shape this closes (2026-09-14 analysis, shape
# (a)); partial coverage of a block stays the auditor's question. The
# requirement-language tripwire widens to `context` blocks as the warning
# `context_requirement_language` (shape (b): whole sections dropped as context
# when their content maps to no statement kind).
# validator/20: the parser stops issuing verdicts (parsing contract v3 §2.1).
# Statements under SCHEMA 3 carry no `importance`/`proficiency` — labels the
# 2026-09-22 review-queue analysis showed one model disagreeing with itself
# across runs of identical text, and neither was checkable against the
# source. In their place: `section_heading`, derived by code from the block
# structure (`source.heading_of`) and re-derived in verification like any
# other code-owned field, and `modality_evidence`, the posting's own modal
# phrase quoted and bound through `source.resolve` as `evidence` is. The
# derivation grammars below are UNCHANGED from 19; what changed is the
# statement shape and the checks that read it, which is why the identifier
# moves. Schema 2 records still verify under this module's schema-2 branch,
# byte-identical to 19's behaviour.
#   Second clause, same identifier (parsing contract v3 §3): SETTLEMENT keeps
# two checks and demotes the rest. `agreement.agree` fails a cohort only on
# `negation` — aligned claims disagreeing about polarity — and on
# `numeric_conflict` — aligned claims that both PARSED a number from the same
# span and disagree about its dimension, bounds or unit. F1, the importance
# ratio (gone with the field) and validator/19's five semantic dimensions are
# still computed on every aligned pair and reported under `report["metrics"]`,
# where `serve.profile_of` carries them into the blob as
# `quality.sample_notes`; none of them parks a document. The evidence is the
# same 300-doc sample: 294 of 300 parked documents split on label variance over
# identical text — 172 `compensation_statement`/`employer_context` pairs, 1,399
# of 1,536 value splits being one number under two scope tags, `gps` against
# `global positioning systems (gps)` — and calibrating those five was tried and
# refuted at 10 recovered of 267. `state.GATE_DIMENSION_CODES` shrinks to the
# two gates with it, because rule 3 can only restate a failure that can happen.
#   Third clause, same identifier (2026-09-28 amendment, approved by Sean; 20
# was not yet live): the two gates compare meaning, not labels — `negation`
# reads statement polarity, counts only `negative` as negated (a hedge is not a
# denial) and parks only on qualification, employment-constraint and
# compensation statements; `numeric_conflict` compares each claim's parsed
# numbers and their dimension, so a comparator, unit, currency, period or
# inclusivity difference parks nothing. What either stopped parking on is
# reported as `metrics.splits.polarity` / `numeric_tags` (2026-09-28 analysis:
# 59% of negation splits were a hedge, 347 of 354 numeric conflicts were one
# number under two tags).
# validator/20, amended in place (2026-09-28, T-20260928-H5XS). An in-place
# amendment, not a bump, because 20 had not gone live on main when the
# 2026-09-28 review/quarantine analysis found three contract defects in it;
# once 20 is live, any change like these bumps. What moved:
#   (1) The unit is the word the number carries. `derive_quantity` searched
# the value span, and failing that the cited unit anchor, for ANY unit word it
# knew, skipping the words it did not — so "30" · "days per year" anchored on
# the denominator and derived 360 months, "40 hours each year" 480, and every
# sample misread identically, so no gate could see it (3.5% of sampled
# quantity facts). The unit must now follow its number with nothing but glue
# between — punctuation, never a word: whitespace, a plus sign, brackets, any
# dash `_RANGE` or `source._TYPO_TRANS` reads as a hyphen, and the open-ended
# "plus"/"or more" — and a cited anchor's first word must be it ("(years)"
# and "+ years" anchor). Over the 73,036 served v10 quantity facts this
# unparses 512 derivations and changes no other: 433 rates read as
# durations, 61 "0" · "Travel Percent" (an anchor that does not begin with
# its unit), and 18 durations a word separates from their number ("three
# years (3)", "5 option years", "(5) five years", the ordinal "4th year"),
# which tests pin as intended: the grammar cannot tell "3 full years" from
# "13 paid days per year" without the skip this amendment removed. Hour/day/week
# are known units the record's vocabulary cannot spell (its duration unit is
# the month; converting is a guess), so they derive nothing — and no schema-3
# unit was added for them, because the grammar is shared with the frozen
# schema-2 shape, whose record enum would reject one. The clause above that
# calls the grammars UNCHANGED from 19 no longer holds for quantities: a
# schema-2 record replayed under 20 re-derives its quantities under this rule.
#   (2) Assembly's character scan (validator/17) splits by record shape.
# Under schema 3 (live) it widens from code points below U+0020 to every
# character no reader can see — Unicode categories Cc (bar \n and \t), Cf
# (format: zero-width space and joiners, BOM, soft hyphen, bidi marks), Co, Cn
# and Cs — in every string the MODEL wrote, so a live emit carrying one is
# retried; they reached 22% of validated profiles as junk topic tails. The set
# is `invisible.py`'s table, frozen at Unicode 15.0.0, never the interpreter's
# Unicode database (Python 3.14 assigns 5,812 code points 15.0.0 leaves Cn). A
# bound quote and a code-derived heading are the document's own bytes (2.3% of
# canonical documents carry such characters legitimately) and keep 17's set.
# Under schema 2 — the frozen v10 registration, which replay re-judges and
# cannot retry — assembly keeps 17's set, because ~4,600 served v10 documents
# (16%) hold an only ok record whose topic ends in zero-width junk; the schema
# 2 -> 3 derivation strips it from model-written strings instead
# (`migrate._stripped`). Both shapes also refuse a lone surrogate, which
# crashed `candidate_hash`. Rebuild's and the live fold's storability check
# is a storage constraint only (17's set plus the surrogate,
# `assemble.control_char_errors`): historical tuples fold exactly as before.
#   (3) Schema 3's statement `topic` loses its 80-character cap: constrained
# decoding emitted junk AT the cap (19% of topics at 78+ characters end in
# junk against 0.4% below), which was the whole control-character quarantine
# class. Schema 2's bytes keep the cap.
#   Fourth clause, same identifier (T-Q3S9): a real ladder that runs out (never
# a refused migration) holding a candidate whose ONLY failing findings are
# block bookkeeping (`state.BOOKKEEPING_CODES`), and which extracted at least
# one statement or fact entry and accounted for most of the source's blocks,
# settles `validated` on the one with the fewest gaps (ties to the latest)
# instead of quarantining, with `quality.completeness: accounting_gaps`, the
# gaps in `quality.accounting_gaps`, and never `search_eligible`; the drain
# and the replay recover that candidate from the archived raw response through
# one shared function (`runner._Recovery`). Inside the ladder accounting
# findings stay retry-worthy errors — 271 of the 706 documents quarantined on
# 2026-09-28 failed on nothing else.
VALIDATOR_VERSION = "20"

# 21 (parsing contract v4) judges schema 4 ONLY: it binds the authorization
# presence and track references, re-derives the code-owned `authorization`
# block, resolves track ids, and warns on a `skill` mention linked only to
# non-demand statements. The derivation grammars above are unchanged. Schema 2
# and 3 records keep 20, so bundle v2's tuple (demand-profile/v11, 3, 20) and
# every record it seals stay exactly as they were.
SCHEMA_4_VALIDATOR_VERSION = "21"
_VALIDATOR_BY_SCHEMA = {"4": SCHEMA_4_VALIDATOR_VERSION}


def validator_version_for(schema_version: str) -> str:
    """The validator that seals and judges a record of `schema_version`."""
    return _VALIDATOR_BY_SCHEMA.get(schema_version, VALIDATOR_VERSION)

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
_UNIT_BODY = (
    # letter-bounded, not \b: "5yrs" must still parse (a \b fails between digit
    # and letter), while "more"/"most"/"percentage" never donate a prefix.
    # `inexpressible` (validator/20, 2026-09-28): units the grammar knows and
    # the record's vocabulary cannot spell — knowing them is what stops a
    # "days per year" from being read as years.
    r"(?<![A-Za-z])(?:(?P<years>years?|yrs?|yoe)|(?P<months>months?|mos?)"
    r"|(?P<pct>%|percent)|times?\s+per\s+(?P<per>week|month|day)"
    r"|(?P<inexpressible>hours?|hrs?|days?|weeks?|wks?))(?![A-Za-z])"
)
#: what may stand between a number and its unit: punctuation, never a word —
#: whitespace, a plus sign, brackets, and every dash (hyphen-minus, U+2010
#: hyphen, U+2011 non-breaking hyphen, U+2012 figure dash, en and em dash,
#: U+2212 minus: each one `source._TYPO_TRANS` folds or `_RANGE` reads as a
#: hyphen)
_GLUE = r"[\s+()\[\]\-‐‑‒–—−]"
#: a cited unit anchor: the unit is the span's first WORD (`.match`, never
#: `.search` — a search skips an unknown word to borrow the next known one),
#: so "years", "(years)" and "+ years" anchor and "Travel Percent" does not
_UNIT = re.compile(_GLUE + r"*" + _UNIT_BODY, re.IGNORECASE)
#: a unit inside the value span: the one a NUMBER carries, with only glue
#: between them, or the open-ended "plus"/"or more" (comparison words, never
#: units; the comparison itself is read from its own cited span) — so
#: "8+ years", "six (6) years", "[7+] years", "4-Year", "4—year" and
#: "5 or more years" parse, while the "year" of "30 days per year" or
#: "4th year" belongs to no number
_CARRIED = re.compile(r"\d(?:" + _GLUE + r"|or\s+more|plus)*" + _UNIT_BODY, re.IGNORECASE)
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


def derive_quantity(value_text: str, comparison_text: str | None,
                    unit_text: str | None = None) -> dict[str, Any] | None:
    """The cited spans as one quantity, or None when the grammar cannot read them.

    validator/19: `unit_text` is the unit the emit cited as its OWN span, the
    way the compensation branch has always taken currency and period anchors.
    It is consulted only when the value span carries no unit of its own — a
    value that says "6 months" means months whatever the anchor says — and an
    anchor the grammar does not know forces None rather than letting the number
    fall through to a dimensionless count. A value span carrying some other
    unit word is a contradiction, not a tie to break, and stays unparsed.

    validator/20 (amended 2026-09-28) makes that last promise literal: the
    unit is the word the number CARRIES — the one following it in the value
    span, or the first word of the cited anchor. A word the grammar does not
    know is never skipped to borrow a later one it does ("13 paid days per
    year" is not 13 years), and hour/day/week, known but outside the record's
    unit vocabulary, derive None rather than a month conversion.
    """
    op = _comparison(comparison_text)
    if op == "?":
        return None
    unit_m = _CARRIED.search(value_text)
    if unit_m is None and (unit_text or "").strip() and not _HAS_ALPHA.search(value_text):
        unit_m = _UNIT.match((unit_text or "").strip())
        if unit_m is None:
            return None  # a cited unit outside the grammar: a gap, never a count
    if unit_m and unit_m.group("inexpressible"):
        return None  # a known unit the record cannot spell: unparsed, never converted
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
