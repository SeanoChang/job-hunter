"""validator/24 (schema 4): a quoted plus sign is the comparison it spells.

On the 2026-10-10 v15 corpus, 52,133 experience facts derived
`present_unparsed`, and 72% of them had one shape: the model quoted the "+" of
"3+ years" (or the whole "3+") as the fact's comparison. The comparison grammar
knew only phrases ("at least", "minimum"), so the span read as an unknown
operator and the fact stayed unparsed. The same miss kept "Minimum" beside a
"3+" and "or more" beside "10 or more years" unparsed. Schema 2 and 3 records
keep validator/20's grammar byte for byte.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhunter.l2.v2 import facts
from jobhunter.l2.v2.facts import derive_quantity
from jobhunter.l2.v2.verify import verify
from tests.l2.v2.conftest import assemble4, quote, s4_emit, s4_statement

GTE_36 = {"dimension": "duration", "comparison": "gte", "min_value": 36, "max_value": None,
          "inclusive_min": True, "inclusive_max": None, "unit": "month"}


def test_schema_4_is_judged_by_validator_24() -> None:
    assert facts.SCHEMA_4_VALIDATOR_VERSION == "24"
    assert facts.validator_version_for("4") == "24"
    assert facts.validator_version_for("3") == "20"


@pytest.mark.parametrize(("value", "comparison", "unit"), [
    ("3+ years", "+", "years"),          # 20,003 corpus facts
    ("3+ years", "3+", "years"),         # 14,644
    ("3+", "+", "years"),                # 2,987
    ("3+ years", "+", None),
    ("3+ years", "Minimum", "years"),
    ("3+ years", "at least", "years"),
    ("3 or more years", "or more", "years"),
])
def test_a_plus_or_floor_comparison_parses_under_schema_4(
    value: str, comparison: str, unit: str | None
) -> None:
    assert derive_quantity(value, comparison, unit, plus_is_floor=True) == GTE_36


@pytest.mark.parametrize(("value", "comparison", "unit"), [
    ("3+ years", "+", "years"),
    ("3+ years", "3+", "years"),
    ("3+ years", "Minimum", "years"),
])
def test_schema_2_and_3_keep_the_validator_20_grammar(
    value: str, comparison: str, unit: str | None
) -> None:
    assert derive_quantity(value, comparison, unit) is None


@pytest.mark.parametrize(("value", "comparison"), [
    ("3+ years", "more than"),   # "more than 3+" is no grammar, never a floor
    ("3-5 years", "+"),          # a plus over a range is ambiguous
    ("3+ years", "4+"),          # a quoted number that is not the value's
])
def test_the_plus_rule_guesses_nothing(value: str, comparison: str) -> None:
    assert derive_quantity(value, comparison, "years", plus_is_floor=True) is None


def test_the_unit_rules_still_hold_under_the_plus_rule() -> None:
    # validator/20's carried-unit rule: a known unit the record cannot spell
    assert derive_quantity("13+ days per year", "+", None, plus_is_floor=True) is None


EXP_MD = "## Requirements\n3+ years of experience in sales.\n"


def _experience_emit(comparison: str) -> dict[str, Any]:
    statement = s4_statement("s1", "qualification", "b000002")
    statement["fact_ids"] = ["f1"]
    emit = s4_emit([statement], [("b000001", []), ("b000002", ["s1"])])
    value = quote("b000002", "3+ years")
    emit["facts"]["presence"]["experience"] = {"state": "stated", "evidence": [value]}
    emit["facts"]["entries"] = [{
        "id": "f1", "family": "experience", "statement_ids": ["s1"], "condition_ids": [],
        "scope": {"kind": "overall", "evidence": None}, "date_kind": None, "component": None,
        "evidence": {"value": [value], "comparison": [quote("b000002", comparison)],
                     "unit": [quote("b000002", "years")], "currency": None,
                     "component": None, "applicability": None},
    }]
    return emit


@pytest.mark.parametrize("comparison", ["+", "3+"])
def test_a_schema_4_record_parses_and_verifies_a_quoted_plus(comparison: str) -> None:
    record = assemble4(_experience_emit(comparison), EXP_MD)
    [entry] = record["facts"]["entries"]
    assert entry["derived"]["state"] == "parsed"
    assert entry["derived"]["quantity"] == GTE_36
    report = verify(record, EXP_MD, schema_version="4")
    assert [f.code for f in report.findings if f.check == "facts"] == []
