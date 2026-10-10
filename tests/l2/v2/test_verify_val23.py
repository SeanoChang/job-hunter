"""validator/23 (schema 4): a lead-in line and a wrapped sentence are covered by
the statement that quotes what they introduce or continue.

The 2026-10-09 v15 run quarantined 237 of ~3,000 documents; 93 of them carried
`accounting:coverage_unevidenced`, and the excerpts were two shapes the check
was never meant to catch. A lead-in ("Your paid 6-12 months' internship
includes:", "**About Us**") accounted to the statements quoting the list or
paragraph under it. And one sentence wrapped over several source lines ("...
from day one. You'll" / "build, test, and learn alongside people" / "while
making an impact ...") accounted to the statement quoting one fragment. Both
are correct bookkeeping. The check exists for the other shape — duty bullets
accounted to statements evidenced only from an intro paragraph (validator/19)
— and that shape still fails: coverage borrows only FROM a lead-in's own
section and WITHIN one sentence, never from a neighbour's.
"""

from __future__ import annotations

from typing import Any

from jobhunter.l2.v2 import facts
from jobhunter.l2.v2.verify import verify
from tests.l2.v2.conftest import FIGMA_MD, assemble4, make_figma_emit, s4_emit, s4_statement


def _coverage(record: dict[str, Any], markdown: str) -> list[str]:
    report = verify(record, markdown, schema_version="4")
    assert not [f for f in report.findings if f.check == "schema"]  # judged, not refused
    return [f.detail["block_id"] for f in report.findings if f.code == "coverage_unevidenced"]


def _account(emit: dict[str, Any], block_id: str, refs: list[str]) -> dict[str, Any]:
    for row in emit["block_accounting"]:
        if row["block_id"] == block_id:
            row["disposition"], row["ref_ids"] = "statements", refs
    return emit


def test_schema_4_is_judged_by_validator_23_or_later() -> None:
    assert int(facts.SCHEMA_4_VALIDATOR_VERSION) >= 23
    assert facts.validator_version_for("4") == facts.SCHEMA_4_VALIDATOR_VERSION
    assert facts.validator_version_for("3") == "20"  # schema 2/3 keep their table


def test_a_colon_lead_in_is_covered_by_a_statement_quoting_its_list() -> None:
    # b000002 "...which areas you're most interested in:" -> the track items below
    emit = _account(make_figma_emit(), "b000002", ["s_product", "s_backend"])
    assert _coverage(assemble4(emit, FIGMA_MD), FIGMA_MD) == []


def test_a_heading_is_covered_by_a_statement_in_its_section() -> None:
    emit = _account(make_figma_emit(), "b000001", ["s_security"])
    assert _coverage(assemble4(emit, FIGMA_MD), FIGMA_MD) == []


TWO_SECTIONS_MD = (
    "## Benefits\n"
    "Your paid internship includes:\n"
    "- Housing support.\n"
    "## Requirements\n"
    "- Pursuing a degree in Computer Science.\n"
)


def _two_sections(lead_in_refs: list[str]) -> dict[str, Any]:
    return s4_emit(
        [s4_statement("s_housing", "compensation_statement", "b000003", "Housing"),
         s4_statement("s_degree", "qualification", "b000005", "Degree")],
        [("b000001", []), ("b000002", lead_in_refs), ("b000003", ["s_housing"]),
         ("b000004", []), ("b000005", ["s_degree"])],
    )


def test_a_lead_in_borrows_only_from_its_own_section() -> None:
    assert _coverage(assemble4(_two_sections(["s_housing"]), TWO_SECTIONS_MD),
                     TWO_SECTIONS_MD) == []
    # the degree line sits under the NEXT heading: still an empty claim
    assert _coverage(assemble4(_two_sections(["s_degree"]), TWO_SECTIONS_MD),
                     TWO_SECTIONS_MD) == ["b000002"]


INTRO_THEN_DUTIES_MD = (
    "As an intern you will build tools for our platform team.\n"
    "- Write integration tests.\n"
    "- Ship a feature to production.\n"
)


def test_a_bullet_never_borrows_from_the_intro_above_it() -> None:
    """The validator/19 shape stays an error: duties accounted to a statement
    evidenced only from the intro paragraph."""
    emit = s4_emit(
        [s4_statement("s_intro", "responsibility", "b000001", "Build tools")],
        [("b000001", ["s_intro"]), ("b000002", ["s_intro"]), ("b000003", ["s_intro"])],
    )
    assert _coverage(assemble4(emit, INTRO_THEN_DUTIES_MD), INTRO_THEN_DUTIES_MD) == [
        "b000002", "b000003"]


WRAPPED_MD = (
    "An internship here means real responsibility from day one. You'll\n"
    "build, test, and learn alongside people who want to see you succeed,\n"
    "while making an impact that reaches far beyond campus.\n"
    "applications close in October.\n"
)


def _wrapped(accounting_refs: dict[str, list[str]]) -> dict[str, Any]:
    return s4_emit(
        [s4_statement("s_learn", "responsibility", "b000002", "Build and learn"),
         s4_statement("s_close", "hiring_policy", "b000004", "Deadline")],
        [(b, accounting_refs.get(b, [])) for b in ("b000001", "b000002", "b000003", "b000004")],
    )


def test_every_fragment_of_a_wrapped_sentence_shares_its_coverage() -> None:
    emit = _wrapped({"b000001": ["s_learn"], "b000002": ["s_learn"], "b000003": ["s_learn"],
                     "b000004": ["s_close"]})
    assert _coverage(assemble4(emit, WRAPPED_MD), WRAPPED_MD) == []


REPEATED_MD = (
    "Hybrid: this role works from the office three days a week.\n"
    "## About the team\n"
    "Hybrid: this role works from the office three days a week.\n"
)


def test_a_repeated_line_is_covered_by_a_quote_of_any_copy() -> None:
    """Postings repeat boilerplate; the statement quoting the first copy covers
    the second."""
    emit = s4_emit(
        [s4_statement("s_hybrid", "employment_constraint", "b000001", "Hybrid")],
        [("b000001", ["s_hybrid"]), ("b000002", []), ("b000003", ["s_hybrid"])],
    )
    assert _coverage(assemble4(emit, REPEATED_MD), REPEATED_MD) == []


def test_a_finished_sentence_does_not_join_the_next_line() -> None:
    # b000003 ends with "." — the lowercase line after it is a new sentence
    emit = _wrapped({"b000002": ["s_learn"], "b000004": ["s_learn"]})
    assert _coverage(assemble4(emit, WRAPPED_MD), WRAPPED_MD) == ["b000004"]
