"""The pure repair contract (`semantic-repair/v1`, spec §4).

Every test here is offline: `apply` is a validator over a model emit plus a
rebuild through `assemble`, so the emits are hand-written and every record
comes from a real `assemble` call over a real document.
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
from collections.abc import Iterator
from typing import Any

import jsonschema
import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.schemas import strict_schema
from jobhunter.l2.v2 import repair
from jobhunter.l2.v2.assemble import assemble
from jobhunter.l2.v2.repair import (
    KINDS,
    OPS,
    REPAIR_VERSION,
    TEMPLATE,
    RepairJudgeError,
    apply,
    emit_schema,
    object_hash,
    render,
    template_sha,
)
from jobhunter.l2.v2.source import annotate
from jobhunter.l2.v2.verify import verify

AT = "2026-09-13T00:00:00+00:00"
OTHER_HASH = "f" * 64

# b000001 "Requirements" / b000002 the experience line / b000003 the
# certification line / b000004 the travel line
REPAIR_MD = (
    "Requirements\n"
    "A minimum of 8 years of experience in sales.\n"
    "Salesforce certification preferred.\n"
    "Travel up to 20% of the time.\n"
)
REPAIR_DOC_HASH = sha256_hex(REPAIR_MD.encode("utf-8"))


def _whole(block_id: str) -> dict[str, Any]:
    return {"block_id": block_id, "text": None, "occurrence": None}


def _ref(block_id: str, text: str, occurrence: int = 0) -> dict[str, Any]:
    return {"block_id": block_id, "text": text, "occurrence": occurrence}


def _base_emit() -> dict[str, Any]:
    """The defective candidate: s2 reads `required` where the source says
    `preferred` — the `importance` finding the repair round exists to fix."""
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [
            {
                "id": "s1", "kind": "qualification", "subject": "candidate",
                "topic": "Sales experience", "evidence": [_whole("b000002")],
                "importance": "required", "importance_evidence": [_whole("b000001")],
                "polarity": "positive", "polarity_evidence": None,
                "proficiency": None, "proficiency_evidence": None,
                "condition_ids": [], "fact_ids": ["f1"], "unresolved": [],
            },
            {
                "id": "s2", "kind": "qualification", "subject": "candidate",
                "topic": "Salesforce certification", "evidence": [_whole("b000003")],
                "importance": "required", "importance_evidence": [_whole("b000003")],
                "polarity": "positive", "polarity_evidence": None,
                "proficiency": None, "proficiency_evidence": None,
                "condition_ids": [], "fact_ids": [], "unresolved": [],
            },
            {
                "id": "s3", "kind": "employment_constraint", "subject": "candidate",
                "topic": "Travel", "evidence": [_whole("b000004")],
                "importance": "required", "importance_evidence": [_whole("b000004")],
                "polarity": "positive", "polarity_evidence": None,
                "proficiency": None, "proficiency_evidence": None,
                "condition_ids": [], "fact_ids": [], "unresolved": [],
            },
        ],
        "relations": {"groups": [], "conditions": [], "example_sets": []},
        "facts": {
            "presence": {
                "experience": {"state": "stated",
                               "evidence": [_ref("b000002", "8 years")]},
                "compensation": {"state": "none_found", "evidence": None},
                "quantities": {"state": "none_found", "evidence": None},
                "dates": {"state": "none_found", "evidence": None},
            },
            "entries": [{
                "id": "f1", "family": "experience", "statement_ids": ["s1"],
                "condition_ids": [], "scope": {"kind": "overall", "evidence": None},
                "date_kind": None, "component": None,
                "evidence": {
                    "value": [_ref("b000002", "8 years")],
                    "comparison": [_ref("b000002", "A minimum of")],
                    "unit": None, "currency": None, "component": None,
                    "applicability": None,
                },
            }],
        },
        "mentions": [{
            "id": "m1", "surface": "Salesforce", "evidence": _ref("b000003", "Salesforce"),
            "statement_ids": ["s2"], "role": "direct",
        }],
        "areas": [{"id": "a1", "name": "Requirements", "kind": "credential",
                   "statement_ids": ["s1", "s2", "s3"], "evidence": None}],
        "block_accounting": [
            {"block_id": "b000001", "disposition": "context", "ref_ids": [],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s1", "f1"],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000003", "disposition": "statements", "ref_ids": ["s2"],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000004", "disposition": "statements", "ref_ids": ["s3"],
             "exclusion_reason": None, "evidence": None},
        ],
    }


def _record() -> dict[str, Any]:
    return assemble(_base_emit(), REPAIR_MD, document_hash=REPAIR_DOC_HASH,
                    observed_model="gpt-5.6-luna", at=AT)


@pytest.fixture
def record() -> dict[str, Any]:
    return _record()


def _find(record: dict[str, Any], collection: str, object_id: str) -> dict[str, Any]:
    node: Any = record
    for key in collection.split("."):
        node = node[key]
    return next(item for item in node if item["id"] == object_id)


def _op(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "op": "replace", "kind": "statement", "target_id": "s2", "object": None,
        "old_object_hash": None, "finding_id": "f1",
        "evidence": [_ref("b000003", "preferred")],
        "reason": "the source says preferred, the candidate says required",
    }
    base.update(over)
    return base


def _emit(record: dict[str, Any], *operations: dict[str, Any]) -> dict[str, Any]:
    return {"base_candidate_hash": record["extraction"]["candidate_hash"],
            "operations": list(operations)}


def _apply(record: dict[str, Any], *operations: dict[str, Any]) -> dict[str, Any]:
    return apply(_emit(record, *operations), record, REPAIR_MD,
                 record["extraction"]["candidate_hash"])


def _preferred_s2(record: dict[str, Any]) -> dict[str, Any]:
    """The repaired statement: importance corrected, id and support kept."""
    return _op(object={
        "id": "s2", "kind": "qualification", "subject": "candidate",
        "topic": "Salesforce certification", "evidence": [_whole("b000003")],
        "importance": "preferred", "importance_evidence": [_ref("b000003", "preferred")],
        "polarity": "positive", "polarity_evidence": None,
        "proficiency": None, "proficiency_evidence": None,
        "condition_ids": [], "fact_ids": [], "unresolved": [],
    }, old_object_hash=object_hash(_find(record, "statements", "s2")))


FINDINGS = [{
    "code": "importance", "severity": "blocking", "dimension": "semantics",
    "targets": ["s2"],
    "evidence": {"block_id": "b000003", "text": "preferred", "occurrence": 0,
                 "span": [58, 67]},
    "explanation": "the source says preferred; the candidate says required",
}]


# --- template pins ----------------------------------------------------------


def test_version_and_template_sha() -> None:
    assert REPAIR_VERSION == "semantic-repair/v1"
    assert template_sha() == sha256_hex(TEMPLATE.encode("utf-8"))


def test_spec_repair_text_is_verbatim() -> None:
    for sentence in (
        "Propose changes to address the cited findings using the original source.",
        "Return only the allowed typed repair operations for the supplied base hash.",
        "Preserve unaffected supported statements and their IDs.",
        "Every addition, replacement, or removal needs source evidence and a reason.",
        "Difficulty parsing is not a reason to remove supported information.",
        "Keep ambiguity or unsupported numeric grammar visible as unresolved.",
        "Do not change document identity, versions, provenance, or quality assessments.",
        "Do not silently change unrelated statements or remove their support links.",
    ):
        assert sentence in TEMPLATE, sentence


def test_template_explains_every_op_and_object_kind() -> None:
    for word in (*OPS, *KINDS):
        assert word in TEMPLATE, word


# --- render -----------------------------------------------------------------


def _section(prompt: str, opener: str, closer: str) -> str:
    return prompt.split(f"<<<{opener}\n", 1)[1].split(f"\n{closer}>>>", 1)[0]


def test_render_lists_blocks_the_candidate_and_the_findings(record: dict[str, Any]) -> None:
    out = render(REPAIR_MD, record["extraction"]["candidate_hash"], record, FINDINGS)
    blocks_section = _section(out, "SOURCE BLOCKS", "SOURCE BLOCKS")
    for block in annotate(REPAIR_MD):
        assert f"{block.id}: {block.text}" in blocks_section
    assert record["extraction"]["candidate_hash"] in out
    findings = json.loads(_section(out, "FINDINGS JSON", "FINDINGS JSON"))
    assert [f["id"] for f in findings] == ["f1"]
    assert findings[0]["code"] == "importance"
    for placeholder in ("{candidate_hash}", "{source_blocks}", "{candidate_json}",
                        "{findings_json}"):
        assert placeholder not in out


def test_render_sends_the_extraction_without_code_owned_bookkeeping(
    record: dict[str, Any],
) -> None:
    """Spec §4: source, candidate hash, candidate, findings. Not our quality
    verdict (the repair is graded by the re-audit, not by its own input) and
    not the extractor's identity."""
    out = render(REPAIR_MD, record["extraction"]["candidate_hash"], record, FINDINGS)
    candidate = json.loads(_section(out, "CANDIDATE JSON", "CANDIDATE JSON"))
    assert "quality" not in candidate and "extraction" not in candidate
    assert "gpt-5.6-luna" not in out


def test_render_shows_the_old_object_hash_of_every_addressable_object(
    record: dict[str, Any],
) -> None:
    """A model cannot compute sha256: every hash an operation echoes back has
    to be visible beside the object it guards."""
    out = render(REPAIR_MD, record["extraction"]["candidate_hash"], record, FINDINGS)
    candidate = json.loads(_section(out, "CANDIDATE JSON", "CANDIDATE JSON"))
    shown = candidate["statements"][1]
    assert shown["id"] == "s2"
    assert shown["object_hash"] == object_hash(_find(record, "statements", "s2"))
    assert candidate["source_assessment"]["object_hash"] == object_hash(
        record["source_assessment"])
    assert candidate["facts"]["presence"]["experience"]["object_hash"] == object_hash(
        record["facts"]["presence"]["experience"])
    assert candidate["block_accounting"][0]["object_hash"] == object_hash(
        record["block_accounting"][0])
    for collection in ("statements", "mentions", "areas"):
        for shown in candidate[collection]:
            assert len(shown["object_hash"]) == 64


def test_render_does_not_mutate_the_record_and_is_deterministic(
    record: dict[str, Any],
) -> None:
    before = copy.deepcopy(record)
    h = record["extraction"]["candidate_hash"]
    assert render(REPAIR_MD, h, record, FINDINGS) == render(
        REPAIR_MD, h, copy.deepcopy(record), copy.deepcopy(FINDINGS))
    assert record == before


def test_render_never_rescans_substituted_text() -> None:
    """A document line may contain a literal placeholder token (source is
    untrusted): substitution happens once, by position."""
    md = "Body {candidate_json} line.\n"
    record = {"statements": [{"id": "s_marker"}], "quality": {}, "extraction": {}}
    out = render(md, "abc", record, [])
    assert out.count("s_marker") == 1
    assert "{candidate_json}" in out  # the literal token stays inert


# --- emit schema ------------------------------------------------------------


def _nodes(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _nodes(value)


def test_emit_schema_is_a_valid_draft_2020_12_schema() -> None:
    jsonschema.Draft202012Validator.check_schema(emit_schema())


def test_emit_schema_shape() -> None:
    schema = emit_schema()
    assert sorted(schema["required"]) == ["base_candidate_hash", "operations"]
    operation = schema["properties"]["operations"]["items"]
    assert sorted(operation["required"]) == [
        "evidence", "finding_id", "kind", "object", "old_object_hash", "op",
        "reason", "target_id",
    ]
    assert operation["properties"]["op"]["enum"] == list(OPS)
    assert operation["properties"]["kind"]["enum"] == list(KINDS)
    for node in _nodes(schema):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, node


def test_every_enum_and_union_member_carries_an_explicit_type() -> None:
    """codex/OpenAI strict mode rejects a bare const/enum node and cannot
    resolve `$ref` in an output schema (the emit_guard.py lesson)."""
    schema = emit_schema()
    for node in _nodes(schema):
        if "enum" in node or "const" in node:
            assert "type" in node, node
        for key in ("anyOf", "oneOf", "allOf"):
            for member in node.get(key, []):
                assert "type" in member, member
    assert "$ref" not in json.dumps(schema)


def test_strict_transform_keeps_the_schema_expressible() -> None:
    strict = json.dumps(strict_schema(emit_schema()))
    assert "serialized as a string" not in strict
    assert "old_object_hash" in strict


def test_emit_schema_admits_a_real_patch_and_closes_the_kind_enum(
    record: dict[str, Any],
) -> None:
    validator = jsonschema.Draft202012Validator(emit_schema())
    ok = _emit(record, _preferred_s2(record))
    assert validator.is_valid(ok), list(validator.iter_errors(ok))
    assert not validator.is_valid(_emit(record, _op(kind="quality", object=None)))
    assert not validator.is_valid(_emit(record, _op(op="rewrite")))


# --- apply: the base contract -----------------------------------------------


def test_apply_rejects_a_stale_base(record: dict[str, Any]) -> None:
    emit = _emit(record, _preferred_s2(record))
    emit["base_candidate_hash"] = OTHER_HASH
    with pytest.raises(RepairJudgeError) as excinfo:
        apply(emit, record, REPAIR_MD, record["extraction"]["candidate_hash"])
    assert any("base_candidate_hash" in e for e in excinfo.value.errors)


def test_apply_rejects_a_record_that_is_not_the_named_candidate(
    record: dict[str, Any],
) -> None:
    with pytest.raises(RepairJudgeError):
        apply(_emit(record, _preferred_s2(record)), record, REPAIR_MD, OTHER_HASH)


def test_apply_rejects_a_record_the_rebuild_cannot_round_trip(
    record: dict[str, Any],
) -> None:
    """A record missing a top-level collection would crash the rebuild; it is
    judged instead, so the phase archives a repair failure like any other."""
    maimed = {k: v for k, v in record.items() if k != "areas"}
    with pytest.raises(RepairJudgeError) as excinfo:
        apply(_emit(record, _preferred_s2(record)), maimed, REPAIR_MD,
              record["extraction"]["candidate_hash"])
    assert any("areas" in e for e in excinfo.value.errors)


def test_apply_rejects_a_structurally_broken_emit(record: dict[str, Any]) -> None:
    candidate_hash = record["extraction"]["candidate_hash"]
    for emit in ([], {"base_candidate_hash": candidate_hash},
                 {"base_candidate_hash": candidate_hash, "operations": "all of them"},
                 {"base_candidate_hash": candidate_hash, "operations": []},
                 {"base_candidate_hash": candidate_hash, "operations": ["fix it"]}):
        with pytest.raises(RepairJudgeError):
            apply(emit, record, REPAIR_MD, candidate_hash)  # type: ignore[arg-type]


def test_apply_reports_every_defect_at_once(record: dict[str, Any]) -> None:
    with pytest.raises(RepairJudgeError) as excinfo:
        _apply(record,
               _op(op="scribble"),
               _op(kind="extraction"),
               _op(target_id="s9", object={}, old_object_hash=OTHER_HASH,
                   finding_id="", reason="", evidence=[]))
    assert len(excinfo.value.errors) >= 5


# --- apply: per-operation validity ------------------------------------------


def test_apply_rejects_an_unknown_op_or_kind(record: dict[str, Any]) -> None:
    for bad in (_op(op="patch"), _op(kind="document")):
        with pytest.raises(RepairJudgeError):
            _apply(record, bad)


def test_apply_rejects_adding_or_removing_a_replace_only_object(
    record: dict[str, Any],
) -> None:
    """Presence objects, the source assessment and accounting entries are
    fields of the record, not members of a collection: there is nothing to
    add and nothing to take away."""
    for kind, target in (("presence", "experience"), ("source_assessment", None),
                         ("accounting_entry", "b000002")):
        for op in ("add", "remove"):
            with pytest.raises(RepairJudgeError):
                _apply(record, _op(op=op, kind=kind, target_id=target,
                                   old_object_hash=OTHER_HASH))


def test_apply_requires_a_finding_id_a_reason_and_evidence(
    record: dict[str, Any],
) -> None:
    good = _preferred_s2(record)
    for over in ({"finding_id": ""}, {"finding_id": None}, {"reason": "  "},
                 {"evidence": []}, {"evidence": None},
                 {"evidence": [_ref("b000003", "unwritten words")]}):
        with pytest.raises(RepairJudgeError):
            _apply(record, {**good, **over})


def test_apply_rejects_a_malformed_evidence_reference(record: dict[str, Any]) -> None:
    """An operation's citation is model-sent like every object it carries, so a
    reference of the wrong shape is a judged defect — never a `TypeError` out of
    the binder, which the phase around `apply` has no reason to catch."""
    good = _preferred_s2(record)
    for ref in (
        {"block_id": "b000003", "text": 5, "occurrence": 0},
        {"block_id": "b000003", "text": ["preferred"], "occurrence": 0},
        {"block_id": "b000003", "text": "", "occurrence": 0},
        {"block_id": "b000003", "text": "preferred", "occurrence": "0"},
        {"block_id": "b000003", "text": "preferred", "occurrence": True},
        {"block_id": "b000003", "text": "preferred", "occurrence": -1},
        {"block_id": "b000003", "text": "preferred"},
        {"block_id": "b000003", "text": "preferred", "occurrence": 0, "note": "here"},
        "b000003: preferred",
    ):
        with pytest.raises(RepairJudgeError) as excinfo:
            _apply(record, {**good, "evidence": [ref]})
        assert any("evidence[0]" in e for e in excinfo.value.errors), ref


def test_apply_accepts_a_citation_that_echoes_a_bound_span(
    record: dict[str, Any],
) -> None:
    """The candidate the repairer is shown carries bound spans: copying one into
    an operation's citation is echoing what code owns, not proposing it, and is
    stripped rather than rejected (the rule `_to_emit` already applies to every
    object an operation carries)."""
    echoed = copy.deepcopy(_find(record, "statements", "s2")["evidence"][0])
    assert "span" in echoed
    repaired = _apply(record, {**_preferred_s2(record), "evidence": [echoed]})
    assert _find(repaired, "statements", "s2")["importance"] == "preferred"


def test_apply_requires_evidence_on_a_removal(record: dict[str, Any]) -> None:
    """Spec §4: unexplained deletions reject the patch."""
    removal = _op(op="remove", kind="mention", target_id="m1", object=None,
                  old_object_hash=object_hash(_find(record, "mentions", "m1")))
    assert _apply(record, removal)["mentions"] == []
    with pytest.raises(RepairJudgeError):
        _apply(record, {**removal, "evidence": []})


def test_apply_requires_the_old_object_hash_to_match(record: dict[str, Any]) -> None:
    good = _preferred_s2(record)
    for over in ({"old_object_hash": None}, {"old_object_hash": OTHER_HASH},
                 {"old_object_hash": object_hash(_find(record, "statements", "s1"))}):
        with pytest.raises(RepairJudgeError):
            _apply(record, {**good, **over})


def test_apply_rejects_an_add_that_carries_an_old_object_hash(
    record: dict[str, Any],
) -> None:
    with pytest.raises(RepairJudgeError):
        _apply(record, _add_mention(old_object_hash=OTHER_HASH))


def _add_mention(**over: Any) -> dict[str, Any]:
    added: dict[str, Any] = {
        "op": "add", "kind": "mention", "target_id": None,
        "object": {"id": "m2", "surface": "sales", "evidence": _ref("b000002", "sales"),
                   "statement_ids": ["s1"], "role": "direct"},
    }
    added.update(over)
    return _op(**added)


def test_apply_rejects_an_add_whose_id_is_already_taken(record: dict[str, Any]) -> None:
    with pytest.raises(RepairJudgeError):
        _apply(record, _add_mention(object={
            "id": "m1", "surface": "sales", "evidence": _ref("b000002", "sales"),
            "statement_ids": ["s1"], "role": "direct"}))


def test_apply_rejects_an_unknown_target(record: dict[str, Any]) -> None:
    for over in ({"target_id": "s9"}, {"target_id": None}, {"target_id": 7}):
        with pytest.raises(RepairJudgeError):
            _apply(record, {**_preferred_s2(record), **over})


def test_apply_rejects_a_replace_that_renames_its_target(record: dict[str, Any]) -> None:
    """A rename is a remove plus an add: it orphans every support link that
    still points at the old id, which is exactly what §4 forbids silently."""
    op = _preferred_s2(record)
    op["object"] = {**op["object"], "id": "s2_fixed"}
    with pytest.raises(RepairJudgeError):
        _apply(record, op)


def test_apply_rejects_conflicting_operations_on_one_object(
    record: dict[str, Any],
) -> None:
    first = _preferred_s2(record)
    removal = _op(op="remove", kind="statement", target_id="s2", object=None,
                  old_object_hash=object_hash(_find(record, "statements", "s2")))
    with pytest.raises(RepairJudgeError) as excinfo:
        _apply(record, first, removal)
    assert any("conflict" in e for e in excinfo.value.errors)


def test_apply_rejects_an_object_that_touches_an_immutable_field(
    record: dict[str, Any],
) -> None:
    for immutable in ("document", "extraction", "quality"):
        op = _preferred_s2(record)
        op["object"] = {**op["object"], immutable: {"anything": True}}
        with pytest.raises(RepairJudgeError) as excinfo:
            _apply(record, op)
        assert any("immutable" in e for e in excinfo.value.errors)


def test_apply_holds_a_repaired_object_to_the_emit_contract(
    record: dict[str, Any],
) -> None:
    """No duplicated grammar: the object an operation carries is exactly the
    emit-schema-2 object it replaces, validated against the packaged schema."""
    op = _preferred_s2(record)
    for broken in ({**op["object"], "importance": "mandatory"},
                   {k: v for k, v in op["object"].items() if k != "polarity"},
                   {**op["object"], "note": "extra"},
                   {**op["object"], "evidence": []}):
        with pytest.raises(RepairJudgeError):
            _apply(record, {**op, "object": broken})


def test_apply_strips_the_code_owned_fields_an_object_echoes_back(
    record: dict[str, Any],
) -> None:
    """The prompt shows spans and an object_hash inside every object; a model
    copying one back is echoing what code owns, not proposing it. Stripped and
    recomputed — never trusted, and never a rejection either."""
    op = _preferred_s2(record)
    echoed = _find(record, "statements", "s2")
    op["object"] = {
        **op["object"],
        "evidence": copy.deepcopy(echoed["evidence"]),  # carries bound spans
        "object_hash": op["old_object_hash"],
    }
    repaired = _apply(record, op)
    fixed = _find(repaired, "statements", "s2")
    assert "object_hash" not in fixed
    assert fixed["evidence"] == echoed["evidence"]
    assert verify(repaired, REPAIR_MD).status == "pass"


def test_apply_rejects_control_characters(record: dict[str, Any]) -> None:
    """validator/17, reused: a NUL in an emitted string crosses every check to
    die at the jsonb boundary, so assembly refuses it — including here."""
    op = _preferred_s2(record)
    op["object"] = {**op["object"], "topic": "Salesforce\x00certification"}
    with pytest.raises(RepairJudgeError) as excinfo:
        _apply(record, op)
    assert any("control character" in e for e in excinfo.value.errors)


def test_apply_rejects_an_object_whose_evidence_does_not_bind(
    record: dict[str, Any],
) -> None:
    op = _preferred_s2(record)
    op["object"] = {**op["object"], "evidence": [_ref("b000003", "words never written")]}
    with pytest.raises(RepairJudgeError):
        _apply(record, op)


def test_a_failed_repair_mutates_nothing(record: dict[str, Any]) -> None:
    before = copy.deepcopy(record)
    ops = [_preferred_s2(record), _op(op="remove", kind="statement", target_id="s2",
                                      object=None, old_object_hash=OTHER_HASH)]
    with pytest.raises(RepairJudgeError):
        _apply(record, *ops)
    assert record == before


# --- apply: the happy path --------------------------------------------------


def test_a_replace_rebinds_rehashes_and_links_to_its_parent(
    record: dict[str, Any],
) -> None:
    base_hash = record["extraction"]["candidate_hash"]
    repaired = _apply(record, _preferred_s2(record))
    assert repaired["extraction"]["parent_candidate_hash"] == base_hash
    assert repaired["extraction"]["candidate_hash"] not in ("", base_hash)
    fixed = _find(repaired, "statements", "s2")
    assert fixed["importance"] == "preferred"
    bound = fixed["importance_evidence"][0]
    assert bound["text"] == "preferred"
    assert REPAIR_MD[bound["span"][0]:bound["span"][1]] == "preferred"
    assert verify(repaired, REPAIR_MD).status == "pass"


def test_a_repair_leaves_every_unaffected_object_byte_identical(
    record: dict[str, Any],
) -> None:
    """Spec §4: "Do not silently change unrelated statements". The rebuild
    round-trips the whole record through assembly, so this is the pin that
    says the round trip is lossless."""
    repaired = _apply(record, _preferred_s2(record))
    for statement_id in ("s1", "s3"):
        assert _find(repaired, "statements", statement_id) == _find(
            record, "statements", statement_id)
    for key in ("relations", "facts", "mentions", "areas", "block_accounting",
                "source_assessment", "document", "quality"):
        assert repaired[key] == record[key], key


def test_a_repair_keeps_the_extraction_provenance_and_versions(
    record: dict[str, Any],
) -> None:
    repaired = _apply(record, _preferred_s2(record))
    for field in ("model", "prompt_version", "schema_version", "validator_version",
                  "rules_version", "at"):
        assert repaired["extraction"][field] == record["extraction"][field], field


def test_an_add_binds_its_evidence_and_derives_its_code_owned_fields(
    record: dict[str, Any],
) -> None:
    repaired = _apply(record, _add_mention())
    added = _find(repaired, "mentions", "m2")
    assert added["normalized_key"] == "sales"  # code-owned, never emitted
    assert REPAIR_MD[added["evidence"]["span"][0]:added["evidence"]["span"][1]] == "sales"
    assert verify(repaired, REPAIR_MD).status == "pass"


def test_a_repair_re_derives_the_facts_it_touches(record: dict[str, Any]) -> None:
    """Code owns every derived number: dropping the comparison citation moves
    the entry from an inclusive floor to a bare stated quantity."""
    assert _find(record, "facts.entries", "f1")["derived"]["quantity"]["comparison"] == "gte"
    entry = {k: v for k, v in _base_emit()["facts"]["entries"][0].items()}
    entry["evidence"] = {**entry["evidence"], "comparison": None}
    repaired = _apply(record, _op(
        kind="fact_entry", target_id="f1", object=entry,
        old_object_hash=object_hash(_find(record, "facts.entries", "f1"))))
    assert _find(repaired, "facts.entries", "f1")["derived"]["quantity"]["comparison"] == (
        "unstated")
    assert verify(repaired, REPAIR_MD).status == "pass"


def test_a_multi_operation_patch_reconciles_presence(record: dict[str, Any]) -> None:
    """Removing the only experience entry downgrades the family's presence
    state through assembly's own reconciliation (parsing-rules/4), and the
    support links that pointed at it are cleaned in the same patch."""
    stripped = {**_base_emit()["statements"][0], "fact_ids": []}
    repaired = _apply(
        record,
        _op(op="remove", kind="fact_entry", target_id="f1", object=None,
            old_object_hash=object_hash(_find(record, "facts.entries", "f1"))),
        _op(kind="statement", target_id="s1", object=stripped,
            old_object_hash=object_hash(_find(record, "statements", "s1"))),
        _op(kind="presence", target_id="experience",
            object={"state": "stated", "evidence": [_ref("b000002", "8 years")]},
            old_object_hash=object_hash(record["facts"]["presence"]["experience"])),
        _op(kind="accounting_entry", target_id="b000002",
            object={"block_id": "b000002", "disposition": "statements",
                    "ref_ids": ["s1"], "exclusion_reason": None, "evidence": None},
            old_object_hash=object_hash(record["block_accounting"][1])),
    )
    assert repaired["facts"]["entries"] == []
    # the emitted "stated" cannot survive with no entries: code reconciles it
    assert repaired["facts"]["presence"]["experience"]["state"] == "unresolved"
    assert verify(repaired, REPAIR_MD).status == "pass"


def test_a_source_assessment_replace_moves_the_code_owned_quality(
    record: dict[str, Any],
) -> None:
    """Usability is the model's cited call; the `source` quality dimension is
    code's reading of it, so a repaired assessment carries its dimension."""
    assert record["quality"]["source"] == "usable"
    repaired = _apply(record, _op(
        kind="source_assessment", target_id=None,
        object={"usability": "partial", "evidence": [_whole("b000001")],
                "note": "requirements only"},
        old_object_hash=object_hash(record["source_assessment"])))
    assert repaired["source_assessment"]["usability"] == "partial"
    assert repaired["quality"]["source"] == "partial"
    assert repaired["quality"]["search_eligible"] is False


def test_two_repairs_of_the_same_base_are_deterministic(record: dict[str, Any]) -> None:
    assert _apply(record, _preferred_s2(record)) == _apply(record, _preferred_s2(record))


def test_a_repaired_candidate_can_be_repaired_again(record: dict[str, Any]) -> None:
    """The chain is the runner's business (spec §5 permits one round); the
    contract itself must still compose — a repaired record is a record."""
    once = _apply(record, _preferred_s2(record))
    twice = apply(
        _emit(once, _op(op="remove", kind="mention", target_id="m1", object=None,
                        old_object_hash=object_hash(_find(once, "mentions", "m1")))),
        once, REPAIR_MD, once["extraction"]["candidate_hash"])
    assert twice["extraction"]["parent_candidate_hash"] == once["extraction"][
        "candidate_hash"]
    assert twice["mentions"] == []


# --- purity -----------------------------------------------------------------


def test_the_module_imports_nothing_with_side_effects() -> None:
    tree = ast.parse(inspect.getsource(repair))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported <= {
        "__future__", "copy", "dataclasses", "functools", "json", "re", "typing",
        "jsonschema", "jobhunter.hashing", "jobhunter.l2.schemas",
        "jobhunter.l2.v2.assemble", "jobhunter.l2.v2.source",
    }, imported
