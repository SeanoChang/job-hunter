import pytest

from jobhunter.l2.schemas import validate_record
from jobhunter.l2.v2.assemble import AssembleError, assemble, candidate_hash
from tests.l2.v2.conftest import AT, DOC_HASH, MD, make_emit


def test_assemble_binds_derives_and_validates() -> None:
    record = assemble(make_emit(), MD, document_hash=DOC_HASH,
                      observed_model="gpt-5.6-luna", at=AT)
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
    assert record["extraction"]["validator_version"] == "13"
    assert record["document"]["annotation_version"] == "blocks/1"
    assert record["extraction"]["candidate_hash"] == candidate_hash(record)


def test_candidate_hash_ignores_quality_and_itself() -> None:
    record = assemble(make_emit(), MD, document_hash=DOC_HASH, observed_model="m", at=AT)
    h = candidate_hash(record)
    record["quality"]["search_eligible"] = True  # quality never moves identity
    assert candidate_hash(record) == h


def test_unparseable_fact_is_kept_not_dropped() -> None:
    # C06's rule: stated-but-unparsed pay is distinguishable from unstated pay
    emit = make_emit()
    emit["facts"]["entries"][0]["evidence"]["comparison"] = None
    emit["facts"]["entries"][0]["evidence"]["value"] = [
        {"block_id": "b000002", "text": "minimum of", "occurrence": 0}]  # no number
    record = assemble(emit, MD, document_hash=DOC_HASH, observed_model="m", at=AT)
    derived = record["facts"]["entries"][0]["derived"]
    assert derived["state"] == "present_unparsed"
    assert derived["quantity"] is None


def test_all_binding_errors_collected() -> None:
    emit = make_emit()
    emit["statements"][0]["evidence"] = [
        {"block_id": "b000099", "text": None, "occurrence": None},
        {"block_id": "b000002", "text": "Ruby", "occurrence": 0},
    ]
    with pytest.raises(AssembleError) as exc:
        assemble(emit, MD, document_hash=DOC_HASH, observed_model="m", at=AT)
    assert len(exc.value.errors) == 2
