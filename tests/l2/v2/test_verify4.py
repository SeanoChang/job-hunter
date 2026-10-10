"""Verification under schema 4 / validator 21 (parsing contract v4 §2, §3).

Covers only what schema 4 adds: the authorization presence references and the
code-derived `authorization` block, the track references and ids, and the one
new warning (a `skill` mention linked only to non-demand statements). The
schema-2 and schema-3 check tables live in `test_verify2.py` and
`test_verify.py` and stay as they are.
"""

from __future__ import annotations

import copy
from typing import Any

from jobhunter.l2.v2.verify import iter_bound_refs, verify
from tests.l2.v2.conftest import (
    ANDURIL_MD,
    FIGMA_MD,
    LYFT_MD,
    S3_MD,
    VISA_MD,
    assemble4,
    make_anduril_emit,
    make_figma_emit,
    make_lyft_emit,
    make_s3_record,
    make_visa_emit,
)


def _report(record: dict[str, Any], markdown: str) -> Any:
    return verify(record, markdown, schema_version="4")


def _errors(record: dict[str, Any], markdown: str) -> set[str]:
    return {f.code for f in _report(record, markdown).findings if f.severity == "error"}


def _warnings(record: dict[str, Any], markdown: str) -> list[Any]:
    return [f for f in _report(record, markdown).findings if f.severity == "warning"]


def test_clean_schema_4_records_pass_under_the_schema_4_validator() -> None:
    for emit, markdown in ((make_visa_emit(), VISA_MD), (make_figma_emit(), FIGMA_MD),
                           (make_anduril_emit(), ANDURIL_MD), (make_lyft_emit(), LYFT_MD)):
        report = _report(assemble4(emit, markdown), markdown)
        assert report.status == "pass", [(f.code, f.path) for f in report.findings]
        assert report.validator_version == "24"
        assert not [f for f in report.findings if f.check == "mentions"]


def test_iter_bound_refs_covers_the_new_reference_families() -> None:
    visa = assemble4(make_visa_emit(), VISA_MD)
    paths = {path for path, _ in iter_bound_refs(visa, schema_version="4")}
    assert {"facts.presence.sponsorship.evidence[0]",
            "facts.presence.sponsorship.polarity_evidence[0]"} <= paths
    figma = assemble4(make_figma_emit(), FIGMA_MD)
    paths = {path for path, _ in iter_bound_refs(figma, schema_version="4")}
    assert {"relations.tracks.selection_evidence[0]",
            "relations.tracks.items[0].name_evidence[0]",
            "relations.tracks.items[0].evidence[0]",
            "relations.tracks.items[3].name_evidence[0]"} <= paths


def test_a_miscited_polarity_quote_is_an_attribution_error() -> None:
    bad = assemble4(make_visa_emit(), VISA_MD)
    bad["facts"]["presence"]["sponsorship"]["polarity_evidence"][0]["text"] = "will not spons"
    assert "text_mismatch" in _errors(bad, VISA_MD)


def test_a_miscited_track_quote_is_an_attribution_error() -> None:
    bad = assemble4(make_figma_emit(), FIGMA_MD)
    bad["relations"]["tracks"]["items"][1]["name_evidence"][0]["block_id"] = "b000003"
    assert "outside_block" in _errors(bad, FIGMA_MD)


def test_track_ids_and_links_resolve() -> None:
    record = assemble4(make_figma_emit(), FIGMA_MD)
    unknown_statement = copy.deepcopy(record)
    unknown_statement["relations"]["tracks"]["items"][0]["statement_ids"] = ["s_nope"]
    assert "unknown_reference" in _errors(unknown_statement, FIGMA_MD)

    unknown_mention = copy.deepcopy(record)
    unknown_mention["relations"]["tracks"]["items"][1]["mention_ids"] = ["m_nope"]
    assert "unknown_reference" in _errors(unknown_mention, FIGMA_MD)

    duplicate = copy.deepcopy(record)
    duplicate["relations"]["tracks"]["items"][1]["id"] = "t_product"
    assert "duplicate_id" in _errors(duplicate, FIGMA_MD)


def test_an_authorization_the_code_did_not_derive_is_a_finding() -> None:
    """`authorization` is code-owned, so verify re-derives it from the
    presence entries — the discipline `section_heading` already follows."""
    bad = assemble4(make_visa_emit(), VISA_MD)
    bad["authorization"]["sponsorship"] = "yes"
    assert "authorization_mismatch" in _errors(bad, VISA_MD)

    citizen = assemble4(make_anduril_emit(), ANDURIL_MD)
    citizen["authorization"]["citizenship_required"] = False
    assert "authorization_mismatch" in _errors(citizen, ANDURIL_MD)


def test_a_stated_authorization_family_needs_evidence() -> None:
    bad = assemble4(make_visa_emit(), VISA_MD)
    bad["facts"]["presence"]["sponsorship"]["evidence"] = None
    bad["authorization"]["evidence"]["sponsorship"] = None
    assert "presence_mismatch" in _errors(bad, VISA_MD)


def test_work_authorization_carries_no_polarity() -> None:
    bad = assemble4(make_visa_emit(), VISA_MD)
    bad["facts"]["presence"]["work_authorization"]["polarity"] = "negative"
    report = _report(bad, VISA_MD)
    assert [f.code for f in report.findings] == ["invalid"]


def test_a_mention_without_a_type_fails_the_schema() -> None:
    bad = assemble4(make_visa_emit(), VISA_MD)
    del bad["mentions"][0]["type"]
    assert [f.code for f in _report(bad, VISA_MD).findings] == ["invalid"]


# --- the §3 warning: a skill linked only to non-demand statements ------------


def test_a_skill_linked_only_to_a_constraint_is_a_warning_never_a_failure() -> None:
    """The Toronto case under the new contract: typed `skill` and linked only
    to an `employment_constraint`."""
    record = assemble4(make_lyft_emit(toronto_type="skill"), LYFT_MD)
    report = _report(record, LYFT_MD)
    assert report.status == "pass"
    flagged = [f for f in report.findings if f.code == "skill_outside_demand"]
    assert len(flagged) == 1
    assert flagged[0].severity == "warning"
    assert flagged[0].path == "mentions[1]"
    assert flagged[0].detail["surface"] == "Toronto"


def test_a_skill_with_one_demand_statement_is_not_flagged() -> None:
    record = assemble4(make_lyft_emit(toronto_type="skill"), LYFT_MD)
    record["mentions"][1]["statement_ids"] = ["s_where", "s_degree"]
    assert not [f for f in _warnings(record, LYFT_MD) if f.code == "skill_outside_demand"]


def test_a_non_skill_mention_on_a_constraint_is_not_flagged() -> None:
    record = assemble4(make_lyft_emit(), LYFT_MD)  # Toronto typed `location`
    assert not [f for f in _warnings(record, LYFT_MD) if f.code == "skill_outside_demand"]


# --- schema 3 is untouched ---------------------------------------------------


def test_schema_3_records_still_verify_under_validator_20() -> None:
    report = verify(make_s3_record(), S3_MD, schema_version="3")
    assert report.status == "pass"
    assert report.validator_version == "20"
