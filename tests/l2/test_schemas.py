import pytest

from jobhunter.l2.schemas import (
    emit_schema,
    normalize_emit,
    record_schema,
    strict_schema,
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
