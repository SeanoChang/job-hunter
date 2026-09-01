import pytest

from jobhunter.l2.transforms import (
    TRANSFORMS,
    VALIDATOR_VERSION,
    parse_compensation,
    parse_deadline,
    parse_experience_months,
)


def test_registry_shape() -> None:
    assert VALIDATOR_VERSION == "2"
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


def test_validator_version_bumped_for_the_grammar_change() -> None:
    assert VALIDATOR_VERSION == "2"
