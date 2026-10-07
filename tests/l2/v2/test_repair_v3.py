"""`semantic-repair/v3` — typed repair over schema-4 records (contract v4 §4).

v2's policy stands: one round, typed operations, old-object-hash guards, a
failed repair mutates nothing, the rebuild goes through assembly. v3 teaches
the schema-4 shapes: a mention's `type`, the three authorization presence
families with their polarity, and `relations.tracks` as an object of its own
(added when the candidate has none, replaced or removed when it has one). The
code-owned `authorization` block is re-derived by assembly, never sent.

v2's bytes and schemas stay exactly as bundle v2 ships them.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import jsonschema
import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.schemas import strict_schema
from jobhunter.l2.v2 import repair
from jobhunter.l2.v2.repair import RepairJudgeError, apply, object_hash
from jobhunter.l2.v2.verify import verify
from tests.l2.v2.conftest import (
    FIGMA_MD,
    LYFT_MD,
    S3_MD,
    VISA_MD,
    WORK_AUTH_MD,
    assemble4,
    make_figma_emit,
    make_lyft_emit,
    make_s3_record,
    make_visa_emit,
    make_work_auth_emit,
    quote,
    s4_presence,
    whole,
)

#: semantic-repair/v2 as bundle v2 ships it, before v3 landed
V2_TEMPLATE_SHA = "7e03e7d0ae763a60f5b3ef6ae024f17735c54b199fac5b9fc6899d5f15537d7d"
V2_SCHEMA_SHA = {
    "2": "d385a91d04c2d0457ea9239f0c670734e5be609b2038a21ff6b16d9e2ca3f93c",
    "3": "73698fb7732c0656944f123f7bc4ed179aa911f4ac331ea3e7c6a58b5ea94c12",
}


def _sha(schema: dict[str, Any]) -> str:
    return sha256_hex(json.dumps(schema, sort_keys=True).encode("utf-8"))


def _flat(text: str) -> str:
    return " ".join(text.split())


def _op(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "op": "replace", "kind": "statement", "target_id": None, "object": None,
        "old_object_hash": None, "finding_id": "f1",
        "evidence": [whole("b000002")], "reason": "the source says so",
    }
    base.update(over)
    return base


def _apply(record: dict[str, Any], markdown: str, *ops: dict[str, Any]) -> dict[str, Any]:
    candidate = record["extraction"]["candidate_hash"]
    return apply({"base_candidate_hash": candidate, "operations": list(ops)},
                 record, markdown, candidate)


def _clean(record: dict[str, Any], markdown: str) -> None:
    report = verify(record, markdown, schema_version="4")
    assert report.status == "pass", [(f.check, f.code, f.path) for f in report.findings]


def _emit_tracks(record: dict[str, Any]) -> dict[str, Any]:
    """The record's tracks back in emit shape (bound spans dropped)."""
    return repair._record_to_emit(record["relations"]["tracks"])  # type: ignore[no-any-return]


# --- identity: v2 untouched, v3 beside it -------------------------------------


def test_v2_is_unchanged() -> None:
    assert repair.REPAIR_VERSION == "semantic-repair/v2"
    assert repair.template_sha() == V2_TEMPLATE_SHA
    for version, sha in V2_SCHEMA_SHA.items():
        assert _sha(repair.emit_schema(version)) == sha


def test_v3_identity_and_template() -> None:
    assert repair.REPAIR_VERSION_V3 == "semantic-repair/v3"
    assert repair.template_sha_v3() == sha256_hex(repair.TEMPLATE_V3.encode("utf-8")) == (
        "99c5ab7db4d1816b7aad3954b2707a7715871e0b27409b3a9c153a7217d03234")
    flat = _flat(repair.TEMPLATE_V3)
    for part in (repair._REPAIRER, repair._EMIT_FORMAT_NOTE):
        assert _flat(part) in flat  # v2's contract text, whole
    assert "sponsorship, citizenship or work_authorization" in flat
    assert '"type"' in flat and "tracks" in flat
    assert 'never send the "authorization" block' in flat


def _nodes(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _nodes(value)


def test_v3_schema_4_advertises_the_new_shapes() -> None:
    schema = repair.emit_schema_v3("4")
    assert "semantic-repair/v3" in schema["title"]
    jsonschema.Draft202012Validator.check_schema(schema)
    op = schema["properties"]["operations"]["items"]
    assert "tracks" in op["properties"]["kind"]["enum"]
    titles = json.dumps(op["properties"]["object"])
    assert "candidate_choice" in titles  # the tracks object
    assert "work-authorization requirement" in titles  # its presence family
    for node in _nodes(schema):
        assert "$ref" not in node
        if "enum" in node:
            assert "type" in node, node
    strict = strict_schema(schema)
    jsonschema.Draft202012Validator.check_schema(strict)


def test_v3_schema_3_has_no_tracks_kind() -> None:
    op = repair.emit_schema_v3("3")["properties"]["operations"]["items"]
    assert "tracks" not in op["properties"]["kind"]["enum"]


def test_render_v3_stamps_the_schema_4_objects() -> None:
    record = assemble4(make_figma_emit(), FIGMA_MD)
    prompt = repair.render_v3(FIGMA_MD, record["extraction"]["candidate_hash"], record, [])
    assert prompt.startswith(repair.TEMPLATE_V3.split("{candidate_hash}", 1)[0])
    body = prompt.split("<<<CANDIDATE JSON\n", 1)[1].split("\nCANDIDATE JSON>>>", 1)[0]
    shown = json.loads(body)
    tracks = shown["relations"]["tracks"]
    assert tracks["object_hash"] == object_hash(record["relations"]["tracks"])
    sponsorship = shown["facts"]["presence"]["sponsorship"]
    assert sponsorship["object_hash"] == object_hash(record["facts"]["presence"]["sponsorship"])


# --- presence: the three authorization families --------------------------------


def test_a_missed_sponsorship_sentence_is_repaired_into_presence() -> None:
    emit = make_visa_emit()
    emit["facts"]["presence"]["sponsorship"] = s4_presence()
    base = assemble4(emit, VISA_MD)
    assert base["authorization"]["sponsorship"] == "undeclared"
    fixed = s4_presence("stated", [whole("b000004")], "negative",
                        [quote("b000004", "will not sponsor")])
    repaired = _apply(base, VISA_MD, _op(
        kind="presence", target_id="sponsorship", object=fixed,
        old_object_hash=object_hash(base["facts"]["presence"]["sponsorship"]),
        evidence=[whole("b000004")]))
    assert repaired["authorization"]["sponsorship"] == "no"
    assert repaired["extraction"]["schema_version"] == "4"
    assert repaired["extraction"]["parent_candidate_hash"] == \
        base["extraction"]["candidate_hash"]
    _clean(repaired, VISA_MD)


def test_work_authorization_takes_no_polarity() -> None:
    base = assemble4(make_work_auth_emit(), WORK_AUTH_MD)
    bad = s4_presence("stated", [whole("b000002")], "positive")
    with pytest.raises(RepairJudgeError):
        _apply(base, WORK_AUTH_MD, _op(
            kind="presence", target_id="work_authorization", object=bad,
            old_object_hash=object_hash(base["facts"]["presence"]["work_authorization"])))


def test_an_authorization_family_is_not_a_schema_3_target() -> None:
    record = make_s3_record()
    with pytest.raises(RepairJudgeError, match="no such fact-presence family"):
        _apply(record, S3_MD, _op(
            kind="presence", target_id="sponsorship", object=s4_presence(),
            old_object_hash="a" * 64, evidence=[whole("b000001")]))


# --- mention type ----------------------------------------------------------------


def test_a_mistyped_mention_is_repaired() -> None:
    base = assemble4(make_lyft_emit(toronto_type="skill"), LYFT_MD)
    index = next(i for i, m in enumerate(base["mentions"]) if m["id"] == "m_toronto")
    fixed = repair._record_to_emit(copy.deepcopy(base["mentions"][index]))
    fixed["type"] = "location"
    repaired = _apply(base, LYFT_MD, _op(
        kind="mention", target_id="m_toronto", object=fixed,
        old_object_hash=object_hash(base["mentions"][index]),
        evidence=[whole("b000003")]))
    types = {m["id"]: m["type"] for m in repaired["mentions"]}
    assert types == {"m_cs": "field_of_study", "m_toronto": "location"}
    _clean(repaired, LYFT_MD)


def test_a_mention_without_a_type_is_refused_under_schema_4() -> None:
    base = assemble4(make_lyft_emit(), LYFT_MD)
    index = next(i for i, m in enumerate(base["mentions"]) if m["id"] == "m_toronto")
    fixed = repair._record_to_emit(copy.deepcopy(base["mentions"][index]))
    del fixed["type"]
    with pytest.raises(RepairJudgeError, match="type"):
        _apply(base, LYFT_MD, _op(
            kind="mention", target_id="m_toronto", object=fixed,
            old_object_hash=object_hash(base["mentions"][index])))


# --- tracks ------------------------------------------------------------------------


def _flattened_figma() -> dict[str, Any]:
    emit = make_figma_emit()
    emit["relations"]["tracks"] = None
    return assemble4(emit, FIGMA_MD)


def test_a_flattened_track_list_is_repaired_by_adding_tracks() -> None:
    base = _flattened_figma()
    tracks = make_figma_emit()["relations"]["tracks"]
    repaired = _apply(base, FIGMA_MD, _op(
        op="add", kind="tracks", object=tracks, evidence=[whole("b000002")]))
    items = repaired["relations"]["tracks"]["items"]
    assert [item["id"] for item in items] == ["t_product", "t_backend", "t_security", "t_open"]
    assert items[-1]["open"] is True
    assert items[0]["name_evidence"][0]["span"]  # bound by assembly
    _clean(repaired, FIGMA_MD)


def test_tracks_are_replaced_and_removed_under_their_hash() -> None:
    base = assemble4(make_figma_emit(), FIGMA_MD)
    fixed = _emit_tracks(base)
    fixed["selection"] = "unstated"
    fixed["selection_evidence"] = None
    old = object_hash(base["relations"]["tracks"])
    replaced = _apply(base, FIGMA_MD, _op(
        kind="tracks", object=fixed, old_object_hash=old, evidence=[whole("b000002")]))
    assert replaced["relations"]["tracks"]["selection"] == "unstated"
    _clean(replaced, FIGMA_MD)
    removed = _apply(base, FIGMA_MD, _op(
        op="remove", kind="tracks", old_object_hash=old, evidence=[whole("b000002")]))
    assert removed["relations"]["tracks"] is None
    _clean(removed, FIGMA_MD)


def test_tracks_operations_are_guarded() -> None:
    present = assemble4(make_figma_emit(), FIGMA_MD)
    absent = _flattened_figma()
    tracks = make_figma_emit()["relations"]["tracks"]
    # an add over existing tracks would overwrite them unseen
    with pytest.raises(RepairJudgeError, match="already"):
        _apply(present, FIGMA_MD, _op(op="add", kind="tracks", object=tracks))
    # a replace or remove of tracks that do not exist
    with pytest.raises(RepairJudgeError, match="no tracks"):
        _apply(absent, FIGMA_MD, _op(kind="tracks", object=tracks,
                                     old_object_hash=object_hash(None)))
    # a stale hash
    with pytest.raises(RepairJudgeError, match="changed"):
        _apply(present, FIGMA_MD, _op(kind="tracks", object=tracks,
                                      old_object_hash="a" * 64))
    # tracks have no id to target
    with pytest.raises(RepairJudgeError, match="target_id"):
        _apply(present, FIGMA_MD, _op(kind="tracks", target_id="t_open", object=tracks,
                                      old_object_hash=object_hash(
                                          present["relations"]["tracks"])))


def test_tracks_are_not_a_schema_3_kind() -> None:
    record = make_s3_record()
    with pytest.raises(RepairJudgeError, match="tracks"):
        _apply(record, S3_MD, _op(op="add", kind="tracks",
                                  object=make_figma_emit()["relations"]["tracks"],
                                  evidence=[whole("b000001")]))


def test_a_failed_schema_4_repair_mutates_nothing() -> None:
    base = assemble4(make_figma_emit(), FIGMA_MD)
    before = copy.deepcopy(base)
    with pytest.raises(RepairJudgeError):
        _apply(base, FIGMA_MD, _op(kind="tracks", object={"selection": "nope"},
                                   old_object_hash=object_hash(base["relations"]["tracks"])))
    assert base == before
