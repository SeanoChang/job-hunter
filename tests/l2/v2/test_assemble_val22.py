"""Validator 22 (schema 4 only): a dangling link beside a live one is dropped.

Live run, bundle v3, 1,219 entry-level postings: 58 of 280 quarantined
documents failed every attempt on `references:unknown_reference at
mentions[i].statement_ids` — a mention linked a statement id the emit never
contained. A link is not evidence: the mention's own quote still binds, so
dropping the dangling id loses nothing quoted. Assembly drops it only when at
least one id in the same list still resolves, and records what it dropped in
`extraction.dropped_links`. A list with no live id keeps today's error.
Schema 2 and 3 records are untouched.
"""

from __future__ import annotations

from typing import Any

from jobhunter.l2.schemas import validate_record
from jobhunter.l2.v2 import facts
from jobhunter.l2.v2.assemble import assemble
from jobhunter.l2.v2.verify import verify
from tests.l2.v2.conftest import (
    AT,
    FIGMA_MD,
    S3_MENTIONS_DOC_HASH,
    S3_MENTIONS_MD,
    VISA_MD,
    assemble4,
    make_figma_emit,
    make_s3_emit_with_mentions,
    make_visa_emit,
)


def _unknown_refs(record: dict[str, Any], markdown: str, schema_version: str) -> list[Any]:
    report = verify(record, markdown, schema_version=schema_version)
    return [f for f in report.findings if f.code == "unknown_reference"]


def test_schema_4_seals_validator_22() -> None:
    assert facts.SCHEMA_4_VALIDATOR_VERSION == "23"
    assert facts.validator_version_for("4") == "23"
    assert facts.validator_version_for("3") == facts.VALIDATOR_VERSION == "20"
    assert facts.validator_version_for("2") == "20"


def test_a_dangling_link_beside_a_live_one_is_dropped_and_recorded() -> None:
    emit = make_visa_emit()
    emit["mentions"][1]["statement_ids"] = ["s_duty", "s25"]
    record = assemble4(emit, VISA_MD)
    assert validate_record(record, "4") == []
    assert record["mentions"][1]["statement_ids"] == ["s_duty"]
    assert record["extraction"]["dropped_links"] == [
        {"path": "mentions[1].statement_ids", "ref_id": "s25"}
    ]
    assert record["extraction"]["validator_version"] == "23"
    report = verify(record, VISA_MD, schema_version="4")
    assert report.status == "pass", [(f.code, f.path) for f in report.findings]
    assert report.validator_version == "23"


def test_a_clean_record_carries_no_dropped_links_key() -> None:
    """Records with nothing dropped are byte-shaped as under validator 21."""
    record = assemble4(make_visa_emit(), VISA_MD)
    assert "dropped_links" not in record["extraction"]


def test_a_mention_whose_only_link_dangles_still_fails() -> None:
    emit = make_visa_emit()
    emit["mentions"][1]["statement_ids"] = ["s25"]
    record = assemble4(emit, VISA_MD)
    assert record["mentions"][1]["statement_ids"] == ["s25"]
    assert "dropped_links" not in record["extraction"]
    found = _unknown_refs(record, VISA_MD, "4")
    assert [(f.path, f.detail["ref_id"]) for f in found] == [
        ("mentions[1].statement_ids", "s25")
    ]


def test_track_links_get_the_same_treatment() -> None:
    emit = make_figma_emit()
    backend = emit["relations"]["tracks"]["items"][1]
    backend["statement_ids"] = ["s_ghost", "s_backend"]
    backend["mention_ids"] = ["m_ds", "m_ghost", "m_tooling"]
    record = assemble4(emit, FIGMA_MD)
    assert validate_record(record, "4") == []
    item = record["relations"]["tracks"]["items"][1]
    assert item["statement_ids"] == ["s_backend"]
    assert item["mention_ids"] == ["m_ds", "m_tooling"]
    assert record["extraction"]["dropped_links"] == [
        {"path": "relations.tracks.items[1].statement_ids", "ref_id": "s_ghost"},
        {"path": "relations.tracks.items[1].mention_ids", "ref_id": "m_ghost"},
    ]
    assert verify(record, FIGMA_MD, schema_version="4").status == "pass"


def test_a_track_whose_only_link_dangles_still_fails() -> None:
    emit = make_figma_emit()
    emit["relations"]["tracks"]["items"][0]["statement_ids"] = ["s_ghost"]
    record = assemble4(emit, FIGMA_MD)
    found = _unknown_refs(record, FIGMA_MD, "4")
    assert [(f.path, f.detail["ref_id"]) for f in found] == [
        ("relations.tracks.items[0].statement_ids", "s_ghost")
    ]


def test_schema_3_keeps_the_dangling_link_and_the_error() -> None:
    emit = make_s3_emit_with_mentions()
    live = emit["mentions"][0]["statement_ids"][0]
    emit["mentions"][0]["statement_ids"] = [live, "s25"]
    record = assemble(emit, S3_MENTIONS_MD, document_hash=S3_MENTIONS_DOC_HASH,
                      observed_model="gpt-5.6-luna", at=AT, schema_version="3")
    assert record["mentions"][0]["statement_ids"] == [live, "s25"]
    assert "dropped_links" not in record["extraction"]
    assert record["extraction"]["validator_version"] == "20"
    found = _unknown_refs(record, S3_MENTIONS_MD, "3")
    assert [(f.path, f.detail["ref_id"]) for f in found] == [
        ("mentions[0].statement_ids", "s25")
    ]
