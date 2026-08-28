import copy
from typing import Any

import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2 import verify
from jobhunter.l2.assemble import AssembleError, assemble
from tests.l2.conftest import DOC_MD

EMIT: dict[str, Any] = {
    "facts": {
        "experience_months": {"scope": "total", "anchor": {"text": "0-2 YOE"}},
        "compensation": [],
        "deadline": None,
        "boilerplate_spans": [{"text": "Equal opportunity employer."}],
    },
    "demand_profile": {
        "areas": [
            {
                "id": "a1",
                "name": "Backend engineering",
                "kind": "technical",
                "importance": "required",
                "level": None,
                "claims": [
                    {
                        "id": "c1",
                        "quote": {"text": "**Python** and distributed systems"},
                        "importance": "required",
                        "level": None,
                        "negated": False,
                    },
                    {
                        "id": "c2",
                        "quote": {"text": "0-2 YOE preferred"},
                        "importance": "preferred",
                        "level": None,
                        "negated": False,
                    },
                ],
                "structure": {"op": "AND", "of": ["c1", "c2"]},
                "mentions": ["Python"],
            }
        ],
        "interview_evaluated": [],
    },
}


def _kwargs() -> dict[str, Any]:
    return {
        "document_hash": sha256_hex(DOC_MD.encode("utf-8")),
        "normalizer_version": "md/1",
        "observed_model": "z-ai/glm-5.2:free",
        "at": "2026-08-27T00:00:00Z",
    }


def test_assembles_verifying_record() -> None:
    record = assemble(copy.deepcopy(EMIT), DOC_MD, **_kwargs())
    report = verify(record, DOC_MD)
    assert report.status == "pass", [f"{f.check}:{f.code}@{f.path}" for f in report.findings]
    assert record["extraction"]["model"] == "z-ai/glm-5.2:free"
    assert record["facts"]["experience_months"]["min"] == 0
    assert record["facts"]["experience_months"]["max"] == 24
    area = record["demand_profile"]["areas"][0]
    assert area["description"] == {"text": None, "synthesis": "none", "run": None}
    assert area["claims"][0]["level_evidence"] is None
    assert area["claims"][0]["qualifiers"] == []


def test_fabricated_and_ambiguous_quotes_batched() -> None:
    emit = copy.deepcopy(EMIT)
    emit["demand_profile"]["areas"][0]["claims"][0]["quote"] = {"text": "Rust experience"}
    emit["demand_profile"]["areas"][0]["claims"][1]["quote"] = {"text": "re"}  # occurs twice+
    with pytest.raises(AssembleError) as exc:
        assemble(emit, DOC_MD, **_kwargs())
    joined = "\n".join(exc.value.errors)
    assert "quote not found" in joined and "matches the document for" in joined
    assert "ambiguous quote" in joined and "occurrence" in joined
    assert len(exc.value.errors) == 2  # both collected in one pass


def test_occurrence_resolves_ambiguity() -> None:
    emit = copy.deepcopy(EMIT)
    emit["demand_profile"]["areas"][0]["context"] = [{"text": "re", "occurrence": 1}]
    record = assemble(emit, DOC_MD, **_kwargs())
    ctx = record["demand_profile"]["areas"][0]["context"][0]
    assert DOC_MD[ctx["span"][0] : ctx["span"][1]] == "re"
    assert ctx["occurrence"] == 1


def test_unparseable_anchor_is_error() -> None:
    emit = copy.deepcopy(EMIT)
    emit["facts"]["experience_months"]["anchor"] = {"text": "distributed systems"}
    with pytest.raises(AssembleError) as exc:
        assemble(emit, DOC_MD, **_kwargs())
    assert any("not parseable" in e for e in exc.value.errors)


def test_single_claim_area_gets_null_structure() -> None:
    emit = copy.deepcopy(EMIT)
    area = emit["demand_profile"]["areas"][0]
    area["claims"] = area["claims"][:1]
    del area["structure"]
    record = assemble(emit, DOC_MD, **_kwargs())
    assert record["demand_profile"]["areas"][0]["structure"] is None
