import pytest

from jobhunter.l2.v2.facts import derive_quantity

# the validator identifier and its history are pinned in test_facts.py


def q(dimension, comparison, lo, hi, inc_lo, inc_hi, unit):
    return {"dimension": dimension, "comparison": comparison, "min_value": lo,
            "max_value": hi, "inclusive_min": inc_lo, "inclusive_max": inc_hi,
            "unit": unit}


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
    # letter-bounded, not \b: number-glued units keep parsing
    assert derive_quantity("5yrs", None)["min_value"] == 60
    assert derive_quantity("20 percentage points", None) is None
    assert derive_quantity("3 times per weekend", None) is None


# --- validator/20, amended in place (2026-09-28): the unit is the word the
# number carries. The unit search used to skip any word it did not know and
# anchor on the next one it did, so a rate over a year — "30 days per year" —
# read its DENOMINATOR as the number's unit and derived 360 months. Every
# sample misread identically, so no agreement gate could see it (3.5% of
# sampled quantity facts, 2026-09-28 review analysis).


@pytest.mark.parametrize(
    ("value", "comparison", "unit"),
    [
        # archived attempt 21T201613Z-23d6b3dca786-s1a2: value "30", unit
        # "days per year" -> 360 months
        ("30", "up to", "days per year"),
        ("30 days per year", None, "days per year"),
        # "4" + "up to" + "weeks per year" -> lte 48 months
        ("4", "up to", "weeks per year"),
        ("4 weeks per year", "up to", "weeks per year"),
        # "40 hours each year" -> 480 months
        ("40 hours each year", "up to", "hours"),
        ("40", None, "hours each year"),
        # "13 paid days per year" -> 156 months
        ("13 paid days per year", None, "days per year"),
        ("13", None, "paid days per year"),
    ],
)
def test_a_rate_over_a_year_never_derives_months_of_years(
    value: str, comparison: str | None, unit: str
) -> None:
    """The record vocabulary has no unit for days/weeks/hours or for a
    per-year rate, so the only honest reading is none: null over guess."""
    assert derive_quantity(value, comparison, unit) is None


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        ("3 days", None), ("1 day", None), ("2 weeks", None), ("1 week", None),
        ("10 hours", None), ("1 hour", None), ("8 hrs", None), ("2 wks", None),
        ("3", "days"), ("2", "weeks"), ("40", "hours"), ("8", "hrs"),
    ],
)
def test_hour_day_week_are_known_units_the_record_cannot_express(
    value: str, unit: str | None
) -> None:
    """Known, and deliberately unparsed: the record's duration unit is the
    month, and converting days or hours into months is a guess."""
    assert derive_quantity(value, None, unit) is None


@pytest.mark.parametrize(
    ("value", "comparison", "unit"),
    [
        # a unit word the grammar does not know is never skipped for one it does
        ("12", "up to", "sessions each year"),       # was 144 months
        ("100+", None, "hires per year"),            # was >= 1200 months
        ("2-3", None, "times per year"),             # was 24-36 months
        ("2", None, "times/month"),                  # was 2 months
        ("10", None, "days per month"),              # was 10 months
        ("1–2 US trips per year", None, "trips per year"),  # was 12-24 months
        ("2x/month", None, "/month"),                # was 2 months
        ("0", None, "Travel Percent"),               # an anchor that is not a unit
        # the unit belongs to ANOTHER number in the anchor
        ("3", "maximum of", "roles within 12 months"),
        # an ordinal is not a duration
        ("4th year", None, None),
    ],
)
def test_a_unit_the_number_does_not_carry_is_never_borrowed(
    value: str, comparison: str | None, unit: str | None
) -> None:
    assert derive_quantity(value, comparison, unit) is None


@pytest.mark.parametrize(
    ("value", "comparison", "unit", "expected"),
    [
        # brackets, a plus sign or a hyphen between a number and its unit are
        # glue, not a different unit (all live corpus shapes)
        ("six (6) years", "At least", "years",
         q("duration", "gte", 72, None, True, None, "month")),
        ("one (1) year", None, "year", q("duration", "unstated", 12, 12, None, None, "month")),
        ("[7+] years", None, "years", q("duration", "gte", 84, None, True, None, "month")),
        ("4‐Year", None, "Year", q("duration", "unstated", 48, 48, None, None, "month")),
        ("5-year", None, None, q("duration", "unstated", 60, 60, None, None, "month")),
        # open-ended comparison words are glue too — never a unit
        ("5 or more years", "minimum", "years",
         q("duration", "gte", 60, None, True, None, "month")),
        ("7 plus years", "Minimum", "years", q("duration", "gte", 84, None, True, None, "month")),
        ("7 to 10 or more years of experience", None, "years",
         q("duration", "range", 84, 120, True, True, "month")),
        # the anchor path: the cited unit span starts with the unit
        ("12+", None, "years of experience", q("duration", "gte", 144, None, True, None, "month")),
        ("12+", None, " years", q("duration", "gte", 144, None, True, None, "month")),
        ("3", None, "months", q("duration", "unstated", 3, 3, None, None, "month")),
        ("20", "up to", "%", q("percentage", "lte", None, 20, None, True, "percent")),
        ("2", None, "times per week", q("frequency", "unstated", 2, 2, None, None, "per_week")),
    ],
)
def test_a_unit_the_number_carries_still_parses(
    value: str, comparison: str | None, unit: str | None, expected: dict[str, object]
) -> None:
    assert derive_quantity(value, comparison, unit) == expected


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        # every dash `source._TYPO_TRANS` folds to a hyphen — and `_RANGE`
        # reads as one — is a hyphen between a number and its unit too
        ("4—year", None, q("duration", "unstated", 48, 48, None, None, "month")),
        ("12—month", None, q("duration", "unstated", 12, 12, None, None, "month")),
        ("4 — year", None, q("duration", "unstated", 48, 48, None, None, "month")),
        ("4‒year", None, q("duration", "unstated", 48, 48, None, None, "month")),
        ("4−year", None, q("duration", "unstated", 48, 48, None, None, "month")),
        ("4–year", None, q("duration", "unstated", 48, 48, None, None, "month")),
        # the non-breaking hyphen, beside U+2010 which already was glue
        ("4‑year", None, q("duration", "unstated", 48, 48, None, None, "month")),
        ("4‑Year", "Year", q("duration", "unstated", 48, 48, None, None, "month")),
        # an opening bracket is glue as its closing one is
        ("5 (years)", None, q("duration", "unstated", 60, 60, None, None, "month")),
        ("5 [years]", None, q("duration", "unstated", 60, 60, None, None, "month")),
        # a cited anchor opening on glue still begins with its unit: the unit
        # is the anchor's first WORD
        ("5", "(years)", q("duration", "unstated", 60, 60, None, None, "month")),
        ("5", "+ years", q("duration", "unstated", 60, 60, None, None, "month")),
        ("5", "[years of experience]", q("duration", "unstated", 60, 60, None, None, "month")),
        ("5", "-year", q("duration", "unstated", 60, 60, None, None, "month")),
    ],
)
def test_glue_is_punctuation_and_the_same_everywhere(
    value: str, unit: str | None, expected: dict[str, object]
) -> None:
    assert derive_quantity(value, None, unit) == expected


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        # served v10 shapes (2026-09-28 corpus comparison against validator 19)
        ("three years (3)", "years"),
        ("one year (1)", "year"),
        ("(5) five years", "years"),
        ("5 option years", "option years"),
        ("5 progressively responsible years", "years"),
        ("8 of more years", "years"),
        ("5 more months", "months"),
        ("6", "6-month period"),  # an anchor opening on a number, not its unit
        # the same rule on shapes the review probed
        ("5 (five) years", "years"),
        ("6 consecutive months", "months"),
        ("3 full years", "years"),
        ("2 additional years", "years"),
        ("5", "full years"),
        ("5", "**years**"),  # emphasis markup is not glue
        ("5 ~ years", None),  # nor is an approximation mark
        ("5", "~ years"),
    ],
)
def test_a_word_between_a_number_and_its_unit_leaves_it_unparsed(
    value: str, unit: str | None
) -> None:
    """Intended, and pinned so a later grammar change moves it only on purpose.

    The grammar cannot tell an adjective ("3 full years") from a unit it does
    not know ("13 paid days per year", "12 sessions each year"), and skipping
    the word is exactly the misread validator 20 removed — so a word between a
    number and its unit is null over guess, as is punctuation outside the glue
    set. facts.py's validator/20 history counts what this costs in served data.
    """
    assert derive_quantity(value, None, unit) is None
