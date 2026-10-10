"""Schema 4 serves a code-derived `requirement`: required, preferred, or null.

Parsing contract v3 removed the model's `importance` verdict because runs
disagreed on it, and kept the two cues it was read from: the posting's own
modal quote (`modality_evidence`) and the code-derived `section_heading`. A
fixed lexicon over those two quotes is checkable against the source and the
same for every sample, so it is no verdict. On 3,000 validated v15 documents
it labels 62% of qualification statements; the rest sit under headings such
as "What we look for" and stay null.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhunter.l2.v2.serve import claim_index, mention_rows, requirement_of
from tests.l2.v2.conftest import assemble4, make_s3_record, s4_emit, s4_mention, s4_statement


def _st(modality: str | None = None, heading: str | None = None) -> dict[str, Any]:
    return {"modality_evidence": [{"text": modality}] if modality else None,
            "section_heading": heading}


@pytest.mark.parametrize(("modality", "heading", "expected"), [
    ("a plus", None, "preferred"),
    ("must", None, "required"),
    ("strongly preferred", None, "preferred"),
    (None, "## Preferred Qualifications", "preferred"),
    (None, "**Nice to Have**", "preferred"),
    (None, "Basic Qualifications", "required"),
    (None, "## Minimum Qualifications", "required"),
    (None, "What You'll Need", "required"),
    (None, "## Requirements", "required"),
    ("preferred", "## Requirements", "preferred"),       # the statement's own word wins
    (None, "## About You", None),
    (None, "## What we look for", None),
    (None, "## Required and Preferred Qualifications", None),  # both: no guess
    (None, "Our surplus", None),                          # words, not substrings
    (None, None, None),
])
def test_requirement_reads_the_modal_quote_then_the_heading(
    modality: str | None, heading: str | None, expected: str | None
) -> None:
    assert requirement_of(_st(modality, heading)) == expected


REQ_MD = "## Requirements\nPython experience.\n## Nice to Have\nGo experience.\n"


def _record() -> dict[str, Any]:
    emit = s4_emit(
        [s4_statement("s1", "qualification", "b000002", "Python"),
         s4_statement("s2", "qualification", "b000004", "Go")],
        [("b000001", []), ("b000002", ["s1"]), ("b000003", []), ("b000004", ["s2"])],
        mentions=[s4_mention("m1", "Python", "b000002", ["s1"], "skill"),
                  s4_mention("m2", "Go", "b000004", ["s2"], "skill")],
    )
    return assemble4(emit, REQ_MD)


def test_schema_4_claims_carry_their_requirement() -> None:
    areas = {a["id"]: a for a in claim_index(_record())["areas"]}
    assert areas["s1"]["requirement"] == "required"
    assert areas["s2"]["requirement"] == "preferred"
    assert {c["requirement"] for c in areas["s2"]["claims"]} == {"preferred"}


def test_schema_4_skill_rows_index_the_requirement() -> None:
    assert sorted(mention_rows(_record())) == [
        ("Go", "qualification", "preferred"),
        ("Python", "qualification", "required"),
    ]


def test_schema_3_claims_are_unchanged() -> None:
    for area in claim_index(make_s3_record())["areas"]:
        assert "requirement" not in area
        assert all("requirement" not in c for c in area["claims"])
