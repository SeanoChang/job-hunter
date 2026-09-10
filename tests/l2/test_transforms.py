import pytest

from jobhunter.l2.transforms import (
    TRANSFORMS,
    VALIDATOR_VERSION,
    parse_compensation,
    parse_deadline,
    parse_experience_months,
)


def test_registry_shape() -> None:
    assert VALIDATOR_VERSION == "12"
    assert set(TRANSFORMS[VALIDATOR_VERSION]) == {
        "experience_months", "compensation", "deadline",
    }


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0-2 YOE", {"min": 0, "max": 24}),
        ("3 – 5 years", {"min": 36, "max": 60}),
        ("5+ years", {"min": 60, "max": None}),
        ("2 years", {"min": 24, "max": 24}),
        ("12 yrs", {"min": 144, "max": 144}),
        ("between 3 and 5 years", {"min": 36, "max": 60}),
        ("2 years in backend plus 5 years total", None),  # two tokens, no range syntax
        ("many years", None),
        ("", None),
    ],
)
def test_experience(text: str, expected: dict[str, object] | None) -> None:
    assert parse_experience_months(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "$130,000 - $150,000",
            {"min": 130000, "max": 150000, "currency": None, "period": None},
        ),
        ("$130K–$150K", {"min": 130000, "max": 150000, "currency": None, "period": None}),
        ("$45 - $55 per hour", {"min": 45, "max": 55, "currency": None, "period": "hour"}),
        (
            "$300,000—$405,000 USD per year",
            {"min": 300000, "max": 405000, "currency": "USD", "period": "year"},
        ),
        (
            "$90,000 - $110,000 CAD",
            {"min": 90000, "max": 110000, "currency": "CAD", "period": None},
        ),
        ("$130 - $150K", {"min": 130000, "max": 150000, "currency": None, "period": None}),
        ("$150K - $130K", None),  # inverted range: ambiguous
        ("competitive salary", None),
    ],
)
def test_compensation(text: str, expected: dict[str, object] | None) -> None:
    assert parse_compensation(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("July 17, 2026", {"date": "2026-07-17"}),
        ("until March 3, 2027", {"date": "2027-03-03"}),
        ("Applications close Jan 5, 2027", {"date": "2027-01-05"}),
        ("posted July 1, 2026; apply by July 17, 2026", None),  # two dates: ambiguous
        ("February 30, 2026", None),  # impossible calendar date
        ("soon", None),
    ],
)
def test_deadline(text: str, expected: dict[str, object] | None) -> None:
    assert parse_deadline(text) == expected


def test_descending_range_is_none() -> None:
    assert parse_experience_months("5-3 years") is None


def test_lowercase_currency_normalized() -> None:
    result = parse_compensation("$90,000 - $110,000 usd")
    assert result is not None and result["currency"] == "USD"


# --- multi-currency (validator/2) ------------------------------------------
# Found by the first 5-document run: a London posting quoting
# "£375,000—£640,000 GBP" was quarantined three times because the money
# grammar was dollar-only. The model had anchored correctly; our parser
# refused it. Currency is RETAINED as written, never converted.

@pytest.mark.parametrize(
    "text,expected",
    [
        # explicit code always wins
        ("£375,000—£640,000 GBP", {"min": 375000, "max": 640000,
                                   "currency": "GBP", "period": None}),
        ("€90,000 - €110,000 EUR", {"min": 90000, "max": 110000,
                                    "currency": "EUR", "period": None}),
        ("¥8,000,000 - ¥12,000,000 JPY", {"min": 8000000, "max": 12000000,
                                          "currency": "JPY", "period": None}),
        # unambiguous symbol implies the currency: that is stated, not guessed
        ("£375,000—£640,000", {"min": 375000, "max": 640000,
                               "currency": "GBP", "period": None}),
        ("€90,000 - €110,000", {"min": 90000, "max": 110000,
                                "currency": "EUR", "period": None}),
        # ambiguous symbols stay null unless a code is written (null-over-guess):
        # $ is USD/CAD/AUD/SGD/HKD/NZD, ¥ is JPY or CNY
        ("$130,000 - $150,000", {"min": 130000, "max": 150000,
                                 "currency": None, "period": None}),
        ("¥8,000,000 - ¥12,000,000", {"min": 8000000, "max": 12000000,
                                      "currency": None, "period": None}),
        # mixed symbols are not a range
        ("£100,000 - €120,000", None),
        ("£45 - £55 per hour", {"min": 45, "max": 55,
                                "currency": "GBP", "period": "hour"}),
    ],
)
def test_compensation_currencies(text: str, expected: dict[str, object] | None) -> None:
    assert parse_compensation(text) == expected


# --- code-suffixed amounts (validator/3) -----------------------------------
# Found by the NVIDIA canary: Workday postings write "136,000 USD - 218,500
# USD for Level 3" — currency code after the amount, no symbol at all. The
# symbol-first grammar returned None on 86 correctly-anchored quotes across
# one board's quarantines (SEA-186).

@pytest.mark.parametrize(
    "text,expected",
    [
        ("136,000 USD - 218,500 USD for Level 3",
         {"min": 136000, "max": 218500, "currency": "USD", "period": None}),
        # code on the trailing amount only still names the range's currency
        ("136,000 - 218,500 USD",
         {"min": 136000, "max": 218500, "currency": "USD", "period": None}),
        ("8,000,000 JPY - 12,000,000 JPY",
         {"min": 8000000, "max": 12000000, "currency": "JPY", "period": None}),
        ("130K - 150K USD per year",
         {"min": 130000, "max": 150000, "currency": "USD", "period": "year"}),
        # mismatched codes are not a range
        ("100,000 USD - 120,000 EUR", None),
        # no symbol and no code anywhere: not evidently money (null-over-guess)
        ("136,000 - 218,500", None),
    ],
)
def test_compensation_code_suffixed(text: str, expected: dict[str, object] | None) -> None:
    assert parse_compensation(text) == expected


def test_validator_version_bumped_for_the_grammar_change() -> None:
    assert VALIDATOR_VERSION == "12"


# --- Workday phrasing (validator/4) -----------------------------------------
# Step-1 of the supervised drain (2026-09-06) tallied 693 compensation, ~140
# experience and 29 deadline anchors the grammars missed — all real Workday
# phrasings, all correctly anchored by the model and rejected by us.

@pytest.mark.parametrize(
    "text,expected",
    [
        # symbol AND trailing code on each side
        ("$123,900 USD - $222,000 USD",
         {"min": 123900, "max": 222000, "currency": "USD", "period": None}),
        ("€78,600 EUR - €118,000 EUR",
         {"min": 78600, "max": 118000, "currency": "EUR", "period": None}),
        # decimal cents, spaced symbol, "to" separator, trailing code + period
        ("$169,100.00 to $ 270,800.00 USD per year",
         {"min": 169100, "max": 270800, "currency": "USD", "period": "year"}),
        ("$18.50 - $24.25 per hour",
         {"min": 18, "max": 24, "currency": None, "period": "hour"}),
    ],
)
def test_compensation_validator4(text: str, expected: dict[str, object] | None) -> None:
    assert parse_compensation(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("5 or more years of relevant experience", {"min": 60, "max": None}),
        ("2 or more years of work experience", {"min": 24, "max": None}),
    ],
)
def test_experience_or_more(text: str, expected: dict[str, object] | None) -> None:
    assert parse_experience_months(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("09/11/26", {"date": "2026-09-11"}),
        ("Apply by 12/01/2026", {"date": "2026-12-01"}),
        ("02/30/26", None),  # impossible calendar date
        ("posted 09/01/26; apply by 09/14/26", None),  # two dates: ambiguous
    ],
)
def test_deadline_numeric(text: str, expected: dict[str, object] | None) -> None:
    assert parse_deadline(text) == expected


# --- validator/6: step-4 leftovers (2026-09-06) ------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        # double-hyphen separator (~64 hits in step 4)
        ("$139,000 -- $257,000",
         {"min": 139000, "max": 257000, "currency": None, "period": None}),
        # symbol on the first amount only (step-2 tail)
        ("$179,500 - 269,300",
         {"min": 179500, "max": 269300, "currency": None, "period": None}),
        # European decimal-thousands with a code (step-2 tail)
        ("EUR 71.000 to EUR 95.000 annually",
         {"min": 71000, "max": 95000, "currency": "EUR", "period": "year"}),
    ],
)
def test_compensation_validator6(text: str, expected: dict[str, object] | None) -> None:
    assert parse_compensation(text) == expected


def test_experience_at_least_is_a_floor() -> None:
    """v8: 'At least 5 years' read as exact {60, 60} under v7 — it is a floor,
    same as '5+ years' (2026-09-06 omission-sample review)."""
    from jobhunter.l2.transforms import parse_experience_months

    assert parse_experience_months("At least 5 years of relevant experience") == {
        "min": 60, "max": None,
    }
    assert parse_experience_months("at least 3 yrs") == {"min": 36, "max": None}
    # untouched neighbours
    assert parse_experience_months("5+ years") == {"min": 60, "max": None}
    assert parse_experience_months("5 years of Python") == {"min": 60, "max": 60}


# --- validator/9: "minimum (of) N", "more than N", "over N" are floors -------
# audit 2026-09-06 defect 1 (C01): "a minimum of 8 years of experience" read as
# exact {96, 96} under v8 — 72 validated records carried a false exact interval.

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a minimum of 8 years of experience", {"min": 96, "max": None}),
        ("Minimum 5 years in consulting", {"min": 60, "max": None}),
        ("minimum of 10 years", {"min": 120, "max": None}),
        ("more than 8 years of experience", {"min": 96, "max": None}),
        ("over 12 years", {"min": 144, "max": None}),
        # regressions: exact stays exact, ranges stay ranges
        ("5 years of experience", {"min": 60, "max": 60}),
        ("5-7 years", {"min": 60, "max": 84}),
        ("at least 8 years of experience", {"min": 96, "max": None}),
        ("8+ years", {"min": 96, "max": None}),
    ],
)
def test_experience_floor_wordings_validator9(
    text: str, expected: dict[str, object] | None
) -> None:
    assert parse_experience_months(text) == expected


def test_validator_version_is_9() -> None:
    # the grammar changed; stored validator/8 rows keep their meaning
    assert VALIDATOR_VERSION == "12"
    assert VALIDATOR_VERSION in TRANSFORMS


def test_negated_more_than_is_not_a_floor() -> None:
    # "no/not more than N years" states a ceiling; the validator/9 floor branch
    # must not fire on the embedded "more than". v1 has no ceiling shape, so
    # the exact fallback (validator/8 parity) is the conservative reading.
    assert parse_experience_months("no more than 5 years") == {"min": 60, "max": 60}
    assert parse_experience_months("not more than 5 years") == {"min": 60, "max": 60}
    assert parse_experience_months("more than 5 years") == {"min": 60, "max": None}
    # only the literal words "no"/"not" suppress the floor; a word that merely
    # ends in them ("casino", "Reno") does not
    assert parse_experience_months("casino over 5 years") == {"min": 60, "max": None}


@pytest.mark.parametrize(
    "text,expected",
    [
        # validator/11: a single stated amount with a currency signal is a
        # point value (quarantine audit 2026-09-09: 344 docs cited one figure)
        ("$332,200.00", {"min": 332200, "max": 332200, "currency": None, "period": None}),
        ("$58.22 / hr", {"min": 58, "max": 58, "currency": None, "period": "hour"}),
        ("$163,800 USD per year",
         {"min": 163800, "max": 163800, "currency": "USD", "period": "year"}),
        ("163,800 USD", {"min": 163800, "max": 163800, "currency": "USD", "period": None}),
        ("EUR 71.000 annually", {"min": 71000, "max": 71000, "currency": "EUR", "period": "year"}),
        ("£95,000", {"min": 95000, "max": 95000, "currency": "GBP", "period": None}),
        ("$130K", {"min": 130000, "max": 130000, "currency": None, "period": None}),
        # one-sided wordings become one-sided intervals, never invented bounds
        ("up to $180,000", {"min": None, "max": 180000, "currency": None, "period": None}),
        ("starting at $140,000 per year",
         {"min": 140000, "max": None, "currency": None, "period": "year"}),
        # no currency signal: a bare number is not compensation evidence
        ("332,200", None),
        ("40 hours", None),
        # two amounts without range syntax stay ambiguous
        ("$100,000 in equity and $10,000 bonus", None),
    ],
)
def test_compensation_single_amount(text: str, expected: dict[str, object] | None) -> None:
    assert parse_compensation(text) == expected


# --- validator/12: international compensation forms -------------------------
# 91 quarantined compensation anchors are international forms the range
# grammar refused: multi-character currency signs (CA$, zł, Kč, RM), "kr"
# with DKK/SEK/NOK ambiguity resolved only through an explicit code,
# space-thousands, a decimal-comma tail, the Unicode minus family, and a
# period fragment inside each bound (2026-09-10 quarantine audit).

@pytest.mark.parametrize(
    "text,expected",
    [
        ("CA$110,200 - CA$160,200 CAD gross",
         {"min": 110200, "max": 160200, "currency": "CAD", "period": None}),
        ("kr539,400 – kr809,200 DKK gross",
         {"min": 539400, "max": 809200, "currency": "DKK", "period": None}),
        ("kr739,000 SEK - kr1,109,000 SEK",
         {"min": 739000, "max": 1109000, "currency": "SEK", "period": None}),
        ("zł188.400 PLN - zł282.600 PLN",
         {"min": 188400, "max": 282600, "currency": "PLN", "period": None}),
        ("Kč2,206,000 CZK - Kč3,308,000 CZK",
         {"min": 2206000, "max": 3308000, "currency": "CZK", "period": None}),
        ("639,400 - 799,300 DKK",
         {"min": 639400, "max": 799300, "currency": "DKK", "period": None}),
        ("DKK 53283 - DKK 66608",
         {"min": 53283, "max": 66608, "currency": "DKK", "period": None}),
        ("210 300.00 USD - 273 400.00 USD",
         {"min": 210300, "max": 273400, "currency": "USD", "period": None}),
        ("Approximately 65,000−87,500 OTE annually",
         {"min": 65000, "max": 87500, "currency": None, "period": "year"}),
        ("$40/hour to $65/hour", {"min": 40, "max": 65, "currency": None, "period": "hour"}),
        ("110,000 - 200,000/year SGD",
         {"min": 110000, "max": 200000, "currency": "SGD", "period": "year"}),
        ("$130.600,00 to $ 209.300,00 USD per year",
         {"min": 130600, "max": 209300, "currency": "USD", "period": "year"}),
        ("RM2,000", {"min": 2000, "max": 2000, "currency": "MYR", "period": None}),
        # right refusals stay refusals
        ("competitive salaries", None),
        ("85% paid through base salary and 15% variable compensation", None),
        ("$M+", None),
        ("5-10% of the time", None),
        # regression: nothing about the US forms moves
        ("$163,800 - $245,800 USD per year",
         {"min": 163800, "max": 245800, "currency": "USD", "period": "year"}),
        ("$332,200.00", {"min": 332200, "max": 332200, "currency": None, "period": None}),
    ],
)
def test_compensation_international(text: str, expected: dict[str, object] | None) -> None:
    assert parse_compensation(text) == expected


# --- QMRF fix: the bare-range gate must not eat non-money ranges ------------
# Adversarial review (2026-09-10, ticket QMRF) found the 36bf9b7 comma-grouped
# gate only refused the eight literal strings in its own test because those
# happened to use ungrouped digits ("100 - 200 people"). Comma-grouping the
# same non-money sentence ("10,000 - 20,000 stock options") re-opened the
# hole: any bare numeric range plus an "annually"/"/hour"-family token
# ANYWHERE in the text still donated a false compensation interval — worst
# case equity/RSU/share-grant prose living in the compensation section
# itself. The gate now also requires the range to be immediately followed by
# "OTE" (on-target-earnings, an unambiguous compensation term) — the exact
# shape of the design's one bare-range row, "65,000−87,500 OTE annually".
# Comma-grouping alone is no longer sufficient; a currency sign or code is
# still required for every other bare numeric range, comma-grouped or not.
@pytest.mark.parametrize(
    "text",
    [
        "PTO: 20-30 days annually",
        "0-2 YOE preferred, promotions annually",
        "We are a team of 100 - 200 people; all-hands per year",
        "Cohorts run 6 - 8 weeks, twice per year",
        "Discount of 10-15 percent per year",
        "Serving 1 000 - 2 000 customers per year",
        "Rated 4.5 - 4.9 stars annually",
        "You will receive 15 to 25 days of paid time off per year",
        # comma-grouped variants of the same non-money classes (QMRF finding):
        # the pre-fix gate accepted every one of these as a compensation range
        "Equity: 10,000 - 20,000 stock options, refreshed annually",
        "Annual bonus target and 15,000 - 25,000 RSUs granted per year",
        "401(k) with company match; 10,000 - 12,000 shares granted annually",
        "A team of 1,000 - 2,000 people; all-hands per year",
        "We serve 10,000 - 50,000 customers annually",
        "Our platform handles 50,000 - 60,000 transactions per hour",
    ],
)
def test_compensation_bare_range_gate_rejects_non_money(text: str) -> None:
    assert parse_compensation(text) is None


def test_compensation_bare_range_still_accepts_the_design_row() -> None:
    # regression: the one legitimate bare-range shape must keep working
    assert parse_compensation("Approximately 65,000−87,500 OTE annually") == {
        "min": 65000, "max": 87500, "currency": None, "period": "year",
    }


# --- QMRF fix: the two-sign equality check must be case-insensitive ---------
# Adversarial review (2026-09-10, ticket QMRF) found `sym_lo != sym_hi` was a
# byte-exact comparison even though the signs are matched case-insensitively
# (`_SIGN` under `re.IGNORECASE`) — a case difference between the two bounds
# ("Kč" vs "KČ", "kr" vs "Kr", "CA$" vs "ca$") turned a legitimate range into
# a refusal. Conservative (null, not wrong data) but it silently caps
# recovery on exactly the currencies this ticket exists to recover, where
# mixed casing across a range is common in real anchors.
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Kč2,206,000 CZK - KČ3,308,000 CZK",
         {"min": 2206000, "max": 3308000, "currency": "CZK", "period": None}),
        ("kr539,400 – Kr809,200 DKK",
         {"min": 539400, "max": 809200, "currency": "DKK", "period": None}),
        ("CA$110,200 - ca$160,200 CAD",
         {"min": 110200, "max": 160200, "currency": "CAD", "period": None}),
        # regression: genuinely mismatched signs still refuse
        ("£100,000 - €120,000", None),
    ],
)
def test_compensation_sign_case_insensitive(
    text: str, expected: dict[str, object] | None
) -> None:
    assert parse_compensation(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # a grade/zone/year token before the amount must never fuse into it
        # (adversarial review 2026-09-10: "Level 3 200,000" read as 3,200,000)
        ("Level 3 200,000 USD - 250,000 USD",
         {"min": 200000, "max": 250000, "currency": "USD", "period": None}),
        ("P4 180,000 USD - 220,000 USD",
         {"min": 180000, "max": 220000, "currency": "USD", "period": None}),
        ("Zone 2 136,000 USD - 218,500 USD",
         {"min": 136000, "max": 218500, "currency": "USD", "period": None}),
        ("In 2026 130,000 USD - 150,000 USD",
         {"min": 130000, "max": 150000, "currency": "USD", "period": None}),
        ("Level 1 100,000 USD - 1,200,000 USD",
         {"min": 100000, "max": 1200000, "currency": "USD", "period": None}),
        ("24 100,000 USD", {"min": 100000, "max": 100000, "currency": "USD", "period": None}),
        # genuine space-thousands still parse (homogeneous separators only)
        ("210 300.00 USD - 273 400.00 USD",
         {"min": 210300, "max": 273400, "currency": "USD", "period": None}),
    ],
)
def test_amount_never_fuses_a_preceding_token(
    text: str, expected: dict[str, object] | None
) -> None:
    assert parse_compensation(text) == expected


def test_space_thousands_never_fuse_a_preceding_token() -> None:
    # second adversarial re-review: "Level 1 100 000" is itself a legal space
    # group, so bare space amounts require a non-word left edge AND a decimal
    # tail; fused ranges must refuse rather than derive a false interval
    assert parse_compensation("Level 1 100 000 USD - 1 200 000 USD") is None
    assert parse_compensation("Step 2 210 300.00 USD - 273 400.00 USD") is None
    # genuine bare space-grouped amounts (decimal tail, non-word left edge) hold
    assert parse_compensation("210 300.00 USD - 273 400.00 USD") == {
        "min": 210300, "max": 273400, "currency": "USD", "period": None,
    }
    assert parse_compensation("Salary: 210 300.00 USD - 273 400.00 USD") == {
        "min": 210300, "max": 273400, "currency": "USD", "period": None,
    }
    # sign-led space groups stay unguarded — the sign disambiguates
    assert parse_compensation("kr 850 000 SEK") == {
        "min": 850000, "max": 850000, "currency": "SEK", "period": None,
    }
    # a fused single must never yield a plausible large value (v11 parity:
    # the residual \d+ branch may still find a filterable 0, never 4.18M)
    r = parse_compensation("Level 4 180 000 SEK")
    assert r is None or r["min"] == 0
