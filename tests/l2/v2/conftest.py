"""Shared v2 record fixtures.

Each fixture builds its own emit + markdown pair rather than mutating a shared
one — spans are line-relative under `blocks/1`, so growing a shared fixture in
place is a silent source of block-id drift across tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.v2.assemble import assemble
from jobhunter.l2.v2.source import annotate, blocks_by_id, resolve

MD = "Requirements\nA minimum of 8 years of experience in sales.\n"
DOC_HASH = sha256_hex(MD.encode("utf-8"))
AT = "2026-09-07T00:00:00+00:00"

MENTIONS_MD = (
    "Requirements\n"
    "Bachelor's degree required.\n"
    "CPA certification preferred.\n"
)
MENTIONS_DOC_HASH = sha256_hex(MENTIONS_MD.encode("utf-8"))


def _emit_with_mentions() -> dict[str, Any]:
    # blocks: b000001 "Requirements" / b000002 "Bachelor's degree required." /
    # b000003 "CPA certification preferred."
    whole_b1 = {"block_id": "b000001", "text": None, "occurrence": None}
    whole_b2 = {"block_id": "b000002", "text": None, "occurrence": None}
    whole_b3 = {"block_id": "b000003", "text": None, "occurrence": None}
    cpa_ref = {"block_id": "b000003", "text": "CPA", "occurrence": 0}
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [
            {
                "id": "s_degree", "kind": "qualification", "subject": "candidate",
                "topic": "Bachelor's degree", "evidence": [whole_b2],
                "importance": "required", "importance_evidence": [whole_b2],
                "polarity": "positive", "polarity_evidence": None,
                "proficiency": None, "proficiency_evidence": None,
                "condition_ids": [], "fact_ids": [], "unresolved": [],
            },
            {
                # the C04 shape: a preferred certification sitting right next to
                # a required degree, inside the same all_of group and the same
                # presentation area — projection must still read "preferred"
                # off this statement, never off the area or its sibling.
                "id": "s_cert", "kind": "qualification", "subject": "candidate",
                "topic": "CPA certification", "evidence": [whole_b3],
                "importance": "preferred", "importance_evidence": [whole_b3],
                "polarity": "positive", "polarity_evidence": None,
                "proficiency": None, "proficiency_evidence": None,
                "condition_ids": [], "fact_ids": [], "unresolved": [],
            },
        ],
        "relations": {
            "groups": [
                {"id": "g1", "operator": "all_of", "members": ["s_degree", "s_cert"],
                 "evidence": [whole_b1]},
            ],
            "conditions": [], "example_sets": [],
        },
        "facts": {
            "presence": {
                "experience": {"state": "none_found", "evidence": None},
                "compensation": {"state": "none_found", "evidence": None},
                "quantities": {"state": "none_found", "evidence": None},
                "dates": {"state": "none_found", "evidence": None},
            },
            "entries": [],
        },
        "mentions": [
            {"id": "m_cpa", "surface": "CPA", "evidence": cpa_ref,
             "statement_ids": ["s_cert"], "role": "direct"},
        ],
        "areas": [
            {"id": "a1", "name": "Requirements", "kind": "credential",
             "statement_ids": ["s_degree", "s_cert"], "evidence": None},
        ],
        "block_accounting": [
            {"block_id": "b000001", "disposition": "context", "ref_ids": [],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s_degree"],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000003", "disposition": "statements", "ref_ids": ["s_cert"],
             "exclusion_reason": None, "evidence": None},
        ],
    }


@pytest.fixture
def v2_record_with_mentions() -> dict[str, Any]:
    """A quality-ineligible (offline, unaudited) record with one mention tied
    to a `preferred` statement that also sits in an `all_of` group and shares
    a presentation area with a `required` sibling statement.
    """
    return assemble(
        _emit_with_mentions(), MENTIONS_MD, document_hash=MENTIONS_DOC_HASH,
        observed_model="gpt-5.6-luna", at="2026-09-07T00:00:00+00:00",
    )


def make_emit() -> dict[str, Any]:
    """The two-block experience document: b000001 "Requirements",
    b000002 "A minimum of 8 years of experience in sales."."""
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


def bound_ref(text: str, occurrence: int = 0, block_id: str = "b000002") -> dict[str, Any]:
    """One materialized reference into MD, bound exactly as assembly binds it —
    a corruption test that needs a *valid* reference must not hand-type spans."""
    return resolve(
        {"block_id": block_id, "text": text, "occurrence": occurrence},
        blocks_by_id(annotate(MD)),
    )


def make_record() -> dict[str, Any]:
    return assemble(make_emit(), MD, document_hash=DOC_HASH,
                    observed_model="gpt-5.6-luna", at=AT)


@pytest.fixture
def v2_record() -> dict[str, Any]:
    """A clean assembled record over MD: every verification check passes."""
    return make_record()


# --- the C02/C07 English-footer shape --------------------------------------

FOOTER_MD = (
    "Responsibilities\n"
    "Own the quarterly close.\n"
    "This position requires the incumbent to have a sufficient knowledge of English "
    "to have professional verbal and written exchanges.\n"
)
FOOTER_DOC_HASH = sha256_hex(FOOTER_MD.encode("utf-8"))


def _footer_emit() -> dict[str, Any]:
    """b000001 "Responsibilities" / b000002 "Own the quarterly close." /
    b000003 the English-proficiency footer, thrown away as EEO boilerplate."""
    whole_b2 = {"block_id": "b000002", "text": None, "occurrence": None}
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [{
            "id": "s1", "kind": "responsibility", "subject": "candidate",
            "topic": "Quarterly close", "evidence": [whole_b2],
            "importance": None, "importance_evidence": None,
            "polarity": "positive", "polarity_evidence": None,
            "proficiency": None, "proficiency_evidence": None,
            "condition_ids": [], "fact_ids": [], "unresolved": [],
        }],
        "relations": {"groups": [], "conditions": [], "example_sets": []},
        "facts": {
            "presence": {
                "experience": {"state": "none_found", "evidence": None},
                "compensation": {"state": "none_found", "evidence": None},
                "quantities": {"state": "none_found", "evidence": None},
                "dates": {"state": "none_found", "evidence": None},
            },
            "entries": [],
        },
        "mentions": [],
        "areas": [],
        "block_accounting": [
            {"block_id": "b000001", "disposition": "context", "ref_ids": [],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s1"],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000003", "disposition": "excluded", "ref_ids": [],
             "exclusion_reason": "eeo", "evidence": None},
        ],
    }


@pytest.fixture
def v2_footer_record() -> dict[str, Any]:
    """The audit's C02/C07 record: a real English requirement excluded as EEO.

    The footer line carries exactly one word from the tripwire's vocabulary —
    "requires" — which is why this fixture, not a hand-written string, is what
    keeps the warning honest about the class it was written for.
    """
    return assemble(_footer_emit(), FOOTER_MD, document_hash=FOOTER_DOC_HASH,
                    observed_model="gpt-5.6-luna", at=AT)
