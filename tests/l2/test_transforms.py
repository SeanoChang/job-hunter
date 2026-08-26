import pytest

from jobhunter.l2.transforms import (
    TRANSFORMS,
    VALIDATOR_VERSION,
    parse_compensation,
    parse_deadline,
    parse_experience_months,
)


def test_registry_shape() -> None:
    assert VALIDATOR_VERSION == "1"
    assert set(TRANSFORMS["1"]) == {"experience_months", "compensation", "deadline"}


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
