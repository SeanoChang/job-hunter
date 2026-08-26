from typing import Any

from jobhunter.l2 import VALIDATOR_VERSION, verify
from tests.l2.conftest import DOC_MD, minimal_record


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
