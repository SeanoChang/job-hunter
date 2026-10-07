from typing import Any

import pytest

from jobhunter.l2.schemas import (
    emit_schema,
    normalize_emit,
    record_schema,
    strict_schema,
    validate_emit,
    validate_record,
)
from tests.l2.conftest import minimal_record


def test_schemas_load() -> None:
    assert record_schema("1")["$defs"]["quote"]["required"] == ["text", "span", "occurrence"]
    assert "span" not in emit_schema("1")["$defs"]["quote"]["properties"]
    with pytest.raises(KeyError):
        record_schema("99")


def test_minimal_record_validates() -> None:
    assert validate_record(minimal_record(), "1") == []


def test_extra_property_rejected() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["claims"][0]["quote"]["extra"] = 1
    errors = validate_record(rec, "1")
    assert errors and "extra" in errors[0]


def test_empty_fragments_rejected() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["mentions"] = [""]
    assert validate_record(rec, "1")

    rec2 = minimal_record()
    rec2["demand_profile"]["areas"][0]["claims"][0]["qualifiers"] = [""]
    assert validate_record(rec2, "1")


def test_schema_accessor_returns_copy() -> None:
    schema = record_schema("1")
    schema["$defs"]["quote"]["required"] = []
    assert record_schema("1")["$defs"]["quote"]["required"] == ["text", "span", "occurrence"]
    assert validate_record(minimal_record(), "1") == []


def test_pathlike_version_is_keyerror() -> None:
    import pytest as _pytest

    with _pytest.raises(KeyError):
        record_schema("1/record.schema.json")


def test_traversal_version_is_keyerror() -> None:
    import pytest as _pytest

    for version in ("1/../1", "../schemas_data/1", "/tmp"):
        with _pytest.raises(KeyError):
            record_schema(version)


def test_whitespace_only_evidence_rejected() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["mentions"] = [" "]
    assert validate_record(rec, "1")

    rec2 = minimal_record()
    rec2["demand_profile"]["areas"][0]["claims"][0]["level_evidence"] = "  "
    assert validate_record(rec2, "1")


# --- strict-mode compatibility (validator/5, 2026-09-06) --------------------
# JOB_HUNTER_L2_SCHEMA_STRICT was off because the emit schema carries optional
# properties, which OpenAI's strict json_schema mode rejects — and with strict
# off, the API enforced nothing and ~690 schema_invalid attempts burned across
# two drain steps. strict_schema() makes every property required (optionals
# become nullable); normalize_emit() strips the forced nulls back out before
# local validation, guided by the ORIGINAL schema's required lists.



def _walk_objects(node, defs_seen=None):
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            yield node
        for v in node.values():
            yield from _walk_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_objects(v)


def test_strict_schema_requires_every_property_everywhere() -> None:
    s = strict_schema(emit_schema("1"))
    for obj in _walk_objects(s):
        assert set(obj["required"]) == set(obj["properties"]), obj.get("description", obj)
        assert obj.get("additionalProperties") is False


def test_strict_schema_makes_former_optionals_nullable() -> None:
    s = strict_schema(emit_schema("1"))
    quote = s["$defs"]["quote"]
    # occurrence was optional; strict makes it required + nullable
    assert "occurrence" in quote["required"]
    occ = quote["properties"]["occurrence"]
    assert occ == {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]}


def test_strict_schema_leaves_the_original_untouched() -> None:
    base = emit_schema("1")
    strict_schema(base)
    assert "occurrence" not in base["$defs"]["quote"].get("required", ["text"]) or \
        base["$defs"]["quote"]["required"] == ["text"]


def test_normalize_emit_strips_nulls_on_optional_keys_only() -> None:
    emit = {
        "facts": {
            "experience_months": None,  # optional -> stripped
            "compensation": [],
            "deadline": None,           # optional -> stripped
            "boilerplate_spans": [],
        },
        "demand_profile": {
            "areas": [{
                "id": "a1", "name": "X", "kind": "technical",
                "importance": "required",
                "level": None,          # REQUIRED nullable -> kept
                "claims": [{
                    "id": "c1", "quote": {"text": "t", "occurrence": None},
                    "importance": "required", "level": None, "negated": False,
                    "threshold": None,  # optional -> stripped
                }],
                "context": [], "structure": None, "mentions": [],  # structure optional -> stripped
            }],
            "interview_evaluated": [],
        },
    }
    out = normalize_emit(emit, "1")
    assert "experience_months" not in out["facts"]
    assert "deadline" not in out["facts"]
    area = out["demand_profile"]["areas"][0]
    assert area["level"] is None
    assert "structure" not in area
    claim = area["claims"][0]
    assert "threshold" not in claim
    assert claim["level"] is None
    assert "occurrence" not in claim["quote"]
    # normalized output passes the base validator exactly as a null-free emit would
    from jobhunter.l2.schemas import validate_emit

    assert validate_emit(out, "1") == []


def test_strict_schema_bridges_free_form_objects_as_strings() -> None:
    # strict mode cannot express "any object" (probe 34061138858: HTTP 400 on
    # claim.threshold); the strict variant carries it as a JSON string and
    # normalize_emit parses it back
    s = strict_schema(emit_schema("1"))
    th = s["$defs"]["claim"]["properties"]["threshold"]
    assert th == {"anyOf": [{"type": "string"}, {"type": "null"}],
                  "description": "JSON object, serialized as a string"}


# --- schema 3: the loader knows "3" (parsing contract v3 §2.1) -------------
# Statements lose `importance`/`proficiency` and their evidence, gain a bound
# `modality_evidence` on both shapes and a code-owned `section_heading` on the
# record alone — an emitted one is an unexpected field, not a hint.

_VERDICT_FIELDS = ("importance", "importance_evidence", "proficiency", "proficiency_evidence")

MINIMAL_EMIT_3: dict[str, Any] = {
    "source_assessment": {"usability": "usable", "evidence": None, "note": None},
    "statements": [],
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
    "block_accounting": [],
}


def _stmt3(**extra: Any) -> dict[str, Any]:
    whole = {"block_id": "b000001", "text": None, "occurrence": None}
    return {
        "id": "s1", "kind": "qualification", "subject": "candidate",
        "topic": "Sales experience", "evidence": [whole],
        "modality_evidence": None,
        "polarity": "positive", "polarity_evidence": None,
        "condition_ids": [], "fact_ids": [], "unresolved": [],
        **extra,
    }


def _emit3(statement: dict[str, Any]) -> dict[str, Any]:
    return {**MINIMAL_EMIT_3, "statements": [statement]}


def test_version_3_resolves() -> None:
    assert emit_schema("3")["title"].endswith("schema_version 3")
    assert record_schema("3")["title"].endswith("schema_version 3")


def test_schema_3_statement_drops_the_verdicts_and_gains_its_context_fields() -> None:
    for schema in (emit_schema("3"), record_schema("3")):
        statement = schema["$defs"]["statement"]
        for gone in _VERDICT_FIELDS:
            assert gone not in statement["properties"]
            assert gone not in statement["required"]
        assert "modality_evidence" in statement["required"]
    record_statement = record_schema("3")["$defs"]["statement"]
    assert "section_heading" in record_statement["required"]
    # code-owned: the model's shape has no place to put one
    assert "section_heading" not in emit_schema("3")["$defs"]["statement"]["properties"]


def test_schema_3_emit_validates_a_statement_with_and_without_modality() -> None:
    quote = {"block_id": "b000001", "text": "must", "occurrence": 0}
    assert validate_emit(_emit3(_stmt3()), "3") == []
    assert validate_emit(_emit3(_stmt3(modality_evidence=[quote])), "3") == []
    # zero or one reference — a modality is one quoted phrase, never a list
    assert validate_emit(_emit3(_stmt3(modality_evidence=[quote, quote])), "3")
    assert validate_emit(_emit3(_stmt3(modality_evidence=[])), "3")


@pytest.mark.parametrize(
    ("field", "value"),
    [("importance", "required"), ("importance_evidence", None),
     ("proficiency", "expert"), ("proficiency_evidence", None),
     ("section_heading", "Requirements")],
)
def test_schema_3_emit_rejects_a_field_the_model_no_longer_owns(
    field: str, value: Any
) -> None:
    errors = validate_emit(_emit3(_stmt3(**{field: value})), "3")
    assert any(field in error for error in errors), errors


@pytest.mark.parametrize("load", [emit_schema, record_schema])
def test_schema_3_keeps_every_other_shape(load: Any) -> None:
    """Only the statement changes: everything else is schema 2's bytes.

    The `$def` key sets are compared BOTH ways. A one-directional walk over
    schema 2 can only see a def that went missing; a stray or misspelled def
    added to 3 — the likelier drift when the file is written as a copy — walks
    straight through it.
    """
    two, three = load("2"), load("3")
    assert set(three["$defs"]) - set(two["$defs"]) == {"modality"}
    assert set(two["$defs"]) - set(three["$defs"]) == {"importance", "proficiency"}
    for name, definition in two["$defs"].items():
        if name in {"statement", "importance", "proficiency"}:
            continue
        assert three["$defs"][name] == definition, name
    assert two["properties"] == three["properties"]
    assert two["required"] == three["required"]


# --- schema 3 amended in place (2026-09-28): no cap on `topic` ---------------
# The 80-character cap made constrained decoding emit junk AT the cap: 19% of
# v10 topics at 78+ characters end in junk against 0.4% below — the whole
# control-character quarantine class (165 docs). Sean approved removing it;
# schema 3 had not gone live on main, so it is amended rather than bumped.
# Truncating in code was rejected: the model's text is damaged before code
# ever sees it.

#: a real topic shape the cap used to cut: past 80 characters, and clean
LONG_TOPIC = (
    "Experience designing and operating high-throughput distributed services"
    " at global scale"
)


def test_schema_3_topic_carries_no_length_cap() -> None:
    assert len(LONG_TOPIC) > 80
    for load in (emit_schema, record_schema):
        topic = load("3")["$defs"]["statement"]["properties"]["topic"]
        assert "maxLength" not in topic
        assert topic == {"type": "string", "minLength": 1}
    assert validate_emit(_emit3(_stmt3(topic=LONG_TOPIC)), "3") == []
    # empty still fails: the floor stays
    assert validate_emit(_emit3(_stmt3(topic="")), "3")


def test_a_schema_3_record_holds_a_topic_past_80_characters() -> None:
    from tests.l2.v2.conftest import make_s3_record

    record = make_s3_record()
    record["statements"][0]["topic"] = LONG_TOPIC
    assert validate_record(record, "3") == []


def test_schema_2_keeps_its_topic_cap() -> None:
    """Schema 2's bytes are frozen (sha-pinned in tests/l2/v2/test_schemas2.py):
    the archived corpus was judged with the cap, and replays still are."""
    for load in (emit_schema, record_schema):
        topic = load("2")["$defs"]["statement"]["properties"]["topic"]
        assert topic["maxLength"] == 80


def test_normalize_emit_parses_the_threshold_string_bridge() -> None:
    emit = {
        "facts": {"compensation": [], "boilerplate_spans": []},
        "demand_profile": {
            "areas": [{
                "id": "a1", "name": "X", "kind": "technical",
                "importance": "required", "level": None,
                "claims": [
                    {"id": "c1", "quote": {"text": "t"}, "importance": "required",
                     "level": None, "negated": False, "threshold": '{"years": 5}'},
                    {"id": "c2", "quote": {"text": "u"}, "importance": "required",
                     "level": None, "negated": False, "threshold": "not json"},
                ],
            }],
            "interview_evaluated": [],
        },
    }
    out = normalize_emit(emit, "1")
    claims = out["demand_profile"]["areas"][0]["claims"]
    assert claims[0]["threshold"] == {"years": 5}
    assert claims[1]["threshold"] is None  # unparseable: null over guess
    from jobhunter.l2.schemas import validate_emit

    assert validate_emit(out, "1") == []
