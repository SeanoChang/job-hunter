"""Bundle v3: parsing contract v4's engine tuple (demand-profile/v12, "4", "21").

v3 travels with the v12 prompt, schema-4 assembly and verification (validator
21), the engine-facing schema-4 emit guard, `semantic-audit/v5`, and
`semantic-repair/v3` (wired by schema version in the runner). Bundle v2 stays
registered exactly as it was, for rollback (spec §6).
"""

from __future__ import annotations

from functools import partial
from typing import Any

import pytest

from jobhunter.config import Settings
from jobhunter.l2.bundles import BUNDLE_NAMES, get_bundle, get_bundle_for_tuple, registered
from jobhunter.l2.v2 import audit, prompt, prompt_v14, serve
from jobhunter.l2.v2.emit_guard import engine_emit_schema
from tests.l2.v2.conftest import VISA_MD, make_visa_emit

V3 = ("demand-profile/v14", "4", "21")  # v12, then v13, until 2026-10-08; see test_bundles_v13
V2 = ("demand-profile/v11", "3", "20")


def _tuple(name: str) -> tuple[str, str, str]:
    b = get_bundle(name)
    return (b.prompt_version, b.schema_version, b.validator_version)


def test_bundle_v3_is_the_contract_v4_tuple() -> None:
    assert _tuple("v3") == V3
    b = get_bundle("v3")
    assert b.name == "v3"
    assert b.template == prompt_v14.TEMPLATE
    assert b.prompt_sha() == prompt_v14.prompt_sha()
    assert b.render(VISA_MD, [], None) == prompt_v14.render(VISA_MD, [], None)


def test_the_new_tuple_resolves_for_replay() -> None:
    assert get_bundle_for_tuple("demand-profile/v14", "4") is get_bundle("v3")
    assert get_bundle_for_tuple("demand-profile/v11", "3") is get_bundle("v2")


def test_bundle_v2_is_unchanged() -> None:
    assert _tuple("v2") == V2
    b = get_bundle("v2")
    assert b.name == "v2"
    assert b.template == prompt.TEMPLATE
    assert b.audit_version == "semantic-audit/v4"
    assert b.audit_render is audit.render
    assert b.audit_emit_schema is audit.emit_schema
    assert b.compat_validators == ("17", "18", "19")
    schema = b.engine_emit_schema
    assert schema is not None and schema() == engine_emit_schema("3")


def test_bundle_v3_carries_audit_v5_and_the_schema_4_guard() -> None:
    b = get_bundle("v3")
    assert b.audit_version == "semantic-audit/v5"
    assert b.audit_render is audit.render_v5
    assert b.audit_emit_schema is audit.emit_schema_v5
    assert b.audit_judge is audit.judge
    assert b.compat_validators == () and b.migrated_from == () and b.adopt is None
    schema = b.engine_emit_schema
    assert schema is not None and schema() == engine_emit_schema("4")
    assert b.profile_of is serve.profile_of and b.mention_rows is serve.mention_rows


def test_the_runner_repairs_schema_4_under_repair_v3() -> None:
    from jobhunter.l2.runner import _repair_contract
    from jobhunter.l2.v2 import repair

    contract = _repair_contract(get_bundle("v3"))
    assert contract is not None
    assert contract.version == "semantic-repair/v3"
    assert contract.render is repair.render_v3
    assert contract.emit_schema("4") == repair.emit_schema_v3("4")
    # bundle v2's round is untouched
    contract_v2 = _repair_contract(get_bundle("v2"))
    assert contract_v2 is not None and contract_v2.version == "semantic-repair/v2"
    assert contract_v2.render is repair.render


def test_bundle_v3_assembles_and_verifies_a_visa_emit() -> None:
    from jobhunter.hashing import sha256_hex

    b = get_bundle("v3")
    record: dict[str, Any] = b.assemble(
        make_visa_emit(), VISA_MD, document_hash=sha256_hex(VISA_MD.encode("utf-8")),
        observed_model="test", at="2026-10-07T00:00:00+00:00",
        prompt_version=b.prompt_version,
    )
    assert record["extraction"]["schema_version"] == "4"
    assert record["extraction"]["validator_version"] == "21"
    assert record["authorization"]["sponsorship"] == "no"
    report = b.verify(record, VISA_MD)
    assert report.status == "pass" and report.validator_version == "21"


def test_v3_is_registered_and_selectable() -> None:
    from jobhunter import config

    assert "v3" in BUNDLE_NAMES
    assert registered() == ("v1", "v2", "v3")
    assert set(config._L2_BUNDLES_WIRED) == set(registered())
    assert set(config._L2_BUNDLE_NAMES) >= set(registered())


@pytest.mark.parametrize("name", ["v2", "v3"])
def test_config_accepts_the_bundle(name: str) -> None:
    settings = Settings.load({"JOB_HUNTER_ARCHIVE_URL": "file:///unused",
                              "JOB_HUNTER_L2_BUNDLE": name})
    assert settings.l2_bundle == name


def test_the_v3_render_is_the_v13_prompt_with_a_retry() -> None:
    render = get_bundle("v3").render
    assert render(VISA_MD, ["x"], "{}") == partial(prompt_v14.render, VISA_MD)(["x"], "{}")
