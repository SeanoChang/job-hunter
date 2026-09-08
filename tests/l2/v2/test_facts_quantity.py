import pytest

from jobhunter.l2.v2.facts import VALIDATOR_VERSION, derive_quantity


def q(dimension, comparison, lo, hi, inc_lo, inc_hi, unit):
    return {"dimension": dimension, "comparison": comparison, "min_value": lo,
            "max_value": hi, "inclusive_min": inc_lo, "inclusive_max": inc_hi,
            "unit": unit}


def test_validator_version_is_10() -> None:
    assert VALIDATOR_VERSION == "10"


@pytest.mark.parametrize(
    ("value", "comparison", "expected"),
    [
        # C01: inclusive floor, no invented ceiling
        ("8 years", "a minimum of", q("duration", "gte", 96, None, True, None, "month")),
        ("8 years", "at least", q("duration", "gte", 96, None, True, None, "month")),
        # spec §3: strict floor, never converted to nine years
        ("8 years", "more than", q("duration", "gt", 96, None, False, None, "month")),
        ("8 years", "over", q("duration", "gt", 96, None, False, None, "month")),
        # bare quantity: stated, not an eligibility band
        ("5 years", None, q("duration", "unstated", 60, 60, None, None, "month")),
        ("5-7 years", None, q("duration", "range", 60, 84, True, True, "month")),
        ("8+ years", None, q("duration", "gte", 96, None, True, None, "month")),
        # C06-adjacent shapes
        ("20%", "up to", q("percentage", "lte", None, 20, None, True, "percent")),
        ("2-3 times per week", None, q("frequency", "range", 2, 3, True, True, "per_week")),
        ("6 months", None, q("duration", "unstated", 6, 6, None, None, "month")),
        ("3", "at least", q("count", "gte", 3, None, True, None, None)),
    ],
)
def test_derivations(value, comparison, expected) -> None:
    assert derive_quantity(value, comparison) == expected


@pytest.mark.parametrize(
    ("value", "comparison"),
    [
        ("several years", None),          # no number
        ("8 years and 3 years", None),    # two tokens, no range syntax
        ("7-5 years", None),              # descending range
        ("acht Jahre", "mindestens"),     # unknown language: unparsed, not guessed
    ],
)
def test_unparseable_is_none(value, comparison) -> None:
    assert derive_quantity(value, comparison) is None
