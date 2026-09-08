import pytest

from jobhunter.l2.transforms import (
    TRANSFORMS,
    VALIDATOR_VERSION,
    parse_compensation,
    parse_deadline,
    parse_experience_months,
)


def test_registry_shape() -> None:
    assert VALIDATOR_VERSION == "9"
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
    assert VALIDATOR_VERSION == "9"


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
    assert VALIDATOR_VERSION == "9"
    assert VALIDATOR_VERSION in TRANSFORMS
