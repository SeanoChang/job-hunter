"""The v2 engine family after the v20 bump: one active tuple, one frozen one.

`(demand-profile/v11, "3", "20")` is what a run extracts under; the retired
`(demand-profile/v10, "2", "20")` stays registered for replay alone, because a
tuple that stops resolving does not disappear from the archive — it folds under
the wrong shapes, or under none (the 2026-09-14 stranding defect).

Kept out of `tests/l2/test_bundles.py` deliberately: that module is being
edited by the parallel v20 tickets, and these are additions, not rewrites.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.bundles import get_bundle, get_bundle_for_tuple, registered
from jobhunter.l2.v2 import prompt as prompt_v2
from jobhunter.l2.v2.verify import verify as verify_v2

ACTIVE = ("demand-profile/v11", "3", "20")
REPLAY = ("demand-profile/v10", "2", "20")

DOC = "Requirements\n\nMinimum 3 years of Python experience required.\n"


def _emit(schema_version: str) -> dict[str, Any]:
    """One statement over `DOC`, in the shape its schema asks for."""
    statement: dict[str, Any] = {
        "id": "s1", "kind": "qualification", "subject": "candidate",
        "topic": "Python experience",
        "evidence": [{"block_id": "b000002", "text": None, "occurrence": None}],
        "polarity": "positive", "polarity_evidence": None,
        "condition_ids": [], "fact_ids": [], "unresolved": [],
    }
    if schema_version == "2":
        statement["importance"] = "required"
        statement["importance_evidence"] = [
            {"block_id": "b000002", "text": "required", "occurrence": 0}]
        statement["proficiency"] = None
        statement["proficiency_evidence"] = None
    else:
        statement["modality_evidence"] = [
            {"block_id": "b000002", "text": "required", "occurrence": 0}]
    none_found = {"state": "none_found", "evidence": None}
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [statement],
        "relations": {"groups": [], "conditions": [], "example_sets": []},
        "facts": {
            "presence": {
                "experience": none_found, "compensation": none_found,
                "quantities": none_found, "dates": none_found,
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
        ],
    }


def _assembled(bundle: Any, schema_version: str) -> dict[str, Any]:
    return dict(bundle.assemble(
        _emit(schema_version), DOC, document_hash=sha256_hex(DOC.encode("utf-8")),
        observed_model="test", at="2026-09-22T00:00:00+00:00",
    ))


# --- the active tuple ------------------------------------------------------


def test_the_active_v2_tuple_is_v11_schema_3_validator_20() -> None:
    b = get_bundle("v2")
    assert (b.prompt_version, b.schema_version, b.validator_version) == ACTIVE
    assert b.prompt_version == prompt_v2.PROMPT_VERSION
    assert b.template == prompt_v2.TEMPLATE
    assert b.prompt_sha() == prompt_v2.prompt_sha()
    assert b.compat_validators == ("17", "18", "19")


def test_the_active_bundle_renders_the_v11_prompt() -> None:
    b = get_bundle("v2")
    assert b.render(DOC, [], None) == prompt_v2.render(DOC, [], None)
    assert "importance" not in b.render(DOC, [], None).lower()


def test_the_active_bundle_assembles_and_verifies_at_schema_3() -> None:
    """The schema version travels with the bundle, not with assembly's default:
    an active bundle that assembled schema-2 records would archive attempts
    keyed "3" whose payload is a schema-2 record."""
    b = get_bundle("v2")
    record = _assembled(b, "3")
    assert record["extraction"]["schema_version"] == "3"
    statement = record["statements"][0]
    assert "importance" not in statement and "proficiency" not in statement
    assert statement["modality_evidence"][0]["text"] == "required"
    assert "section_heading" in statement  # code-derived, always present
    report = b.verify(record, DOC)
    assert report.status == "pass", [(f.check, f.code, f.path) for f in report.findings]
    # and the bundle's verify is the schema-3 reading of the shared verifier
    assert report.findings == verify_v2(record, DOC, schema_version="3").findings


def test_the_active_engine_emit_schema_is_the_schema_3_contract() -> None:
    """Whatever tightening the engine receives, it must be the schema the
    prompt describes: a schema-2 emit schema under the v11 prompt demands the
    very fields v11 stopped asking for."""
    schema = get_bundle("v2").engine_emit_schema
    assert schema is not None
    statement_fields = set(schema()["$defs"]["statement"]["properties"])
    assert "modality_evidence" in statement_fields
    assert "importance" not in statement_fields and "proficiency" not in statement_fields


# --- the frozen replay tuple ----------------------------------------------


def test_the_v10_schema_2_tuple_still_resolves_for_replay() -> None:
    b = get_bundle_for_tuple("demand-profile/v10", "2")
    assert (b.prompt_version, b.schema_version, b.validator_version) == REPLAY
    assert b.compat_validators == ("17", "18", "19")
    assert b is not get_bundle("v2")
    assert b.template == prompt_v2.TEMPLATE_V10
    assert b.prompt_sha() == prompt_v2.prompt_sha_v10()
    assert b.render(DOC, [], None) == prompt_v2.render_v10(DOC, [], None)


def test_the_replay_bundle_assembles_and_verifies_at_schema_2() -> None:
    b = get_bundle_for_tuple("demand-profile/v10", "2")
    record = _assembled(b, "2")
    assert record["extraction"]["schema_version"] == "2"
    assert record["statements"][0]["importance"] == "required"
    report = b.verify(record, DOC)
    assert report.status == "pass", [(f.check, f.code, f.path) for f in report.findings]


def test_a_retired_v2_prompt_still_folds_under_the_schema_2_shapes() -> None:
    """v6..v9 attempts are schema-2 records too. With the active bundle at
    schema 3 nothing registered claims their shape any more, so the frozen
    registration is what keeps a mixed archive folding: a retired prompt takes
    the frozen bundle of its own SCHEMA, never v1's projections (which raised
    KeyError('demand_profile') on the first mixed-archive catch-up)."""
    from jobhunter.l2.runner import _bundle_for

    frozen = get_bundle_for_tuple("demand-profile/v10", "2")
    assert get_bundle_for_tuple("demand-profile/v7", "2") is frozen
    assert _bundle_for("demand-profile/v7", "2") is frozen
    assert _bundle_for("demand-profile/v11", "3") is get_bundle("v2")


def test_an_unclaimed_schema_is_still_a_key_error() -> None:
    """The schema fallback reaches frozen registrations only: a v1-era tuple
    no bundle claims must still raise rather than be relabelled by today's
    shapes."""
    with pytest.raises(KeyError):
        get_bundle_for_tuple("demand-profile/v4", "1")
    with pytest.raises(KeyError):
        get_bundle_for_tuple("demand-profile/v10", "1")
    with pytest.raises(KeyError):
        get_bundle_for_tuple("demand-profile/v11", "9")


def test_the_frozen_registration_is_not_selectable() -> None:
    """Replay resolves it by tuple; nothing can RUN under it. `registered()` is
    the selection surface config mirrors, and a frozen prompt is not something
    a live drain may be pointed at by name."""
    from jobhunter import config

    assert registered() == ("v1", "v2")
    assert set(config._L2_BUNDLES_WIRED) == set(registered())
    assert get_bundle("v2").prompt_version == "demand-profile/v11"


def test_both_registrations_keep_the_audit_phase() -> None:
    from jobhunter.l2.v2 import audit

    for b in (get_bundle("v2"), get_bundle_for_tuple("demand-profile/v10", "2")):
        assert b.audit_version == audit.AUDIT_VERSION
        assert b.audit_render is audit.render
        assert b.audit_emit_schema is audit.emit_schema
        assert b.audit_judge is audit.judge
