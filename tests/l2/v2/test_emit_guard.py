"""The engine-facing tightening, at both schema versions it is asked for.

`engine_emit_schema()` bare is schema 2's — the tuple the archived corpus was
extracted under, and the shape every union test below is written in. The LIVE
bundle binds `engine_emit_schema("3")` (bundles.py), so the schema-3 half is
not a hypothetical branch: it is the only schema an engine is handed today.
Everything the guard says under BOTH schemas is parametrized over both, and
the statement union — which is conditionals on fields schema 3 deleted — is
asserted absent rather than assumed absent.
"""

import json
from typing import Any

import jsonschema
import pytest

from jobhunter.l2.schemas import strict_schema
from jobhunter.l2.v2.emit_guard import engine_emit_schema

SCHEMAS = ["2", "3"]


def _statement(kind: str, importance: str | None) -> dict[str, object]:
    return {
        "id": "s1", "kind": kind, "subject": "candidate", "topic": "Go",
        "evidence": [{"block_id": "b000001", "text": None, "occurrence": None}],
        "importance": importance,
        "importance_evidence": None, "polarity": "positive",
        "polarity_evidence": None, "proficiency": None,
        "proficiency_evidence": None, "condition_ids": [], "fact_ids": [],
        "unresolved": [],
    }


def _statement_validator() -> jsonschema.protocols.Validator:
    schema = engine_emit_schema()
    wrapper = {"$defs": schema["$defs"], **schema["$defs"]["statement"]}
    return jsonschema.Draft202012Validator(wrapper)


@pytest.mark.parametrize("kind", ["qualification", "employment_constraint", "hiring_policy"])
def test_importance_kinds_require_importance(kind: str) -> None:
    v = _statement_validator()
    evidenced = _statement(kind, "required")
    evidenced["importance_evidence"] = [
        {"block_id": "b000001", "text": None, "occurrence": None}
    ]
    assert v.is_valid(evidenced)
    assert v.is_valid(_statement(kind, "unstated"))
    assert not v.is_valid(_statement(kind, None))


@pytest.mark.parametrize("kind", ["responsibility", "compensation_statement", "employer_context"])
def test_other_kinds_forbid_importance(kind: str) -> None:
    v = _statement_validator()
    assert v.is_valid(_statement(kind, None))
    assert not v.is_valid(_statement(kind, "required"))


def test_strict_transform_accepts_the_union() -> None:
    strict = strict_schema(engine_emit_schema())
    assert "anyOf" in str(strict["$defs"]["statement"])


def test_the_schema_3_statement_carries_no_union_and_no_verdicts() -> None:
    """Schema 3 deleted both fields the statement union discriminated on
    (parsing contract v3 §2.1), so the union is not merely unnecessary — every
    variant it would build names properties the definition no longer has. What
    must survive is the statement itself: `modality_evidence` optional-by-null,
    nothing else to rule out."""
    statement = engine_emit_schema("3")["$defs"]["statement"]
    assert "anyOf" not in statement
    fields = set(statement["properties"])
    assert "modality_evidence" in fields
    assert not fields & {"importance", "importance_evidence",
                         "proficiency", "proficiency_evidence"}


def test_the_engine_facing_schema_3_topic_carries_no_length_cap() -> None:
    """The engine is handed `strict_schema(engine_emit_schema("3"))`, not the
    packaged file: a cap re-added by either transform would put back the
    constrained-decoding junk the schema-3 amendment (2026-09-28) removed."""
    for schema in (engine_emit_schema("3"), strict_schema(engine_emit_schema("3"))):
        statement = schema["$defs"]["statement"]
        assert "maxLength" not in statement["properties"]["topic"]
        assert "maxLength" not in json.dumps(statement)
    schema3 = engine_emit_schema("3")
    v = jsonschema.Draft202012Validator({"$defs": schema3["$defs"],
                                         **schema3["$defs"]["statement"]})
    statement3 = {
        "id": "s1", "kind": "qualification", "subject": "candidate",
        "topic": "Experience designing and operating high-throughput distributed services"
                 " at global scale",
        "evidence": [{"block_id": "b000001", "text": None, "occurrence": None}],
        "modality_evidence": None, "polarity": "positive", "polarity_evidence": None,
        "condition_ids": [], "fact_ids": [], "unresolved": [],
    }
    assert v.is_valid(statement3)


def test_the_engine_facing_schema_2_union_keeps_the_topic_cap() -> None:
    """Schema 2 is frozen: every variant of its statement union still caps."""
    variants = engine_emit_schema("2")["$defs"]["statement"]["anyOf"]
    assert {v["properties"]["topic"]["maxLength"] for v in variants} == {80}


@pytest.mark.parametrize("schema_version", SCHEMAS)
def test_the_strict_transform_survives_either_schema(schema_version: str) -> None:
    """`strict_schema` is what the engine's structured-output mode is actually
    handed; a guard output it cannot transform fails at the first live call,
    not in assembly."""
    strict = strict_schema(engine_emit_schema(schema_version))
    jsonschema.Draft202012Validator.check_schema(strict)
    # the two tightenings the guard applies under either schema survive the
    # transform as unions at the top of their definition — `in str(...)` would
    # be satisfied by an `anyOf` nested anywhere inside the untightened one
    assert "anyOf" in strict["$defs"]["fact_entry"]
    assert "anyOf" in strict["$defs"]["accounting_entry"]


def test_evidenced_importance_requires_evidence() -> None:
    v = _statement_validator()
    with_ev = _statement("qualification", "required")
    with_ev["importance_evidence"] = [{"block_id": "b000001", "text": None, "occurrence": None}]
    assert v.is_valid(with_ev)
    assert not v.is_valid(_statement("qualification", "required"))  # evidence null
    unstated = _statement("qualification", "unstated")
    assert v.is_valid(unstated)


def test_proficiency_requires_evidence() -> None:
    v = _statement_validator()
    s = _statement("responsibility", None)
    s["proficiency"] = "working"
    assert not v.is_valid(s)
    s["proficiency_evidence"] = [{"block_id": "b000001", "text": None, "occurrence": None}]
    assert v.is_valid(s)


def _def_validator(name: str, schema_version: str = "2") -> jsonschema.protocols.Validator:
    schema = engine_emit_schema(schema_version)
    wrapper = {"$defs": schema["$defs"], **schema["$defs"][name]}
    return jsonschema.Draft202012Validator(wrapper)


def _fact_validator(schema_version: str = "2") -> jsonschema.protocols.Validator:
    return _def_validator("fact_entry", schema_version)


@pytest.mark.parametrize("schema_version", SCHEMAS)
def test_fact_family_shapes(schema_version: str) -> None:
    """The per-family rules are statements about FACT entries, which schema 3
    did not touch, so they must bind identically under both — the version the
    live bundle binds included."""
    v = _fact_validator(schema_version)
    base = {
        "id": "f1", "family": "compensation", "statement_ids": [], "condition_ids": [],
        "scope": None, "date_kind": None, "component": "base",
        "evidence": {"value": [{"block_id": "b000001", "text": None, "occurrence": None}],
                     "comparison": None, "unit": None, "currency": None,
                     "component": None, "applicability": None},
    }
    assert v.is_valid(base)
    bad_scope = dict(base, scope={"kind": "team", "evidence": []})
    assert not v.is_valid(bad_scope)
    bad_date = dict(base, date_kind="other")
    assert not v.is_valid(bad_date)


@pytest.mark.parametrize("schema_version", SCHEMAS)
def test_accounting_dispositions_are_tightened(schema_version: str) -> None:
    """The other tightening the guard applies under either schema: an
    `excluded` row names a reason, a `statements` row names at least one
    object, and a `context` row names no reason. The stored contract allows
    all three violations (assembly cannot invent a reason), so, like the
    kind↔importance rule before it, this can only bind at emit time."""
    v = _def_validator("accounting_entry", schema_version)
    row: dict[str, Any] = {"block_id": "b000001", "disposition": "excluded",
                           "ref_ids": [], "exclusion_reason": "eeo", "evidence": None}
    assert v.is_valid(row)
    assert not v.is_valid(dict(row, exclusion_reason=None))
    assert v.is_valid(dict(row, disposition="statements", ref_ids=["s1"],
                           exclusion_reason=None))
    assert not v.is_valid(dict(row, disposition="statements", ref_ids=[],
                               exclusion_reason=None))
    assert v.is_valid(dict(row, disposition="context", exclusion_reason=None))
    assert not v.is_valid(dict(row, disposition="context"))
