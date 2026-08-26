from typing import Any

from jobhunter.l2 import VALIDATOR_VERSION, verify
from tests.l2.conftest import DOC_MD, make_quote, minimal_record


def codes(report: Any, check: str) -> list[str]:
    return [f.code for f in report.findings if f.check == check]


def test_valid_record_passes() -> None:
    report = verify(minimal_record(), DOC_MD)
    assert report.status == "pass"
    assert report.validator_version == VALIDATOR_VERSION


def test_doc_binding_hard_fail() -> None:
    report = verify(minimal_record(), DOC_MD + " tampered")
    assert report.status == "fail"
    assert codes(report, "doc_binding") == ["hash_mismatch"]
    assert len(report.findings) == 1  # fail-fast: nothing else ran


def test_schema_invalid() -> None:
    rec = minimal_record()
    del rec["demand_profile"]["areas"][0]["claims"][0]["negated"]
    report = verify(rec, DOC_MD)
    assert report.status == "fail"
    assert codes(report, "schema")


def test_attribution_text_mismatch_has_prefix() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["claims"][1]["quote"]["text"] = "0-2 YOE preferrd"
    report = verify(rec, DOC_MD)
    assert codes(report, "attribution") == ["text_mismatch"]
    finding = next(f for f in report.findings if f.check == "attribution")
    assert finding.detail["longest_prefix"] == len("0-2 YOE preferr")


def test_attribution_span_bounds_and_occurrence() -> None:
    rec = minimal_record()
    rec["facts"]["experience_months"]["anchor"]["span"] = [55, 9999]
    report = verify(rec, DOC_MD)
    assert codes(report, "attribution") == ["span_bounds"]

    rec2 = minimal_record()
    rec2["facts"]["experience_months"]["anchor"]["occurrence"] = 1
    report2 = verify(rec2, DOC_MD)
    assert codes(report2, "attribution") == ["occurrence_mismatch"]


def test_block_bounds_rejects_newline_quote() -> None:
    rec = minimal_record()
    claim = rec["demand_profile"]["areas"][0]["claims"][0]
    start = DOC_MD.index("**Python**")
    end = DOC_MD.index("preferred") + len("preferred")
    claim["quote"]["text"] = DOC_MD[start:end]  # spans two list lines
    claim["quote"]["span"] = [start, end]
    report = verify(rec, DOC_MD)
    assert "newline_in_quote" in codes(report, "block_bounds")


def test_structure_missing_and_dangling() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["structure"] = None
    report = verify(rec, DOC_MD)
    assert "structure_missing" in codes(report, "structure")

    rec2 = minimal_record()
    rec2["demand_profile"]["areas"][0]["structure"] = {"op": "AND", "of": ["c1", "cX"]}
    report2 = verify(rec2, DOC_MD)
    assert "unknown_claim_id" in codes(report2, "structure")


def test_structure_each_claim_exactly_once() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["structure"] = {"op": "AND", "of": ["c1", "c1"]}
    report = verify(rec, DOC_MD)
    assert "claim_reference_count" in codes(report, "structure")


def test_structure_depth_cap() -> None:
    rec = minimal_record()
    node: dict[str, object] = {"op": "AND", "of": ["c1", "c2"]}
    for _ in range(6):
        node = {"op": "OR", "of": [node, "c1"]}
    rec["demand_profile"]["areas"][0]["structure"] = node
    report = verify(rec, DOC_MD)
    assert "depth_exceeded" in codes(report, "structure")


def test_interview_evaluated_resolves() -> None:
    rec = minimal_record()
    rec["demand_profile"]["interview_evaluated"] = ["a99"]
    report = verify(rec, DOC_MD)
    assert "unknown_area_id" in codes(report, "structure")


def test_evidence_substring_of_quote_or_context() -> None:
    rec = minimal_record()
    area = rec["demand_profile"]["areas"][0]
    area["claims"][1]["level_evidence"] = "preferred"  # substring of its quote: ok
    area["claims"][0]["qualifiers"] = ["with guidance"]  # nowhere in quote or context
    report = verify(rec, DOC_MD)
    assert "fragment_unanchored" in codes(report, "evidence_substrings")

    rec2 = minimal_record()
    area2 = rec2["demand_profile"]["areas"][0]
    area2["context"] = [make_quote("0-2 YOE preferred", occurrence=0)]
    area2["claims"][0]["qualifiers"] = ["preferred"]  # in context text: ok
    report2 = verify(rec2, DOC_MD)
    assert codes(report2, "evidence_substrings") == []


def test_mentions_grounded() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["mentions"] = ["Python", "Kubernetes"]
    report = verify(rec, DOC_MD)
    assert codes(report, "mentions_grounded") == ["mention_ungrounded"]


def test_facts_rederive_mismatch_and_unanchored() -> None:
    rec = minimal_record()
    rec["facts"]["experience_months"]["max"] = 36  # anchor says 0-2 YOE -> 24
    report = verify(rec, DOC_MD)
    assert "fact_mismatch" in codes(report, "facts_rederive")

    rec2 = minimal_record()
    rec2["facts"]["experience_months"]["anchor"] = make_quote("distributed systems")
    report2 = verify(rec2, DOC_MD)
    assert "fact_unanchored" in codes(report2, "facts_rederive")


def test_overlap_claim_in_boilerplate() -> None:
    rec = minimal_record()
    rec["facts"]["boilerplate_spans"] = [make_quote("0-2 YOE preferred")]
    report = verify(rec, DOC_MD)
    assert "claim_in_boilerplate" in codes(report, "overlap")


def test_quote_shape_bounds() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["claims"][1]["quote"] = make_quote("YOE")
    report = verify(rec, DOC_MD)
    assert "quote_too_short" in codes(report, "quote_shape")


def test_template_description() -> None:
    rec = minimal_record()
    area = rec["demand_profile"]["areas"][0]
    area["description"] = {"text": "wrong", "synthesis": "none", "run": None}
    report = verify(rec, DOC_MD)
    assert "description_text_unexpected" in codes(report, "template_description")

    area["description"] = {
        "text": "Backend engineering: **Python** and distributed systems • 0-2 YOE preferred",
        "synthesis": "template",
        "run": None,
    }
    report2 = verify(rec, DOC_MD)
    assert codes(report2, "template_description") == []


def test_coverage_metrics() -> None:
    report = verify(minimal_record(), DOC_MD)
    assert report.metrics["n_areas"] == 1
    assert report.metrics["n_claims"] == 2
    c1 = make_quote("**Python** and distributed systems")["span"]
    c2 = make_quote("0-2 YOE preferred")["span"]
    boiler = make_quote("Equal opportunity employer.")["span"]
    covered = (c1[1] - c1[0]) + (c2[1] - c2[0])
    denominator = len(DOC_MD) - (boiler[1] - boiler[0])
    assert report.metrics["claim_char_coverage"] == round(covered / denominator, 4)
