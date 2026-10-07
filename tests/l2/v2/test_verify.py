"""Verification under schema 3 / validator 20 (parsing contract v3 §2.1).

The schema-2 check table lives in `test_verify2.py` and stays exactly as it
is. This module covers only what changes when statements lose their verdicts:
the `modality_evidence` reference family, the code-owned `section_heading`,
and the importance/proficiency checks that no longer have a field to fire on.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from jobhunter.l2.v2.verify import iter_bound_refs, verify
from tests.l2.v2.conftest import MD, S3_MD, make_record


def _codes(record: dict[str, Any], markdown: str = S3_MD) -> set[str]:
    return {f.code for f in verify(record, markdown, schema_version="3").findings}


def test_clean_schema_3_record_passes(v3_record: dict[str, Any]) -> None:
    """The statement is a `qualification` — an IMPORTANCE_KIND — carrying no
    importance at all. Under schema 2 that is `importance_missing`; under 3
    there is no such field and no such question."""
    report = verify(v3_record, S3_MD, schema_version="3")
    assert [(f.code, f.severity, f.detail["block_id"]) for f in report.findings] == [
        ("context_requirement_language", "warning", "b000001")
    ]
    assert report.status == "pass" and report.validator_version == "20"


def test_iter_bound_refs_covers_the_modality_family(v3_record: dict[str, Any]) -> None:
    paths = {path for path, _ in iter_bound_refs(v3_record, schema_version="3")}
    assert paths == {
        "statements[0].evidence[0]",
        "statements[0].modality_evidence[0]",
        "facts.presence.experience.evidence[0]",
        "facts.entries[0].evidence.value[0]",
        "facts.entries[0].evidence.comparison[0]",
    }


@pytest.mark.parametrize("field", ["evidence", "modality_evidence"])
def test_a_miscited_reference_is_caught_in_either_family(
    v3_record: dict[str, Any], field: str
) -> None:
    bad = copy.deepcopy(v3_record)
    bad["statements"][0][field][0]["text"] = "Requirements"  # right block, wrong span
    assert "text_mismatch" in _codes(bad)

    outside = copy.deepcopy(v3_record)
    outside["statements"][0][field][0]["block_id"] = "b000001"  # span lies in b000002
    assert "outside_block" in _codes(outside)

    occurrence = copy.deepcopy(v3_record)
    occurrence["statements"][0][field][0]["occurrence"] = 2
    assert "occurrence_mismatch" in _codes(occurrence)


def test_a_section_heading_the_code_did_not_derive_is_a_finding(
    v3_record: dict[str, Any]
) -> None:
    """`section_heading` is code-owned, so verify re-derives it from the
    record's own first evidence span — the same discipline as a fact entry's
    `derived` and a mention's `normalized_key`."""
    bad = copy.deepcopy(v3_record)
    bad["statements"][0]["section_heading"] = "Benefits"
    assert "section_heading_mismatch" in _codes(bad)


def test_a_statement_carrying_a_verdict_fails_the_schema(v3_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v3_record)
    bad["statements"][0]["importance"] = "required"
    report = verify(bad, S3_MD, schema_version="3")
    assert [f.code for f in report.findings] == ["invalid"]
    assert "importance" in str(report.findings[0].detail["message"])


def test_schema_2_records_still_verify_under_the_frozen_check_table() -> None:
    """The schema-3 path is a parameter, not a replacement: the default is
    still schema 2, and a schema-2 record is judged by the same checks."""
    report = verify(make_record(), MD)
    assert report.status == "pass"
    assert {f.code for f in report.findings} == {"context_requirement_language"}
