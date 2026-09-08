import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.schemas import validate_record
from jobhunter.l2.v2.assemble import AssembleError, assemble, candidate_hash

MD = "Requirements\nA minimum of 8 years of experience in sales.\n"
DOC_HASH = sha256_hex(MD.encode("utf-8"))


def _emit() -> dict:
    ref_cmp = {"block_id": "b000002", "text": "A minimum of", "occurrence": 0}
    ref_val = {"block_id": "b000002", "text": "8 years", "occurrence": 0}
    whole = {"block_id": "b000002", "text": None, "occurrence": None}
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [{
            "id": "s1", "kind": "qualification", "subject": "candidate",
            "topic": "Sales experience", "evidence": [whole],
            "importance": "required",
            "importance_evidence": [{"block_id": "b000001", "text": None, "occurrence": None}],
            "polarity": "positive", "polarity_evidence": None,
            "proficiency": None, "proficiency_evidence": None,
            "condition_ids": [], "fact_ids": ["f1"], "unresolved": [],
        }],
        "relations": {"groups": [], "conditions": [], "example_sets": []},
        "facts": {
            "presence": {
                "experience": {"state": "stated", "evidence": [ref_val]},
                "compensation": {"state": "none_found", "evidence": None},
                "quantities": {"state": "none_found", "evidence": None},
                "dates": {"state": "none_found", "evidence": None},
            },
            "entries": [{
                "id": "f1", "family": "experience", "statement_ids": ["s1"],
                "condition_ids": [],
                "scope": {"kind": "overall", "evidence": None},
                "date_kind": None, "component": None,
                "evidence": {"value": [ref_val], "comparison": [ref_cmp],
                              "unit": None, "currency": None, "component": None,
                              "applicability": None},
            }],
        },
        "mentions": [],
        "areas": [{"id": "a1", "name": "Experience", "kind": "capability",
                    "statement_ids": ["s1"], "evidence": None}],
        "block_accounting": [
            {"block_id": "b000001", "disposition": "context", "ref_ids": ["s1"],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s1"],
             "exclusion_reason": None, "evidence": None},
        ],
    }


def test_assemble_binds_derives_and_validates() -> None:
    record = assemble(_emit(), MD, document_hash=DOC_HASH,
                      observed_model="gpt-5.6-luna", at="2026-09-07T00:00:00+00:00")
    assert validate_record(record, "2") == []
    stmt = record["statements"][0]
    assert MD[slice(*stmt["evidence"][0]["span"])] == stmt["evidence"][0]["text"]
    derived = record["facts"]["entries"][0]["derived"]
    assert derived["state"] == "parsed"
    assert derived["quantity"] == {"dimension": "duration", "comparison": "gte",
                                   "min_value": 96, "max_value": None,
                                   "inclusive_min": True, "inclusive_max": None,
                                   "unit": "month"}
    assert record["extraction"]["schema_version"] == "2"
    assert record["extraction"]["validator_version"] == "10"
    assert record["document"]["annotation_version"] == "blocks/1"
    assert record["extraction"]["candidate_hash"] == candidate_hash(record)


def test_candidate_hash_ignores_quality_and_itself() -> None:
    record = assemble(_emit(), MD, document_hash=DOC_HASH,
                      observed_model="m", at="2026-09-07T00:00:00+00:00")
    h = candidate_hash(record)
    record["quality"]["search_eligible"] = True  # quality never moves identity
    assert candidate_hash(record) == h


def test_unparseable_fact_is_kept_not_dropped() -> None:
    # C06's rule: stated-but-unparsed pay is distinguishable from unstated pay
    emit = _emit()
    emit["facts"]["entries"][0]["evidence"]["comparison"] = None
    emit["facts"]["entries"][0]["evidence"]["value"] = [
        {"block_id": "b000002", "text": "minimum of", "occurrence": 0}]  # no number
    record = assemble(emit, MD, document_hash=DOC_HASH,
                      observed_model="m", at="2026-09-07T00:00:00+00:00")
    derived = record["facts"]["entries"][0]["derived"]
    assert derived["state"] == "present_unparsed"
    assert derived["quantity"] is None


def test_all_binding_errors_collected() -> None:
    emit = _emit()
    emit["statements"][0]["evidence"] = [
        {"block_id": "b000099", "text": None, "occurrence": None},
        {"block_id": "b000002", "text": "Ruby", "occurrence": 0},
    ]
    with pytest.raises(AssembleError) as exc:
        assemble(emit, MD, document_hash=DOC_HASH,
                 observed_model="m", at="2026-09-07T00:00:00+00:00")
    assert len(exc.value.errors) == 2
