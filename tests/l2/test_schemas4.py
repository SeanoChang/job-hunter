"""Schema 4: authorization presence, typed mentions, tracks (contract v4 §2).

The loader serves "4" from `schemas_data/4/`. Schema 3's bytes are pinned so
the new directory cannot drift the live v11 partition's contract.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.schemas import emit_schema, record_schema, validate_emit
from tests.l2.test_schemas import MINIMAL_EMIT_3

#: schema 3's bytes as shipped: schema 4 is a new directory, and the live v11
#: partition was judged against exactly these.
SCHEMA_3_SHA256 = {
    "emit.schema.json": "4601f7e12fa6b45e51be83ca658d2df5265a61365ba9b6a9c206af0864fb301a",
    "record.schema.json": "e667333e52e0e350cbb8874d9a06193a9aed8b2fd1578155b2282bc553f80520",
}

_AUTH_FAMILIES = ("sponsorship", "citizenship", "work_authorization")
_REF = {"block_id": "b000001", "text": "Python", "occurrence": 0}


def test_schema_3_bytes_are_frozen() -> None:
    root = resources.files("jobhunter.l2.schemas_data") / "3"
    actual = {name: sha256_hex((root / name).read_bytes()) for name in sorted(SCHEMA_3_SHA256)}
    assert actual == SCHEMA_3_SHA256


def _presence4(**overrides: dict[str, Any]) -> dict[str, Any]:
    none = {"state": "none_found", "evidence": None}
    auth = {"state": "none_found", "evidence": None, "polarity": None, "polarity_evidence": None}
    presence: dict[str, Any] = {
        "experience": none, "compensation": none, "quantities": none, "dates": none,
        **{family: auth for family in _AUTH_FAMILIES},
    }
    presence.update(overrides)
    return presence


def _emit4(**parts: Any) -> dict[str, Any]:
    emit: dict[str, Any] = {
        **MINIMAL_EMIT_3,
        "relations": {"groups": [], "conditions": [], "example_sets": [], "tracks": None},
        "facts": {"presence": _presence4(), "entries": []},
    }
    emit.update(parts)
    return emit


def _with_presence(**overrides: dict[str, Any]) -> dict[str, Any]:
    return _emit4(facts={"presence": _presence4(**overrides), "entries": []})


def _mention4(**extra: Any) -> dict[str, Any]:
    return {"id": "m1", "surface": "Python", "evidence": _REF, "statement_ids": ["s1"],
            "role": "direct", "type": "skill", **extra}


def _tracks(**extra: Any) -> dict[str, Any]:
    item = {"id": "t1", "name_evidence": [_REF], "evidence": [_REF], "open": False,
            "statement_ids": ["s1"], "mention_ids": []}
    return {"selection": "candidate_choice", "selection_evidence": [_REF],
            "items": [item], **extra}


def _with_tracks(tracks: Any) -> dict[str, Any]:
    return _emit4(relations={"groups": [], "conditions": [], "example_sets": [],
                             "tracks": tracks})


def test_version_4_resolves() -> None:
    assert emit_schema("4")["title"].endswith("schema_version 4")
    assert record_schema("4")["title"].endswith("schema_version 4")


def test_schema_4_minimal_emit_validates() -> None:
    assert validate_emit(_emit4(), "4") == []


def test_schema_4_presence_requires_the_three_authorization_families() -> None:
    for schema in (emit_schema("4"), record_schema("4")):
        presence = schema["properties"]["facts"]["properties"]["presence"]
        assert set(_AUTH_FAMILIES) <= set(presence["required"])
    emit = _emit4()
    del emit["facts"]["presence"]["sponsorship"]
    assert any("sponsorship" in e for e in validate_emit(emit, "4"))


@pytest.mark.parametrize("family", ["sponsorship", "citizenship"])
@pytest.mark.parametrize("polarity", ["positive", "negative", "ambiguous"])
def test_schema_4_sponsorship_and_citizenship_carry_a_polarity(
    family: str, polarity: str
) -> None:
    entry = {"state": "stated", "evidence": [_REF], "polarity": polarity,
             "polarity_evidence": [_REF]}
    assert validate_emit(_with_presence(**{family: entry}), "4") == []


def test_schema_4_work_authorization_polarity_must_be_null() -> None:
    entry = {"state": "stated", "evidence": [_REF], "polarity": "negative",
             "polarity_evidence": None}
    assert validate_emit(_with_presence(work_authorization=entry), "4")
    entry = {"state": "stated", "evidence": [_REF], "polarity": None,
             "polarity_evidence": [_REF]}
    assert validate_emit(_with_presence(work_authorization=entry), "4")
    entry = {"state": "stated", "evidence": [_REF], "polarity": None,
             "polarity_evidence": None}
    assert validate_emit(_with_presence(work_authorization=entry), "4") == []


def test_schema_4_authorization_states_are_closed() -> None:
    entry = {"state": "explicitly_absent", "evidence": [_REF], "polarity": None,
             "polarity_evidence": None}
    assert validate_emit(_with_presence(sponsorship=entry), "4")


def test_schema_4_mentions_require_a_type() -> None:
    for mtype in ("skill", "field_of_study", "credential", "location", "organization", "other"):
        assert validate_emit(_emit4(mentions=[_mention4(type=mtype)]), "4") == []
    assert validate_emit(_emit4(mentions=[_mention4(type="person")]), "4")
    untyped = _mention4()
    del untyped["type"]
    assert any("type" in e for e in validate_emit(_emit4(mentions=[untyped]), "4"))


def test_schema_4_tracks_shape() -> None:
    assert validate_emit(_with_tracks(_tracks()), "4") == []
    for selection in ("team_match", "unstated"):
        tracks = _tracks(selection=selection, selection_evidence=None)
        assert validate_emit(_with_tracks(tracks), "4") == []
    assert validate_emit(_with_tracks(_tracks(selection="pick_one")), "4")
    assert validate_emit(_with_tracks(_tracks(items=[])), "4")
    # `tracks` is required: null, never absent
    missing = _emit4(relations={"groups": [], "conditions": [], "example_sets": []})
    assert validate_emit(missing, "4")


def test_schema_4_authorization_is_code_owned() -> None:
    record = record_schema("4")
    assert "authorization" in record["required"]
    auth = record["$defs"]["authorization"]
    assert set(auth["required"]) == {"sponsorship", "citizenship_required", "evidence"}
    assert auth["properties"]["sponsorship"]["enum"] == ["yes", "no", "undeclared"]
    assert set(auth["properties"]["evidence"]["required"]) == set(_AUTH_FAMILIES)
    # the model's shape has no place to put one
    assert "authorization" not in emit_schema("4")["properties"]
    emit = _emit4(authorization={"sponsorship": "no"})
    assert any("authorization" in e for e in validate_emit(emit, "4"))


@pytest.mark.parametrize("load", [emit_schema, record_schema])
def test_schema_4_keeps_every_other_shape(load: Any) -> None:
    """Only presence, mentions and relations change: every other def is schema
    3's bytes. Def sets are compared both ways."""
    three, four = load("3"), load("4")
    expected = {"authorization_presence", "work_authorization_presence", "track", "tracks"}
    if load is record_schema:
        expected |= {"authorization"}
    assert set(four["$defs"]) - set(three["$defs"]) == expected
    assert set(three["$defs"]) - set(four["$defs"]) == set()
    for name, definition in three["$defs"].items():
        if name == "mention":
            continue
        assert four["$defs"][name] == definition, name
    mention3, mention4 = three["$defs"]["mention"], four["$defs"]["mention"]
    assert mention4["required"] == [*mention3["required"], "type"]
    assert {k: v for k, v in mention4["properties"].items() if k != "type"} == (
        mention3["properties"]
    )
    for key in ("source_assessment", "statements", "mentions", "areas", "block_accounting"):
        assert four["properties"][key] == three["properties"][key], key
