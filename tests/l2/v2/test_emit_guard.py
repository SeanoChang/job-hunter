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


def _fact_validator() -> jsonschema.protocols.Validator:
    schema = engine_emit_schema()
    wrapper = {"$defs": schema["$defs"], **schema["$defs"]["fact_entry"]}
    return jsonschema.Draft202012Validator(wrapper)


def test_fact_family_shapes() -> None:
    v = _fact_validator()
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
