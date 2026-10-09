"""Bundle v3 extracts under `demand-profile/v14`; v12 and v13 stay replayable, not selectable."""

from __future__ import annotations

from jobhunter.l2.bundles import get_bundle, get_bundle_for_tuple, registered
from jobhunter.l2.v2 import prompt_v12, prompt_v13, prompt_v14


def test_bundle_v3_extracts_under_v14() -> None:
    b = get_bundle("v3")
    assert (b.prompt_version, b.schema_version, b.validator_version) == (
        "demand-profile/v14", "4", "21")
    assert b.template == prompt_v14.TEMPLATE
    assert b.prompt_sha() == prompt_v14.prompt_sha()


def test_v14_resolves_for_replay_to_bundle_v3() -> None:
    assert get_bundle_for_tuple("demand-profile/v14", "4") is get_bundle("v3")


def test_v13_attempts_still_replay_under_their_own_prompt() -> None:
    b = get_bundle_for_tuple("demand-profile/v13", "4")
    assert b.prompt_version == "demand-profile/v13"
    assert b.template == prompt_v13.TEMPLATE


def test_v12_attempts_still_replay_under_their_own_prompt() -> None:
    b = get_bundle_for_tuple("demand-profile/v12", "4")
    assert b.prompt_version == "demand-profile/v12"
    assert b.template == prompt_v12.TEMPLATE
    assert b.validator_version == "21"


def test_v12_is_frozen_not_selectable() -> None:
    assert registered() == ("v1", "v2", "v3")
    assert all(get_bundle(n).prompt_version not in ("demand-profile/v12", "demand-profile/v13")
               for n in registered())
