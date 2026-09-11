"""One corruption test per finding code in the v2 check table.

Each test deep-copies the clean assembled record, breaks exactly one thing, and
asserts the code fires — the shape that kept v1's verifier honest: a check with
no test that can turn it red is a check nobody knows is running.
"""

from __future__ import annotations

import copy
from typing import Any

from jobhunter.l2.v2.assemble import _derive as _assembly_derive
from jobhunter.l2.v2.verify import _rederive, iter_bound_refs, verify
from tests.l2.v2.conftest import FOOTER_MD, MD, bound_ref


def _codes(record: dict[str, Any], markdown: str = MD) -> set[str]:
    return {f.code for f in verify(record, markdown).findings}


def test_clean_record_passes(v2_record: dict[str, Any]) -> None:
    report = verify(v2_record, MD)
    assert report.findings == []
    assert report.status == "pass" and report.validator_version == "14"
    assert report.metrics == {
        "n_statements": 1, "n_mentions": 0, "n_fact_entries": 1,
        "n_blocks": 2, "blocks_accounted": 2, "excluded_blocks": 0,
    }


def test_iter_bound_refs_covers_every_bound_position(v2_record: dict[str, Any]) -> None:
    paths = {path for path, _ in iter_bound_refs(v2_record)}
    assert paths == {
        "statements[0].evidence[0]",
        "statements[0].importance_evidence[0]",
        "facts.presence.experience.evidence[0]",
        "facts.entries[0].evidence.value[0]",
        "facts.entries[0].evidence.comparison[0]",
    }


# --- fail-fast preamble ----------------------------------------------------


def test_hash_mismatch(v2_record: dict[str, Any]) -> None:
    report = verify(v2_record, MD + "trailing edit\n")
    assert [f.code for f in report.findings] == ["hash_mismatch"]  # nothing else runs
    assert report.status == "fail"


def test_annotation_version(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["document"]["annotation_version"] = "blocks/2"
    report = verify(bad, MD)
    assert [f.code for f in report.findings] == ["annotation_version"]
    assert report.status == "fail"


def test_invalid(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["statements"][0]["kind"] = "vibes"
    report = verify(bad, MD)
    assert [f.code for f in report.findings] == ["invalid"]  # span checks would KeyError
    assert report.status == "fail"


# --- attribution -----------------------------------------------------------


def test_span_bounds(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["statements"][0]["evidence"][0]["span"] = [0, 10_000]
    assert "span_bounds" in _codes(bad)


def test_text_mismatch(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["statements"][0]["evidence"][0]["text"] = "Requirements"  # right block, wrong span
    assert "text_mismatch" in _codes(bad)


def test_outside_block(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["statements"][0]["evidence"][0]["block_id"] = "b000001"  # span lies in b000002
    assert "outside_block" in _codes(bad)


def test_occurrence_mismatch(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["facts"]["entries"][0]["evidence"]["comparison"][0]["occurrence"] = 2
    assert "occurrence_mismatch" in _codes(bad)


# --- references ------------------------------------------------------------


def test_unknown_reference(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["statements"][0]["fact_ids"] = ["f_ghost"]
    assert "unknown_reference" in _codes(bad)


def test_duplicate_id(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["statements"].append(copy.deepcopy(bad["statements"][0]))
    assert "duplicate_id" in _codes(bad)


def test_reference_cycle(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["relations"]["groups"] = [
        {"id": "g1", "operator": "unresolved", "members": ["g2", "s1"], "evidence": None},
        {"id": "g2", "operator": "unresolved", "members": ["g1", "s1"], "evidence": None},
    ]
    assert "reference_cycle" in _codes(bad)

    # every group on the loop is named, not just the back edge's two ends: a
    # reader handed "g1 and g3" would go looking for a cycle that isn't there
    longer = copy.deepcopy(v2_record)
    longer["relations"]["groups"] = [
        {"id": "g1", "operator": "unresolved", "members": ["g2", "s1"], "evidence": None},
        {"id": "g2", "operator": "unresolved", "members": ["g3", "s1"], "evidence": None},
        {"id": "g3", "operator": "unresolved", "members": ["g1", "s1"], "evidence": None},
    ]
    cycle = [f for f in verify(longer, MD).findings if f.code == "reference_cycle"]
    assert sorted(str(f.detail["group_id"]) for f in cycle) == ["g1", "g2", "g3"]
    # paths index the array the way every other path in the report does
    assert sorted(f.path for f in cycle) == [
        "relations.groups[0]", "relations.groups[1]", "relations.groups[2]",
    ]


def test_depth_exceeded(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    chain = [
        {"id": f"g{i}", "operator": "unresolved", "members": [f"g{i + 1}", "s1"], "evidence": None}
        for i in range(1, 6)
    ]
    chain.append({"id": "g6", "operator": "unresolved", "members": ["s1", "s1"], "evidence": None})
    bad["relations"]["groups"] = chain
    codes = _codes(bad)
    assert "depth_exceeded" in codes and "reference_cycle" not in codes


def test_connective_evidence_missing(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["relations"]["groups"] = [
        {"id": "g1", "operator": "all_of", "members": ["s1", "s1"], "evidence": None},
        # an unresolved connective may stay bare: the model saw no operator word
        {"id": "g2", "operator": "unresolved", "members": ["s1", "s1"], "evidence": None},
    ]
    findings = [f for f in verify(bad, MD).findings if f.code == "connective_evidence_missing"]
    assert len(findings) == 1


# --- statements ------------------------------------------------------------


def test_importance_missing(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["statements"][0]["importance"] = None  # a qualification always carries one
    bad["statements"][0]["importance_evidence"] = None
    assert "importance_missing" in _codes(bad)


def test_importance_unexpected(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["statements"][0]["kind"] = "responsibility"  # duties impose no applicant rule
    assert "importance_unexpected" in _codes(bad)


def test_evidence_missing(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["statements"][0]["importance_evidence"] = None
    assert "evidence_missing" in _codes(bad)

    unstated = copy.deepcopy(v2_record)
    unstated["statements"][0]["importance"] = "unstated"  # bare is exactly right here
    unstated["statements"][0]["importance_evidence"] = None
    assert "evidence_missing" not in _codes(unstated)

    proficiency = copy.deepcopy(v2_record)
    proficiency["statements"][0]["proficiency"] = "expert"
    assert "evidence_missing" in _codes(proficiency)


# --- facts -----------------------------------------------------------------


def test_fact_mismatch(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["facts"]["entries"][0]["derived"]["quantity"]["max_value"] = 96  # C01's legacy shape
    report = verify(bad, MD)
    assert any(f.code == "fact_mismatch" for f in report.findings)
    assert report.status == "fail"


def test_rederivation_matches_assembly() -> None:
    """The verifier parses the cited spans itself — a second call site of the
    same `facts` grammar, not a re-run of assembly's private helper, so a wrong
    family→grammar or state mapping in assembly can still turn `fact_mismatch`
    red. This test is the other half of that choice: the two must agree.
    """

    def aspect(text: str | None) -> list[dict[str, Any]] | None:
        return None if text is None else [{"text": text, "span": [0, len(text)]}]

    def evidence(value: str, comparison: str | None = None, unit: str | None = None,
                 currency: str | None = None) -> dict[str, Any]:
        return {"value": aspect(value), "comparison": aspect(comparison),
                "unit": aspect(unit), "currency": aspect(currency),
                "component": None, "applicability": None}

    cases = [
        ("experience", evidence("8 years", "A minimum of")),
        ("experience", evidence("a while")),  # grammar miss ⇒ present_unparsed
        ("quantity", evidence("3 projects")),
        ("compensation", evidence("$120,000", unit="per year", currency="USD")),
        ("compensation", evidence("competitive pay")),
        ("date", evidence("March 1, 2026")),
        ("date", evidence("03/04/2026")),  # ambiguous locale, still a fact
        ("date", evidence("soon")),
    ]
    for family, ev in cases:
        assert _rederive(family, ev) == _assembly_derive(family, ev), family


def test_fact_family_shape(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["facts"]["entries"][0]["date_kind"] = "application_deadline"  # family is experience
    assert "fact_family_shape" in _codes(bad)

    component = copy.deepcopy(v2_record)
    component["facts"]["entries"][0]["component"] = "base"  # compensation only
    assert "fact_family_shape" in _codes(component)


def test_presence_mismatch(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["facts"]["presence"]["experience"] = {"state": "none_found", "evidence": None}
    assert "presence_mismatch" in _codes(bad)

    absent = copy.deepcopy(v2_record)
    absent["facts"]["presence"]["compensation"] = {"state": "explicitly_absent", "evidence": None}
    assert "presence_mismatch" in _codes(absent)


# --- mentions --------------------------------------------------------------


def test_mention_ungrounded(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["mentions"] = [{
        "id": "m1", "surface": "Ruby", "evidence": bound_ref("sales"),
        "statement_ids": ["s1"], "role": "direct", "normalized_key": "ruby",
    }]
    assert "mention_ungrounded" in _codes(bad)

    rekeyed = copy.deepcopy(v2_record)
    rekeyed["mentions"] = [{
        "id": "m1", "surface": "sales", "evidence": bound_ref("sales"),
        "statement_ids": ["s1"], "role": "direct", "normalized_key": "structured query language",
    }]
    assert "mention_ungrounded" in _codes(rekeyed)


# --- block accounting ------------------------------------------------------


def test_block_unaccounted(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["block_accounting"] = bad["block_accounting"][:1]
    assert "block_unaccounted" in _codes(bad)


def test_unknown_block(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["block_accounting"].append({
        "block_id": "b000009", "disposition": "context", "ref_ids": [],
        "exclusion_reason": None, "evidence": None,
    })
    assert "unknown_block" in _codes(bad)


def test_exclusion_reason_missing(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["block_accounting"][1] = {
        "block_id": "b000002", "disposition": "excluded", "ref_ids": [],
        "exclusion_reason": None, "evidence": None,
    }
    assert "exclusion_reason_missing" in _codes(bad)

    unexpected = copy.deepcopy(v2_record)
    unexpected["block_accounting"][1]["exclusion_reason"] = "benefits"  # disposition is statements
    assert "exclusion_reason_missing" in _codes(unexpected)


def test_refs_missing(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["block_accounting"][1]["ref_ids"] = []  # disposition "statements" names nothing
    assert "refs_missing" in _codes(bad)


def test_exclusion_requirement_language_warns(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["block_accounting"][1] = {"block_id": "b000002", "disposition": "excluded",
                                  "ref_ids": [], "exclusion_reason": "eeo",
                                  "evidence": None}
    report = verify(bad, MD)  # b000002 contains "A minimum of 8 years..."
    codes = {f.code: f.severity for f in report.findings}
    assert codes.get("exclusion_requirement_language") == "warning"
    assert report.status == "pass"  # a warning never fails the record

    heading = copy.deepcopy(v2_record)
    heading["block_accounting"][0] = {"block_id": "b000001", "disposition": "excluded",
                                      "ref_ids": [], "exclusion_reason": "navigation",
                                      "evidence": None}
    # a block that literally reads "Requirements", thrown away as navigation, is
    # the auditor's business too — the noun counts, not only the verb
    assert "exclusion_requirement_language" in _codes(heading)


def test_exclusion_requirement_language_catches_the_english_footer(
    v2_footer_record: dict[str, Any],
) -> None:
    """Audit defect 3, the case the tripwire exists for.

    C02/C07's footer says "requires" and nothing else in the vocabulary — no
    must, no minimum, no proficiency word. A verb-blind pattern passes exactly
    the class of omission this warning was written to expose.
    """
    report = verify(v2_footer_record, FOOTER_MD)
    warnings = [f for f in report.findings if f.code == "exclusion_requirement_language"]
    assert [f.severity for f in warnings] == ["warning"]
    assert warnings[0].detail["block_id"] == "b000003"
    assert report.status == "pass"

    quiet = copy.deepcopy(v2_footer_record)
    quiet["block_accounting"][0] = {"block_id": "b000001", "disposition": "excluded",
                                    "ref_ids": [], "exclusion_reason": "navigation",
                                    "evidence": None}
    # a heading with no obligation word in it stays quiet: still one warning
    still = [f for f in verify(quiet, FOOTER_MD).findings
             if f.code == "exclusion_requirement_language"]
    assert [f.detail["block_id"] for f in still] == ["b000003"]


# --- usability -------------------------------------------------------------


def test_usability_conflict(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["source_assessment"]["usability"] = "partial"  # a claim that needs a citation
    assert "usability_conflict" in _codes(bad)

    empty = copy.deepcopy(v2_record)
    empty["source_assessment"]["usability"] = "empty"  # two annotated blocks say otherwise
    assert "usability_conflict" in _codes(empty)


def test_empty_with_content(v2_record: dict[str, Any]) -> None:
    bad = copy.deepcopy(v2_record)
    bad["source_assessment"]["usability"] = "placeholder"
    bad["source_assessment"]["evidence"] = [bound_ref("A minimum of")]
    codes = _codes(bad)
    assert "empty_with_content" in codes and "usability_conflict" not in codes
