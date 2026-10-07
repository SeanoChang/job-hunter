"""`semantic-audit/v5` — the auditor under parsing contract v4 (spec §4).

v4's codes, severities and triage stand. v5's template says three things out
loud: a named technology in a responsibility line with no mention is an
omission, a track list not recorded as `tracks` is a bad_exclusion, and an
authorization sentence missing from presence is an omission. A track id is a
legitimate target. v4's bytes stay exactly as bundle v2 ships them.
"""

from __future__ import annotations

import json
from typing import Any

from jobhunter.hashing import sha256_hex
from jobhunter.l2.v2 import audit
from tests.l2.v2.conftest import (
    FIGMA_MD,
    VISA_MD,
    assemble4,
    make_figma_emit,
    make_visa_emit,
)

#: semantic-audit/v4 as bundle v2 ships it, before v5 landed
V4_TEMPLATE_SHA = "b625914a3a415b7f3f7c54adaa3cc41d43cadd0fa4f8a65e78349d6c0b8bb39b"
V4_SCHEMA_SHA = "eb377b6c5f68f908b54f6353ddde8d9e3c0187485339f03cccc6bf5151a36054"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _sha(schema: dict[str, Any]) -> str:
    return sha256_hex(json.dumps(schema, sort_keys=True).encode("utf-8"))


def test_v4_is_unchanged() -> None:
    assert audit.AUDIT_VERSION == "semantic-audit/v4"
    assert audit.template_sha() == V4_TEMPLATE_SHA
    assert _sha(audit.emit_schema()) == V4_SCHEMA_SHA


def test_v5_identity() -> None:
    assert audit.AUDIT_VERSION_V5 == "semantic-audit/v5"
    assert audit.template_sha_v5() == sha256_hex(audit.TEMPLATE_V5.encode("utf-8")) == (
        "e396a3e2dd6b7078be9d83748e7c000d4a715590a7199b7875cf559acd5f9d67")
    assert audit.TEMPLATE_V5 != audit.TEMPLATE
    assert "semantic-audit/v5" in audit.emit_schema_v5()["title"]
    # the codes are v4's: one closed vocabulary
    assert audit.emit_schema_v5()["properties"]["findings"]["items"]["properties"]["code"][
        "enum"] == list(audit.CODES)


def test_v5_carries_v4_whole() -> None:
    """v5 is v4 plus a paragraph: every v4 rule is still in what it sends."""
    flat = _flat(audit.TEMPLATE_V5)
    for part in (audit._AUDITOR, audit._SCOPE, audit._EMIT_FORMAT_NOTE):
        assert _flat(part) in flat


def test_v5_states_the_contract_v4_scope() -> None:
    flat = _flat(audit.TEMPLATE_V5)
    # §4: the responsibility-line technology
    assert ("A named technology, tool, language, framework, platform or method in a "
            "responsibility line that no mention captures is an omission") in flat
    # §4: the flattened track list
    assert "a track list that was not recorded as relations.tracks is bad_exclusion" in flat
    # the authorization presence families
    assert ("A sponsorship, citizenship or work-authorization sentence that the "
            "matching facts.presence family does not record is an omission") in flat
    # a missing mention or presence entry names no candidate object
    assert "its targets stay empty" in flat
    # the code-owned block is not a finding
    assert '"authorization" block is derived by code' in flat
    assert "mention_linkage" in flat and '"type"' in flat


def test_render_v5_lists_the_schema_4_candidate() -> None:
    record = assemble4(make_visa_emit(), VISA_MD)
    candidate_hash = record["extraction"]["candidate_hash"]
    prompt = audit.render_v5(VISA_MD, candidate_hash, record)
    assert prompt.startswith(audit.TEMPLATE_V5.split("{candidate_hash}", 1)[0])
    assert f"CANDIDATE HASH: {candidate_hash}" in prompt
    body = prompt.split("<<<CANDIDATE JSON\n", 1)[1].split("\nCANDIDATE JSON>>>", 1)[0]
    shown = json.loads(body)
    assert shown["authorization"]["sponsorship"] == "no"
    assert "quality" not in shown and "extraction" not in shown


def test_a_track_id_is_a_target_the_judge_accepts() -> None:
    record = assemble4(make_figma_emit(), FIGMA_MD)
    outcome = audit.judge(
        {"findings": [{"code": "relationship", "targets": ["t_backend"], "evidence": None,
                       "explanation": "the backend track lost a statement"}],
         "unresolved": []},
        record, FIGMA_MD, record["extraction"]["candidate_hash"],
    )
    assert outcome.findings[0]["targets"] == ["t_backend"]
    assert outcome.semantics == "findings"


def test_an_omitted_responsibility_skill_with_no_target_stays_blocking() -> None:
    """The v5 instruction leaves `targets` empty for a missing mention, so the
    granularity triage (which lowers an omission whose target already quotes
    the block) cannot demote it."""
    emit = make_visa_emit()
    emit["mentions"] = []
    record = assemble4(emit, VISA_MD)
    outcome = audit.judge(
        {"findings": [{"code": "omission", "targets": [],
                       "evidence": {"block_id": "b000002", "text": "Kafka", "occurrence": 0},
                       "explanation": "Kafka is named in the duty line and has no mention"}],
         "unresolved": []},
        record, VISA_MD, record["extraction"]["candidate_hash"],
    )
    assert outcome.findings[0]["severity"] == "blocking"
    assert outcome.completeness == "findings"
