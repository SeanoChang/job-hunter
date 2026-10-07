"""The engine-facing emit guard under schema 4 (parsing contract v4).

Bundle v3 hands the engine `strict_schema(engine_emit_schema("4"))`. Schema 4
keeps schema 3's statement, so the guard's fact-entry and accounting
tightenings carry over unchanged; what it adds is the authorization presence
rule the verifier enforces and the stored contract cannot express: a stated or
unresolved family cites its sentence, a none_found family cites nothing and
carries no polarity, and a stated sponsorship or citizenship entry says which
way it reads. Schema 2 and 3 come out byte-identical to before.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import jsonschema
import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.schemas import normalize_emit, strict_schema, validate_emit
from jobhunter.l2.v2.emit_guard import engine_emit_schema
from tests.l2.v2.conftest import (
    make_anduril_emit,
    make_figma_emit,
    make_may_sponsor_emit,
    make_visa_emit,
    make_work_auth_emit,
    quote,
    s4_presence,
    whole,
)

#: `engine_emit_schema(v)` for the two older shapes before schema 4 landed
GUARD_SHA = {
    "2": "8be6f30225757ccada6f29fc95479d6714a40cba8bcd0ea2571fa3ee4c4ff967",
    "3": "6ada351a7dd060781883cd63ffc20a8ed9251ccfc767a131209638f44998bd32",
}

V_EMITS = [make_visa_emit, make_figma_emit, make_anduril_emit, make_work_auth_emit,
           make_may_sponsor_emit]


def _sha(schema: dict[str, Any]) -> str:
    return sha256_hex(json.dumps(schema, sort_keys=True).encode("utf-8"))


def _validator(schema: dict[str, Any]) -> jsonschema.protocols.Validator:
    return jsonschema.Draft202012Validator(schema)


def _def_validator(name: str) -> jsonschema.protocols.Validator:
    schema = engine_emit_schema("4")
    return jsonschema.Draft202012Validator({"$defs": schema["$defs"], **schema["$defs"][name]})


@pytest.mark.parametrize("version", ["2", "3"])
def test_the_older_shapes_are_unchanged(version: str) -> None:
    assert _sha(engine_emit_schema(version)) == GUARD_SHA[version]


def test_schema_4_is_the_schema_4_contract() -> None:
    schema = engine_emit_schema("4")
    assert "schema_version 4" in schema["title"]
    assert "anyOf" not in schema["$defs"]["statement"]  # schema 3's statement
    assert "type" in schema["$defs"]["mention"]["required"]
    assert "tracks" in schema["properties"]["relations"]["required"]
    # the two tightenings every version gets
    assert "anyOf" in schema["$defs"]["fact_entry"]
    assert "anyOf" in schema["$defs"]["accounting_entry"]


@pytest.mark.parametrize("make", V_EMITS)
def test_the_v_fixtures_pass_the_guard_and_its_strict_form(make: Any) -> None:
    emit = make()
    assert validate_emit(emit, "4") == []
    guard = engine_emit_schema("4")
    assert _validator(guard).is_valid(emit), list(_validator(guard).iter_errors(emit))[:3]
    strict = strict_schema(guard)
    jsonschema.Draft202012Validator.check_schema(strict)
    assert _validator(strict).is_valid(emit)
    assert normalize_emit(copy.deepcopy(emit), "4") == emit


@pytest.mark.parametrize("family", ["sponsorship", "citizenship"])
def test_a_polarized_family_is_tightened(family: str) -> None:
    v = _def_validator("authorization_presence")
    stated = s4_presence("stated", [whole("b000002")], "negative", [quote("b000002", "not")])
    assert v.is_valid(stated)
    # stated cites its sentence and says which way it reads
    assert not v.is_valid(dict(stated, evidence=None))
    assert not v.is_valid(dict(stated, polarity=None))
    # unresolved cites its sentence; polarity may stay open
    assert v.is_valid(s4_presence("unresolved", [whole("b000002")]))
    assert not v.is_valid(s4_presence("unresolved"))
    # none_found cites nothing and reads no way
    assert v.is_valid(s4_presence())
    assert not v.is_valid(s4_presence("none_found", [whole("b000002")]))
    assert not v.is_valid(s4_presence("none_found", None, "positive"))
    assert not v.is_valid(s4_presence("none_found", None, None, [whole("b000002")]))


def test_work_authorization_is_tightened_without_a_polarity() -> None:
    v = _def_validator("work_authorization_presence")
    assert v.is_valid(s4_presence("stated", [whole("b000002")]))
    assert not v.is_valid(s4_presence("stated"))
    assert not v.is_valid(s4_presence("stated", [whole("b000002")], "positive"))
    assert v.is_valid(s4_presence())
    assert not v.is_valid(s4_presence("none_found", [whole("b000002")]))


def test_the_strict_transform_keeps_the_presence_unions() -> None:
    strict = strict_schema(engine_emit_schema("4"))
    assert "anyOf" in strict["$defs"]["authorization_presence"]
    assert "anyOf" in strict["$defs"]["work_authorization_presence"]
    # every union member spells its type out (OpenAI strict rejects bare enums)
    for name in ("authorization_presence", "work_authorization_presence"):
        for variant in strict["$defs"][name]["anyOf"]:
            for prop in variant["properties"].values():
                assert "type" in prop or "anyOf" in prop or "$ref" in prop, prop
