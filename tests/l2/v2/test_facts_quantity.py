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
        # negated ceiling: "no more than" is an explicit lte phrase (plan Task 5
        # grammar), never the "more than" gt floor it contains as a substring
        ("8 years", "no more than", q("duration", "lte", None, 96, None, True, "month")),
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
        # comparison phrases outside the enumerated grammar must never be
        # guessed from a substring of a phrase that IS in the grammar
        ("8 years", "not more than"),               # not the same phrase as "no more than"
        ("8 years", "greater than or equal to"),    # contains "greater than" but isn't it
        ("8 years", "less than or equal to"),       # contains "less than" but isn't it
        ("8 years", "over the course of"),          # contains "over" but isn't a comparison
        # a unit word outside the known grammar (years/months/%/times-per-*)
        # is a grammar gap, not a bare integer: never silently becomes "count"
        ("10 hours per week", None),
        ("3 days", None),
        ("2-3 times a week", None),   # "times a week", not the grammar's "times per week"
    ],
)
def test_unparseable_is_none(value, comparison) -> None:
    assert derive_quantity(value, comparison) is None


def test_unit_words_require_word_boundaries() -> None:
    # Release-review finding: an unbounded `mos?` matched the "mo" prefix of
    # ordinary words, so leftmost search read "more than 5 years" as 5 months.
    assert derive_quantity("more than 5 years", None) == {
        "dimension": "duration",
        "comparison": "unstated",
        "min_value": 60,
        "max_value": 60,
        "inclusive_min": None,
        "inclusive_max": None,
        "unit": "month",
    }
    assert derive_quantity("most weekends, 5 years", None)["min_value"] == 60
    # no real unit token at all: alphabetic residue is a gap, never a guess
    assert derive_quantity("more than 5", None) is None
    # the genuine abbreviation still parses
    assert derive_quantity("6 mos", None)["min_value"] == 6
