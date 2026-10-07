"""Assembly under schema 4 / validator 21 (parsing contract v4 §2.1–2.5).

Covers what schema 4 adds: the three authorization presence families and the
code-derived `authorization` object (§2.2), typed mentions (§2.3) and
`relations.tracks` (§2.5). The spec §7 cases V1–V6 are built here at assembly
level over small hand-written sources (`conftest.py`). Schema 2 and 3 assembly
stays as it was, and a test below pins that.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhunter.l2.schemas import validate_record
from jobhunter.l2.v2.assemble import AssembleError, assemble, candidate_hash
from tests.l2.v2.conftest import (
    ANDURIL_MD,
    AT,
    FIGMA_MD,
    LYFT_MD,
    MAY_SPONSOR_MD,
    S3_DOC_HASH,
    S3_MD,
    VISA_MD,
    VISA_POLICY,
    WORK_AUTH_MD,
    assemble4,
    make_anduril_emit,
    make_figma_emit,
    make_lyft_emit,
    make_may_sponsor_emit,
    make_s3_emit,
    make_visa_emit,
    make_work_auth_emit,
    quote,
)


def _valid(record: dict[str, Any]) -> dict[str, Any]:
    assert validate_record(record, "4") == []
    return record


# --- V1: an explicit refusal is `no` ----------------------------------------


def test_v1_visa_will_not_sponsor_is_no_with_its_quote() -> None:
    record = _valid(assemble4(make_visa_emit(), VISA_MD))
    authorization = record["authorization"]
    assert authorization["sponsorship"] == "no"
    assert authorization["citizenship_required"] is False
    evidence = authorization["evidence"]
    assert [ref["text"] for ref in evidence["sponsorship"]] == [VISA_POLICY]
    assert evidence["citizenship"] is None and evidence["work_authorization"] is None
    presence = record["facts"]["presence"]["sponsorship"]
    assert presence["polarity"] == "negative"
    assert [ref["text"] for ref in presence["polarity_evidence"]] == ["will not sponsor"]


def test_v1_duty_line_skills_are_typed_skill_mentions_on_the_responsibility() -> None:
    record = assemble4(make_visa_emit(), VISA_MD)
    skills = {m["surface"]: m for m in record["mentions"]}
    assert set(skills) == {"Kafka", "Docker", "Kubernetes"}
    for mention in skills.values():
        assert mention["type"] == "skill"
        assert mention["role"] == "direct"
        assert mention["statement_ids"] == ["s_duty"]
    assert record["statements"][0]["kind"] == "responsibility"


# --- V2: mention types travel from the emit ---------------------------------


def test_v2_lyft_mention_types_are_carried() -> None:
    record = _valid(assemble4(make_lyft_emit(), LYFT_MD))
    types = {m["surface"]: m["type"] for m in record["mentions"]}
    assert types == {"Computer Science": "field_of_study", "Toronto": "location"}
    skills = [m for m in record["mentions"] if m["type"] == "skill"]
    assert skills == []  # no location or organization among skills


# --- V3: tracks --------------------------------------------------------------


def test_v3_figma_four_tracks_with_open_and_candidate_choice() -> None:
    record = _valid(assemble4(make_figma_emit(), FIGMA_MD))
    tracks = record["relations"]["tracks"]
    assert tracks["selection"] == "candidate_choice"
    assert [ref["text"] for ref in tracks["selection_evidence"]] == [
        "When you apply, you'll tell us which areas"
    ]
    names = [item["name_evidence"][0]["text"] for item in tracks["items"]]
    assert names == ["Product", "Backend/Infrastructure", "Security Engineering", "Open"]
    by_name = dict(zip(names, tracks["items"], strict=True))
    assert by_name["Open"]["open"] is True
    assert [i["open"] for i in tracks["items"]].count(True) == 1
    backend = by_name["Backend/Infrastructure"]
    assert backend["mention_ids"] == ["m_ds", "m_tooling"]
    assert backend["statement_ids"] == ["s_backend"]
    assert backend["evidence"][0]["text"].startswith("- Backend/Infrastructure:")
    for ref in backend["name_evidence"] + backend["evidence"]:
        assert FIGMA_MD[slice(*ref["span"])] == ref["text"]


def test_tracks_are_null_when_the_posting_describes_one_kind_of_work() -> None:
    record = _valid(assemble4(make_visa_emit(), VISA_MD))
    assert record["relations"]["tracks"] is None


# --- V4: citizenship is its own field ---------------------------------------


def test_v4_anduril_us_person_is_citizenship_required_and_sponsorship_undeclared() -> None:
    record = _valid(assemble4(make_anduril_emit(), ANDURIL_MD))
    authorization = record["authorization"]
    assert authorization["citizenship_required"] is True
    # never folded into sponsorship: the reader's filter is `no OR citizenship`
    assert authorization["sponsorship"] == "undeclared"
    assert authorization["evidence"]["citizenship"][0]["text"].startswith(
        "Must be a U.S. Person"
    )
    assert authorization["evidence"]["sponsorship"] is None


# --- V5 / V6: synthetic rulings ---------------------------------------------


def test_v5_work_authorization_alone_is_undeclared_with_its_quote_kept() -> None:
    record = _valid(assemble4(make_work_auth_emit(), WORK_AUTH_MD))
    authorization = record["authorization"]
    assert authorization["sponsorship"] == "undeclared"
    assert authorization["citizenship_required"] is False
    assert [ref["text"] for ref in authorization["evidence"]["work_authorization"]] == [
        "Must be authorized to work in the US."
    ]
    assert authorization["evidence"]["sponsorship"] is None


def test_v6_sponsorship_may_be_available_is_yes() -> None:
    record = _valid(assemble4(make_may_sponsor_emit(), MAY_SPONSOR_MD))
    assert record["authorization"]["sponsorship"] == "yes"


def test_ambiguous_polarity_is_undeclared_with_its_quote_kept() -> None:
    record = _valid(assemble4(make_may_sponsor_emit("ambiguous"), MAY_SPONSOR_MD))
    authorization = record["authorization"]
    assert authorization["sponsorship"] == "undeclared"
    assert [ref["text"] for ref in authorization["evidence"]["sponsorship"]] == [
        "Sponsorship may be available for this role."
    ]


@pytest.mark.parametrize("state", ["none_found", "unresolved"])
def test_a_polarity_outside_a_stated_sponsorship_is_undeclared(state: str) -> None:
    emit = make_may_sponsor_emit()
    emit["facts"]["presence"]["sponsorship"]["state"] = state
    assert assemble4(emit, MAY_SPONSOR_MD)["authorization"]["sponsorship"] == "undeclared"


def test_stated_without_a_polarity_is_undeclared() -> None:
    emit = make_may_sponsor_emit()
    emit["facts"]["presence"]["sponsorship"]["polarity"] = None
    emit["facts"]["presence"]["sponsorship"]["polarity_evidence"] = None
    assert assemble4(emit, MAY_SPONSOR_MD)["authorization"]["sponsorship"] == "undeclared"


def test_a_negative_citizenship_polarity_requires_nothing() -> None:
    emit = make_anduril_emit()
    emit["facts"]["presence"]["citizenship"]["polarity"] = "negative"
    assert assemble4(emit, ANDURIL_MD)["authorization"]["citizenship_required"] is False


def test_an_emitted_authorization_is_never_read() -> None:
    """`authorization` is code-owned: the model's copy is ignored (the emit
    schema rejects it outright)."""
    emit = make_work_auth_emit()
    emit["authorization"] = {"sponsorship": "yes", "citizenship_required": True}
    record = assemble4(emit, WORK_AUTH_MD)
    assert record["authorization"]["sponsorship"] == "undeclared"
    assert record["authorization"]["citizenship_required"] is False


# --- binding: the new references bind like every other ----------------------


def test_new_reference_families_collect_their_binding_errors_together() -> None:
    emit = make_figma_emit()
    emit["facts"]["presence"]["sponsorship"] = {
        "state": "stated", "evidence": [quote("b000002", "Ruby")],
        "polarity": "negative", "polarity_evidence": [quote("b000002", "will not")],
    }
    emit["relations"]["tracks"]["selection_evidence"] = [quote("b000002", "Pick one")]
    emit["relations"]["tracks"]["items"][1]["name_evidence"] = [quote("b000004", "Frontend")]
    emit["relations"]["tracks"]["items"][3]["evidence"] = [quote("b000006", "Closed")]
    with pytest.raises(AssembleError) as exc:
        assemble4(emit, FIGMA_MD)
    assert exc.value.errors == [
        "relations.tracks.selection_evidence[0]: b000002: not a literal substring: 'Pick one'",
        "relations.tracks.items[1].name_evidence[0]: "
        "b000004: not a literal substring: 'Frontend'",
        "relations.tracks.items[3].evidence[0]: b000006: not a literal substring: 'Closed'",
        "facts.presence.sponsorship.evidence[0]: b000002: not a literal substring: 'Ruby'",
        "facts.presence.sponsorship.polarity_evidence[0]: "
        "b000002: not a literal substring: 'will not'",
    ]


def test_schema_4_seals_validator_21_and_hashes_the_derived_block() -> None:
    record = assemble4(make_visa_emit(), VISA_MD)
    assert record["extraction"]["schema_version"] == "4"
    assert record["extraction"]["validator_version"] == "21"
    assert record["extraction"]["candidate_hash"] == candidate_hash(record)
    other = make_visa_emit()
    other["facts"]["presence"]["sponsorship"]["polarity"] = "positive"
    assert assemble4(other, VISA_MD)["extraction"]["candidate_hash"] != (
        record["extraction"]["candidate_hash"]
    )


# --- schema 3 is untouched ---------------------------------------------------


def test_schema_3_assembly_is_untouched_by_the_schema_4_path() -> None:
    record = assemble(make_s3_emit(), S3_MD, document_hash=S3_DOC_HASH,
                      observed_model="m", at=AT, schema_version="3")
    assert validate_record(record, "3") == []
    assert record["extraction"]["validator_version"] == "20"
    assert "authorization" not in record
    assert set(record["relations"]) == {"groups", "conditions", "example_sets"}
    assert set(record["facts"]["presence"]) == {"experience", "compensation",
                                                "quantities", "dates"}
    for presence in record["facts"]["presence"].values():
        assert set(presence) == {"state", "evidence"}
