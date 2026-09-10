"""The three serving projections of a v2 record (`l2/v2/serve.py`).

`profile_of` decides what the `extractions.profile` blob holds, `mention_rows`
decides what the `profile_mentions` aggregate asserts, and `summary` is what
every current renderer reads. All three are pure, and all three are read by the
runner through the v2 bundle, so their contracts are storage contracts.

Records here are assembled from the real case fixtures. Where a test needs a
record an audit has cleared, `_audited` re-runs the frozen `quality.assess`
policy with the two audit dimensions satisfied — never a hand-set
`search_eligible`, which would make the test the policy's author.
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any

import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.v2 import serve
from jobhunter.l2.v2.assemble import assemble
from jobhunter.l2.v2.quality import assess

CASES = pathlib.Path(__file__).parent / "cases"
AT = "2026-09-10T00:00:00+00:00"
MODEL = "fixture-hand-authored"


def case_emit(case: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((CASES / f"{case}.emit.json").read_text(encoding="utf-8"))
    body: dict[str, Any] = loaded["emit"]
    return body


def case_record(case: str, emit: dict[str, Any] | None = None) -> dict[str, Any]:
    markdown = (CASES / f"{case}.source.md").read_text(encoding="utf-8")
    return assemble(
        case_emit(case) if emit is None else emit, markdown,
        document_hash=sha256_hex(markdown.encode("utf-8")), observed_model=MODEL, at=AT,
    )


def _audited(record: dict[str, Any]) -> dict[str, Any]:
    """The same record after the increment-2 audit phases complete cleanly.

    Offline assembly always leaves `semantics`/`completeness` at `not_checked`,
    so no assembled record is search-eligible until the auditor exists; this is
    the only thing that changes when it does.
    """
    out = copy.deepcopy(record)
    out["quality"] = assess(
        source=record["quality"]["source"], evidence=record["quality"]["evidence"],
        semantics="no_findings", completeness="no_findings",
    )
    assert out["quality"]["search_eligible"] is True
    return out


# --- profile_of: the stored slice -------------------------------------------


def test_profile_of_is_the_served_slice_under_a_shape_marker(v2_record: dict[str, Any]) -> None:
    profile = serve.profile_of(v2_record)
    assert profile == {
        "schema": "2",
        "statements": v2_record["statements"],
        "relations": v2_record["relations"],
        "facts": v2_record["facts"],
        "mentions": v2_record["mentions"],
        "quality": v2_record["quality"],
    }


def test_profile_of_leaves_the_bulk_of_the_record_in_the_archive(
    v2_record: dict[str, Any],
) -> None:
    """`pulse` loads every profiled event's blob, so block accounting and the
    extraction envelope stay in the archived attempt, not in Postgres."""
    profile = serve.profile_of(v2_record)
    for key in ("block_accounting", "areas", "extraction", "document", "source_assessment"):
        assert key not in profile
        assert key in v2_record  # ... and the record they were dropped from has them


def test_profile_of_is_idempotent_over_its_own_output(v2_record: dict[str, Any]) -> None:
    """`settle` folds archived records; a caller holding only the stored blob
    must get the same slice back, or a re-fold would change the row."""
    once = serve.profile_of(v2_record)
    assert serve.profile_of(once) == once


# --- mention_rows: the C04 fix at the write path ----------------------------


def test_mention_rows_take_importance_from_the_linked_statement() -> None:
    """C04: CPA/ACCA/ACA are `preferred` statements sitting in a `credential`
    area that also holds a `required` one. v1 wrote the area's importance; the
    row must read the statement's."""
    record = _audited(case_record("C04"))
    rows = serve.mention_rows(record)
    assert rows == [
        ("CPA", "qualification", "preferred"),
        ("ACCA", "qualification", "preferred"),
        ("ACA", "qualification", "preferred"),
    ]
    assert all(importance != "required" for _, _, importance in rows)


def test_mention_rows_are_empty_until_a_record_is_search_eligible(
    v2_record_with_mentions: dict[str, Any],
) -> None:
    """The quality gate at the write path: an unaudited record still stores its
    profile blob, it just never enters the mention index."""
    assert v2_record_with_mentions["quality"]["search_eligible"] is False
    assert serve.mention_rows(v2_record_with_mentions) == []
    assert serve.mention_rows(_audited(v2_record_with_mentions)) == [
        ("CPA", "qualification", "preferred")
    ]


def test_mention_rows_read_the_stored_blob_too() -> None:
    """`settle` holds the record; the blob is a superset of what the projection
    reads, so the aggregate and the profile can never disagree."""
    record = _audited(case_record("C04"))
    assert serve.mention_rows(serve.profile_of(record)) == serve.mention_rows(record)


def test_one_mention_supporting_two_equal_statements_is_one_row() -> None:
    """Two statements of the same kind and importance collapse into one row —
    the columns cannot tell them apart, and the PK would reject the duplicate."""
    emit = copy.deepcopy(case_emit("C04"))
    cpa = next(m for m in emit["mentions"] if m["id"] == "m_cpa")
    cpa["statement_ids"] = ["s_cpa", "s_netsuite"]  # both `qualification`/`preferred`
    rows = serve.mention_rows(_audited(case_record("C04", emit)))
    assert rows.count(("CPA", "qualification", "preferred")) == 1


def test_statements_without_importance_project_as_contextual() -> None:
    """Responsibilities, compensation statements and employer context carry a
    null importance (spec §3); the NOT NULL column takes v1's word for
    "named, not demanded"."""
    emit = copy.deepcopy(case_emit("C04"))
    statement = next(s for s in emit["statements"] if s["id"] == "s_cpa")
    statement["kind"] = "responsibility"
    statement["importance"] = None
    statement["importance_evidence"] = None
    rows = serve.mention_rows(_audited(case_record("C04", emit)))
    assert rows == [
        ("CPA", "responsibility", "contextual"),
        ("ACCA", "responsibility", "contextual"),
        ("ACA", "responsibility", "contextual"),
    ]


# --- summary: the v1 renderer contract --------------------------------------

V1_SUMMARY_KEYS = {"areas", "mentions", "facts"}
V1_FACT_KEYS = {"compensation", "experience_months", "deadline"}


def test_summary_speaks_the_v1_summary_shape(v2_record: dict[str, Any]) -> None:
    out = serve.summary(serve.profile_of(v2_record))
    assert set(out) == V1_SUMMARY_KEYS
    assert set(out["facts"]) == V1_FACT_KEYS
    assert set(out["areas"][0]) == {"name", "kind", "importance", "level"}


def test_summary_carries_the_c01_floor_not_a_bounded_range() -> None:
    """C01's "a minimum of 8 years" is 96 months with no ceiling — the audit
    defect was a 96..96 range, and the summary must not reintroduce it."""
    out = serve.summary(serve.profile_of(case_record("C01")))
    assert out["facts"]["experience_months"] == {"min": 96, "max": None}
    assert out["facts"]["compensation"] == [] and out["facts"]["deadline"] is None


def test_summary_areas_group_statement_topics_by_kind_and_importance() -> None:
    out = serve.summary(serve.profile_of(case_record("C04")))
    assert out["areas"] == [
        {
            "name": "NetSuite or other revenue recognition systems, "
                    "CPA or ACCA/ACA certification",
            "kind": "qualification", "importance": "preferred", "level": None,
        },
        {
            "name": "ASC 606 application experience",
            "kind": "qualification", "importance": "required", "level": None,
        },
    ]


def test_summary_mentions_are_the_surfaces_in_record_order() -> None:
    out = serve.summary(serve.profile_of(case_record("C04")))
    assert out["mentions"] == ["CPA", "ACCA", "ACA"]


def test_summary_mentions_are_bounded_like_the_v1_summary() -> None:
    blob = {"mentions": [{"surface": f"s{i}"} for i in range(serve.MAX_MENTIONS + 5)]}
    assert len(serve.summary(blob)["mentions"]) == serve.MAX_MENTIONS


def test_the_mention_bound_is_pulses_bound() -> None:
    """`serve` restates the constant instead of importing `pulse` (which reaches
    into the store); this is what keeps the two from drifting apart."""
    from jobhunter.pulse import MAX_MENTIONS

    assert serve.MAX_MENTIONS == MAX_MENTIONS


def test_summary_keeps_compensation_amounts_exactly() -> None:
    """C02's annual band: the period survives, "$" alone never implies USD, and
    the decimal strings are passed through rather than rounded into ints."""
    out = serve.summary(serve.profile_of(case_record("C02")))
    assert out["facts"]["compensation"] == [
        {"min": "210300", "max": "273400", "currency": None, "period": "year"}
    ]


def test_summary_reports_the_application_deadline() -> None:
    blob = {
        "facts": {
            "entries": [
                {"family": "date", "date_kind": "interview_date",
                 "derived": {"state": "parsed", "date": {"date": "2026-10-01"}}},
                {"family": "date", "date_kind": "application_deadline",
                 "derived": {"state": "parsed", "date": {"date": "2026-11-30"}}},
            ]
        }
    }
    assert serve.summary(blob)["facts"]["deadline"] == "2026-11-30"


def test_summary_ignores_facts_the_grammar_could_not_parse() -> None:
    """`present_unparsed` is stated-but-unparsed, never a value to invent."""
    blob = {
        "facts": {
            "entries": [
                {"family": "experience",
                 "derived": {"state": "present_unparsed", "quantity": None}},
                {"family": "compensation",
                 "derived": {"state": "present_unparsed", "money": None}},
            ]
        }
    }
    facts = serve.summary(blob)["facts"]
    assert facts == {"compensation": [], "experience_months": None, "deadline": None}


@pytest.mark.parametrize("blob", [{}, {"statements": None}, {"facts": {}}])
def test_summary_reads_defensively(blob: dict[str, Any]) -> None:
    """A blob written under an older shape must summarize, never raise."""
    out = serve.summary(blob)
    assert out == {"areas": [], "mentions": [],
                   "facts": {"compensation": [], "experience_months": None, "deadline": None}}
