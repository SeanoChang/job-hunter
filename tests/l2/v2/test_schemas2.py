"""Schema bundle 2 loads through the existing version-parameterized loader."""

from importlib import resources

from jobhunter.hashing import sha256_hex
from jobhunter.l2.schemas import (
    emit_schema,
    normalize_emit,
    record_schema,
    strict_schema,
    validate_emit,
)

#: schema 2's bytes as shipped. A frozen identifier bumps — schema 3 is a new
#: directory — and never changes underneath the corpus keyed to it: every
#: archived schema-2 record was judged against exactly these bytes.
SCHEMA_2_SHA256 = {
    "emit.schema.json": "5c10cad2f747e23d541d768d3e54e36a9ad406a711120a646c5e236ea99ecd9d",
    "record.schema.json": "ac51b7ab5f73773463d435abbea76b99cb79a51223a3f866b4f5ec15ba205228",
}


def test_schema_2_bytes_are_frozen() -> None:
    root = resources.files("jobhunter.l2.schemas_data") / "2"
    actual = {
        name: sha256_hex((root / name).read_bytes()) for name in sorted(SCHEMA_2_SHA256)
    }
    assert actual == SCHEMA_2_SHA256

MINIMAL_EMIT: dict[str, object] = {
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


def test_version_2_resolves() -> None:
    assert emit_schema("2")["title"].endswith("schema_version 2")
    assert record_schema("2")["title"].endswith("schema_version 2")


def test_minimal_emit_validates() -> None:
    assert validate_emit(MINIMAL_EMIT, "2") == []


def test_unknown_top_level_key_rejected() -> None:
    bad = {**MINIMAL_EMIT, "demand_profile": {}}
    assert any("demand_profile" in e for e in validate_emit(bad, "2"))


def test_area_importance_rejected() -> None:
    # v2's core rule: no authoritative area-level importance exists (spec §3)
    bad = {
        **MINIMAL_EMIT,
        "statements": [_stmt("s1")],
        "areas": [{"id": "a1", "name": "Skills", "kind": "technical",
                   "statement_ids": ["s1"], "evidence": None, "importance": "required"}],
    }
    assert any("importance" in e for e in validate_emit(bad, "2"))


def test_strict_mode_round_trip_keeps_required_nullables() -> None:
    # no free-form objects exist in schema 2; strict_schema must not
    # introduce the string bridge, and normalize_emit must keep required
    # nullable fields (e.g. statement.importance = null) intact
    strict = strict_schema(emit_schema("2"))
    assert '"anyOf": [{"type": "string"}' not in str(strict).replace("'", '"')
    emit = {**MINIMAL_EMIT, "statements": [_stmt("s1")]}
    out = normalize_emit(emit, "2")
    assert out["statements"][0]["importance"] is None


def _stmt(sid: str) -> dict[str, object]:
    return {
        "id": sid, "kind": "employer_context", "subject": "employer",
        "topic": "About", "evidence": [{"block_id": "b000001", "text": None,
                                         "occurrence": None}],
        "importance": None, "importance_evidence": None,
        "polarity": "positive", "polarity_evidence": None,
        "proficiency": None, "proficiency_evidence": None,
        "condition_ids": [], "fact_ids": [], "unresolved": [],
    }
