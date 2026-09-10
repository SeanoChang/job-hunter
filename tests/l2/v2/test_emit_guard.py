import jsonschema
import pytest

from jobhunter.l2.schemas import strict_schema
from jobhunter.l2.v2.emit_guard import engine_emit_schema


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
    assert v.is_valid(_statement(kind, "required"))
    assert not v.is_valid(_statement(kind, None))


@pytest.mark.parametrize("kind", ["responsibility", "compensation_statement", "employer_context"])
def test_other_kinds_forbid_importance(kind: str) -> None:
    v = _statement_validator()
    assert v.is_valid(_statement(kind, None))
    assert not v.is_valid(_statement(kind, "required"))


def test_strict_transform_accepts_the_union() -> None:
    strict = strict_schema(engine_emit_schema())
    assert "anyOf" in str(strict["$defs"]["statement"])
