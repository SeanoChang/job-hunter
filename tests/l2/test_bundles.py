"""The engine tuple as data: the v1 bundle must BE today's constants.

Every assertion here is a pin against drift. The runner reads its prompt,
schema, validator, assembler, verifier and the two storage projections off the
bundle it is handed, so a v1 bundle that disagreed with `l2/prompt.py`,
`l2/transforms.py` or `store/extraction.upsert_state` would move the whole
corpus onto a different engine tuple without anything saying so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jobhunter.archive.base import ArchiveStore
from jobhunter.config import ConfigError, Settings
from jobhunter.l2 import prompt as prompt_mod
from jobhunter.l2.assemble import AssembleError
from jobhunter.l2.assemble import assemble as assemble_v1
from jobhunter.l2.bundles import get_bundle, get_bundle_for_tuple, registered
from jobhunter.l2.runner import run
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.l2.v2 import prompt as prompt_v6
from jobhunter.l2.v2 import serve
from jobhunter.l2.v2.verify import verify as verify_v2
from jobhunter.l2.verify import verify as verify_v1
from tests.l2.test_runner import GOOD, FakeEngine, _seed_doc, _settings, store  # noqa: F401

Conn = psycopg.Connection[dict[str, Any]]

FIXTURE = Path(__file__).parent / "fixtures" / "anthropic.extraction.json"


def _record() -> dict[str, Any]:
    """The anthropic record: one technical, required area mentioning
    Python/React/TypeScript — the same fixture the store's write-path test uses."""
    loaded: dict[str, Any] = json.loads(FIXTURE.read_text())
    return loaded


def test_v1_bundle_is_todays_constants() -> None:
    b = get_bundle("v1")
    assert b.name == "v1"
    assert (b.prompt_version, b.schema_version, b.validator_version) == (
        "demand-profile/v5",
        "1",
        VALIDATOR_VERSION,
    )
    assert b.prompt_version == prompt_mod.PROMPT_VERSION
    assert b.template == prompt_mod.TEMPLATE
    assert b.prompt_sha() == prompt_mod.prompt_sha()
    # the exact objects, not lookalikes: a wrapper here would be a second
    # implementation to keep in step
    assert b.render is prompt_mod.render
    assert b.assemble is assemble_v1
    assert b.verify is verify_v1


def test_the_runner_module_tuple_still_names_v1() -> None:
    """`pulse`, `views` and `cli` import `runner.SCHEMA_VERSION`, and a v1 run
    takes its identity from this module, so the names must stay v1's."""
    from jobhunter.l2 import runner

    b = get_bundle("v1")
    assert (b.prompt_version, b.schema_version, b.validator_version) == (
        runner.PROMPT_VERSION,
        runner.SCHEMA_VERSION,
        runner.VALIDATOR_VERSION,
    )


def test_v1_profile_of_is_the_stored_blob_shape() -> None:
    record = _record()
    assert get_bundle("v1").profile_of(record) == {
        "facts": record["facts"],
        "demand_profile": record["demand_profile"],
    }


def test_v1_mention_rows_reproduce_the_upsert_state_walk() -> None:
    """Same rows, same order as `upsert_state`'s area walk derives today
    (tests/store/test_extraction.py::test_profile_mentions_are_a_validated_only_aggregate)."""
    assert get_bundle("v1").mention_rows(_record()) == [
        ("Python", "technical", "required"),
        ("React", "technical", "required"),
        ("TypeScript", "technical", "required"),
    ]


def test_v1_mention_rows_normalize_and_dedupe_like_the_write_path() -> None:
    record = _record()
    record["demand_profile"]["areas"][0]["mentions"] = [
        "Python/C/C++",
        "python",
        "CI/CD",
        "HTTP/2",
        "A/B testing",
        "Python (pandas, PySpark)",
        "Kubernetes and/or Docker",
        "TCP/IP",
    ]
    assert {m for m, _, _ in get_bundle("v1").mention_rows(record)} == {
        "Python",
        "C",
        "C++",
        "CI/CD",
        "HTTP/2",
        "A/B testing",
        "Kubernetes",
        "Docker",
        "TCP/IP",
    }


def test_v1_mention_rows_read_the_profile_blob_too() -> None:
    """`settle` holds the record; a caller holding only the stored blob must
    derive the identical rows — the blob is a superset of what the walk reads."""
    b = get_bundle("v1")
    record = _record()
    assert b.mention_rows(b.profile_of(record)) == b.mention_rows(record)


def test_v1_mention_rows_are_empty_without_a_demand_profile() -> None:
    assert get_bundle("v1").mention_rows({"facts": {}}) == []


def test_the_drain_writes_the_bundles_mention_rows(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The wiring proof: `settle` hands `upsert_state` the bundle's projection
    rather than letting the store walk the blob itself, so the aggregate and the
    stored profile can only ever have come from the same record and bundle."""
    _seed_doc(pg)
    assert (
        run(_settings(), pg, store, engine=FakeEngine([GOOD]), max_docs=10, max_usd=5.0).validated
        == 1
    )
    row = pg.execute("SELECT profile FROM extractions").fetchone()
    assert row is not None
    written = [
        (r["mention"], r["area_kind"], r["importance"])
        for r in pg.execute(
            "SELECT mention, area_kind, importance FROM profile_mentions ORDER BY mention"
        ).fetchall()
    ]
    assert written and written == sorted(get_bundle("v1").mention_rows(row["profile"]))


def test_v2_bundle_is_the_v6_engine_tuple() -> None:
    b = get_bundle("v2")
    assert b.name == "v2"
    assert (b.prompt_version, b.schema_version, b.validator_version) == (
        "demand-profile/v9",
        "2",
        "15",
    )
    assert b.template == prompt_v6.TEMPLATE
    assert b.prompt_sha() == prompt_v6.prompt_sha()
    assert b.render is prompt_v6.render
    assert b.verify is verify_v2
    assert b.profile_of is serve.profile_of
    assert b.mention_rows is serve.mention_rows


def test_v2_assemble_speaks_the_runners_failure_vocabulary() -> None:
    """The runner catches ONE AssembleError to decide `attribution_failed`; the
    v2 adapter re-raises v2's as that one, errors intact."""
    emit = {"source_assessment": {"usability": "usable", "evidence": None, "note": None},
            "statements": [{"id": "s1", "kind": "qualification", "subject": "candidate",
                            "topic": "t", "evidence": [{"block_id": "b000009", "text": None,
                                                        "occurrence": None}],
                            "importance": "required", "importance_evidence": None,
                            "polarity": "positive", "polarity_evidence": None,
                            "proficiency": None, "proficiency_evidence": None,
                            "condition_ids": [], "fact_ids": [], "unresolved": []}]}
    with pytest.raises(AssembleError) as excinfo:
        get_bundle("v2").assemble(emit, "Requirements\nA degree.\n", document_hash="d" * 64,
                                  observed_model="m", at="2026-09-10T00:00:00+00:00")
    assert any("b000009" in e for e in excinfo.value.errors)


def test_get_bundle_for_tuple_maps_both_engine_tuples() -> None:
    assert get_bundle_for_tuple("demand-profile/v5", "1") is get_bundle("v1")
    assert get_bundle_for_tuple("demand-profile/v9", "2") is get_bundle("v2")
    # a historical or unregistered tuple is a KeyError, never a silent v1
    with pytest.raises(KeyError):
        get_bundle_for_tuple("demand-profile/v4", "1")
    with pytest.raises(KeyError):
        get_bundle_for_tuple("demand-profile/v9", "1")


def _env(**extra: str) -> dict[str, str]:
    return {"JOB_HUNTER_ARCHIVE_URL": "file:///unused", **extra}


def test_settings_default_to_the_v1_bundle() -> None:
    assert Settings.load(_env()).l2_bundle == "v1"
    assert Settings.load(_env(JOB_HUNTER_L2_BUNDLE="v1")).l2_bundle == "v1"


def test_settings_accept_the_wired_v2_bundle() -> None:
    assert Settings.load(_env(JOB_HUNTER_L2_BUNDLE="v2")).l2_bundle == "v2"


def test_config_knows_exactly_which_bundles_are_registered() -> None:
    """`config.py` refuses a name the registry does not carry, and it holds that
    list itself so loading settings stays free of the L2 stack. The two lists
    drifting apart is either a startup crash or a name silently unreachable."""
    from jobhunter import config

    assert set(config._L2_BUNDLES_WIRED) == set(registered())
    assert set(config._L2_BUNDLE_NAMES) >= set(registered())


def test_settings_reject_an_unknown_bundle_name() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(_env(JOB_HUNTER_L2_BUNDLE="v9"))
    assert "v1" in str(excinfo.value)  # the teaching error names what is valid


def test_retired_v2_prompt_folds_under_the_schema_bundle() -> None:
    # a bumped v2 prompt leaves archived (retired-prompt, schema-2) attempts;
    # their record shape is the schema's — never fold them under v1 shapes
    from jobhunter.l2.bundles import DEFAULT_BUNDLE
    from jobhunter.l2.runner import _bundle_for

    b = _bundle_for("demand-profile/v7", "2")
    assert b.name == "v2"
    assert _bundle_for("demand-profile/v5", "1").name == "v1"
    assert _bundle_for(None, None).name == DEFAULT_BUNDLE
