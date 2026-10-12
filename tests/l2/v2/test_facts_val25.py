"""validator/25 (schema 4): pay reads what the posting states, and an unmarked
qualification serves as preferred.

The 2026-10-11 answer key (50 postings) scored pay at 0.48-0.62 while the
amounts were almost always right. Three shapes lost the rest, every one a
grammar miss over spans the model had quoted correctly:

* the amount carried its own currency code or a trailing or prefixed symbol
  ("$181,050 CAD - $213,000 CAD", "CAD $112,000", "34 $ à 36 $"), or the model
  cited the range's dash as the comparison and the two ends as two values;
* the period sat on the amount's own line or the short label line above it
  ("Hourly Pay Range", "US Salary Range", "Salario mensual:"), not in a quote;
* a bare "$" in a posting that names no other dollar currency.

Every example below is a real v15 quote from that set.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhunter.l2.v2 import facts
from jobhunter.l2.v2.facts import PayContext, derive_money, derive_requirement, pay_context
from jobhunter.l2.v2.source import annotate
from jobhunter.l2.v2.verify import verify
from tests.l2.v2.conftest import assemble4, quote, s4_emit, s4_statement

US = PayContext(label="", dollar="USD")
NONE = PayContext(label="", dollar=None)


def _money(lo: str | None, hi: str | None, currency: str | None, period: str | None,
           comparison: str = "range") -> dict[str, Any]:
    return {"comparison": comparison, "min_amount": lo, "max_amount": hi,
            "currency": currency, "period": period}


def test_schema_4_is_judged_by_validator_25() -> None:
    assert facts.SCHEMA_4_VALIDATOR_VERSION == "25"
    assert facts.validator_version_for("4") == "25"
    assert facts.validator_version_for("3") == "20"


@pytest.mark.parametrize(("value", "comparison", "currency", "unit", "expected"), [
    ("$181,050 CAD - $213,000 CAD", None, "CAD CAD", None,
     _money("181050", "213000", "CAD", None)),
    ("$112,000 - CAD $140,000", None, "CAD", None, _money("112000", "140000", "CAD", None)),
    ("$72,000 - CAD 90,000", None, "CAD", None, _money("72000", "90000", "CAD", None)),
    ("34 $ à 36 $", None, "CAD", "/heure", _money("34", "36", "CAD", "hour")),
    ("€169,000 - €232,000", "between", "€", "annual", _money("169000", "232000", "EUR", "year")),
    ("143,700.00 194,400.00", "-", "USD", "annually",
     _money("143700.00", "194400.00", "USD", "year")),
    ("$1,813,900", None, "COP", "mensual", _money("1813900", "1813900", "COP", "month",
                                                  "unstated")),
    ("C$95,000 - C$120,000", None, None, None, _money("95000", "120000", "CAD", None)),
])
def test_the_schema_4_pay_grammar_reads_codes_symbols_and_split_ranges(
    value: str, comparison: str | None, currency: str | None, unit: str | None,
    expected: dict[str, Any],
) -> None:
    assert derive_money(value, comparison, currency, unit, context=NONE) == expected


def test_schema_2_and_3_keep_the_old_pay_grammar() -> None:
    assert derive_money("$181,050 CAD - $213,000 CAD", None, "CAD CAD", None) is None
    assert derive_money("$27 - $43", None, None, "per hour") == _money("27", "43", None, "hour")


@pytest.mark.parametrize(("label", "period"), [
    ("Hourly Pay Range $76.96—$92.53 USD", "hour"),
    ("Hourly PhD Pay $85—$85 USD", "hour"),
    ("US Salary Range $86,000—$114,000 USD", "year"),
    ("Annual Salary Range for jobs which could be performed in the US: $115,110.00-162,500.00 USD",
     "year"),
    ("**Salario mensual:** $1,813,900 COP", "month"),
    ("Zone 1 (Menlo Park, CA; New York, NY) $60—$60 USD", None),  # nothing says: null
    ("Hourly or annual, by level: $50—$90 USD", None),               # two periods: null
])
def test_an_uncited_period_comes_from_the_amounts_label(label: str, period: str | None) -> None:
    got = derive_money("$1—$2", None, "USD", None, context=PayContext(label=label, dollar=None))
    assert got is not None
    assert got["period"] == period


def test_a_cited_period_wins_over_the_label() -> None:
    got = derive_money("$1—$2", None, "USD", "per hour",
                       context=PayContext(label="Annual Salary Range", dollar=None))
    assert got is not None
    assert got["period"] == "hour"


@pytest.mark.parametrize(("markdown", "dollar"), [
    ("New York, NY: $148,000 - $267,500\nDublin, Ireland: € 88,500 - € 159,500\n", "USD"),
    ("Pay Range:\nLevel 1: $125,000.00 - $160,000.00\n", "USD"),
    ("Toronto, Canada: $120,000 - $150,000\n", None),
    ("Seattle: $150,000 - $180,000\nVancouver: CAD $130,000\n", None),
    ("Sydney: $140,000 - $160,000 AUD\n", None),
])
def test_a_bare_dollar_is_usd_only_where_no_other_dollar_is_named(
    markdown: str, dollar: str | None
) -> None:
    blocks = annotate(markdown)
    assert pay_context(blocks, blocks[0].id).dollar == dollar


def test_a_bare_dollar_reads_the_documents_dollar() -> None:
    assert derive_money("$27 - $43", None, None, "per hour", context=US) == _money(
        "27", "43", "USD", "hour")
    assert derive_money("$27 - $43", None, None, "per hour", context=NONE) == _money(
        "27", "43", None, "hour")
    # a cited or carried code always wins over the document's dollar
    assert derive_money("$27 - $43 CAD", None, None, None, context=US) == _money(
        "27", "43", "CAD", None)


def test_the_label_is_the_amounts_line_and_a_short_line_above_it() -> None:
    blocks = annotate("Some long paragraph about hourly roles and paid time off that runs on"
                      " well past any label length a heading would have.\n"
                      "US Salary Range\n$86,000—$114,000 USD\n")
    assert pay_context(blocks, blocks[2].id).label == "US Salary Range $86,000—$114,000 USD"
    # a long line above is a paragraph, not a label: its "hourly" is not this pay's
    assert "hourly" not in pay_context(blocks, blocks[1].id).label


@pytest.mark.parametrize(("modality", "heading", "kind", "expected"), [
    (None, "Qualifications", "qualification", "preferred"),
    (None, "What you'll bring", "qualification", "preferred"),
    (None, "Basic Qualifications", "qualification", "required"),
    ("must", "Qualifications", "qualification", "required"),
    (None, "Preferred Qualifications", "qualification", "preferred"),
    (None, "Responsibilities", "responsibility", None),
    (None, None, None, None),
])
def test_an_unmarked_qualification_is_preferred(
    modality: str | None, heading: str | None, kind: str | None, expected: str | None
) -> None:
    assert derive_requirement(modality, heading, kind=kind) == expected


PAY_MD = "## Compensation\nHourly Pay Range\n$76.96—$92.53 USD\n"


def test_a_schema_4_record_derives_and_verifies_a_labelled_period() -> None:
    statement = s4_statement("s1", "compensation_statement", "b000003")
    statement["fact_ids"] = ["f1"]
    emit = s4_emit([statement], [("b000001", []), ("b000002", []), ("b000003", ["s1"])])
    value = quote("b000003", "$76.96—$92.53")
    emit["facts"]["presence"]["compensation"] = {"state": "stated", "evidence": [value]}
    emit["facts"]["entries"] = [{
        "id": "f1", "family": "compensation", "statement_ids": ["s1"], "condition_ids": [],
        "scope": None, "date_kind": None, "component": "base",
        "evidence": {"value": [value], "comparison": None, "unit": None,
                     "currency": [quote("b000003", "USD")], "component": None,
                     "applicability": None},
    }]
    record = assemble4(emit, PAY_MD)
    [entry] = record["facts"]["entries"]
    assert entry["derived"]["money"] == _money("76.96", "92.53", "USD", "hour")
    report = verify(record, PAY_MD, schema_version="4")
    assert [f.code for f in report.findings if f.check == "facts"] == []
