import json
from typing import Any

import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.schemas import validate_record
from jobhunter.l2.v2.assemble import (
    AssembleError,
    assemble,
    candidate_hash,
    character_errors,
    control_char_errors,
)
from tests.l2.v2.conftest import (
    AT,
    DOC_HASH,
    MD,
    S3_DOC_HASH,
    S3_MD,
    make_emit,
    make_s3_emit,
    make_s3_record,
)

# --- validator/19: the unit cited as its own span ---------------------------

UNIT_MD = (
    "Sales leadership\n"
    "Enterprise sales leadership: 12+ years in a quota-carrying role.\n"
)
UNIT_DOC_HASH = sha256_hex(UNIT_MD.encode("utf-8"))


def unit_anchor_emit() -> dict[str, Any]:
    """The 2026-09-15 external probe: the number and its unit cited separately.

    b000001 "Sales leadership" / b000002 the requirement line. The value aspect
    cites "12+" and the unit aspect cites "years" — the exact split that made
    `derive_quantity` read a dimensionless count of 12 under validator/18.
    """
    whole = {"block_id": "b000002", "text": None, "occurrence": None}
    value = {"block_id": "b000002", "text": "12+", "occurrence": 0}
    unit = {"block_id": "b000002", "text": "years", "occurrence": 0}
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [{
            "id": "s1", "kind": "qualification", "subject": "candidate",
            "topic": "Enterprise sales leadership", "evidence": [whole],
            "importance": "required", "importance_evidence": [whole],
            "polarity": "positive", "polarity_evidence": None,
            "proficiency": None, "proficiency_evidence": None,
            "condition_ids": [], "fact_ids": ["f1"], "unresolved": [],
        }],
        "relations": {"groups": [], "conditions": [], "example_sets": []},
        "facts": {
            "presence": {
                "experience": {"state": "stated", "evidence": [value]},
                "compensation": {"state": "none_found", "evidence": None},
                "quantities": {"state": "none_found", "evidence": None},
                "dates": {"state": "none_found", "evidence": None},
            },
            "entries": [{
                "id": "f1", "family": "experience", "statement_ids": ["s1"],
                "condition_ids": [], "scope": {"kind": "overall", "evidence": None},
                "date_kind": None, "component": None,
                "evidence": {"value": [value], "comparison": None, "unit": [unit],
                             "currency": None, "component": None, "applicability": None},
            }],
        },
        "mentions": [],
        "areas": [{"id": "a1", "name": "Experience", "kind": "capability",
                   "statement_ids": ["s1"], "evidence": None}],
        "block_accounting": [
            {"block_id": "b000001", "disposition": "context", "ref_ids": [],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s1"],
             "exclusion_reason": None, "evidence": None},
        ],
    }


def unit_anchor_record() -> dict[str, Any]:
    return assemble(unit_anchor_emit(), UNIT_MD, document_hash=UNIT_DOC_HASH,
                    observed_model="gpt-5.6-luna", at=AT)


def test_assemble_binds_derives_and_validates() -> None:
    record = assemble(make_emit(), MD, document_hash=DOC_HASH,
                      observed_model="gpt-5.6-luna", at=AT)
    assert validate_record(record, "2") == []
    stmt = record["statements"][0]
    assert MD[slice(*stmt["evidence"][0]["span"])] == stmt["evidence"][0]["text"]
    derived = record["facts"]["entries"][0]["derived"]
    assert derived["state"] == "parsed"
    assert derived["quantity"] == {"dimension": "duration", "comparison": "gte",
                                   "min_value": 96, "max_value": None,
                                   "inclusive_min": True, "inclusive_max": None,
                                   "unit": "month"}
    assert record["extraction"]["schema_version"] == "2"
    assert record["extraction"]["validator_version"] == "20"
    assert record["document"]["annotation_version"] == "blocks/1"
    assert record["extraction"]["candidate_hash"] == candidate_hash(record)


def test_candidate_hash_ignores_quality_and_itself() -> None:
    record = assemble(make_emit(), MD, document_hash=DOC_HASH, observed_model="m", at=AT)
    h = candidate_hash(record)
    record["quality"]["search_eligible"] = True  # quality never moves identity
    assert candidate_hash(record) == h


def test_unparseable_fact_is_kept_not_dropped() -> None:
    # C06's rule: stated-but-unparsed pay is distinguishable from unstated pay
    emit = make_emit()
    emit["facts"]["entries"][0]["evidence"]["comparison"] = None
    emit["facts"]["entries"][0]["evidence"]["value"] = [
        {"block_id": "b000002", "text": "minimum of", "occurrence": 0}]  # no number
    record = assemble(emit, MD, document_hash=DOC_HASH, observed_model="m", at=AT)
    derived = record["facts"]["entries"][0]["derived"]
    assert derived["state"] == "present_unparsed"
    assert derived["quantity"] is None


def test_all_binding_errors_collected() -> None:
    emit = make_emit()
    emit["statements"][0]["evidence"] = [
        {"block_id": "b000099", "text": None, "occurrence": None},
        {"block_id": "b000002", "text": "Ruby", "occurrence": 0},
    ]
    with pytest.raises(AssembleError) as exc:
        assemble(emit, MD, document_hash=DOC_HASH, observed_model="m", at=AT)
    assert len(exc.value.errors) == 2


def test_a_control_character_in_any_emitted_string_is_a_content_error() -> None:
    """validator/17: codex emitted "…at global\\u0000" into a statement topic
    (2026-09-12 campaign, doc 3ad988f4); assemble accepted it and the record
    crashed at the jsonb boundary (Postgres cannot store NUL in text). The
    boundary that owns emit content is assemble: a control character other
    than newline/tab in ANY emitted string is a content error, so the retry
    loop hands it back to the model instead of the store finding it."""
    emit = make_emit()
    emit["statements"][0]["topic"] = "high-throughput services at global\x00"
    with pytest.raises(AssembleError) as exc:
        assemble(emit, MD, document_hash=DOC_HASH, observed_model="m", at=AT)
    assert any("control character" in e and "topic" in e for e in exc.value.errors)


def test_a_separately_cited_unit_anchor_reaches_the_derivation() -> None:
    """validator/19, the external probe: "12+" with the unit cited as its own
    span is twelve YEARS. Under 18 assembly never passed `evidence["unit"]` to
    `derive_quantity` (only the compensation branch forwarded its anchors), so
    the record stored a dimensionless count of 12 and every check agreed."""
    record = unit_anchor_record()
    assert validate_record(record, "2") == []
    derived = record["facts"]["entries"][0]["derived"]
    assert derived["state"] == "parsed"
    assert derived["quantity"] == {"dimension": "duration", "comparison": "gte",
                                   "min_value": 144, "max_value": None,
                                   "inclusive_min": True, "inclusive_max": None,
                                   "unit": "month"}


def test_an_unknown_unit_anchor_keeps_the_fact_present_unparsed() -> None:
    """Null-over-guess at the assembly boundary: a cited unit the grammar does
    not know leaves the fact stated-but-unparsed, never a guessed count."""
    emit = unit_anchor_emit()
    emit["facts"]["entries"][0]["evidence"]["unit"] = [
        {"block_id": "b000002", "text": "quota", "occurrence": 0}]
    record = assemble(emit, UNIT_MD, document_hash=UNIT_DOC_HASH, observed_model="m", at=AT)
    derived = record["facts"]["entries"][0]["derived"]
    assert derived["state"] == "present_unparsed" and derived["quantity"] is None


def test_newlines_and_tabs_in_emitted_strings_stay_legal() -> None:
    emit = make_emit()
    emit["statements"][0]["topic"] = "line one\nline\ttwo"
    record = assemble(emit, MD, document_hash=DOC_HASH, observed_model="m", at=AT)
    assert record["statements"][0]["topic"] == "line one\nline\ttwo"


# --- validator/20, amended 2026-09-28: every character no reader can see -----
# Validator/17 rejected code points below U+0020 only, so zero-width and other
# format characters reached 22% of validated profiles as junk topic tails
# (2026-09-28 review analysis). Under schema 3 the rejected set is now the
# Unicode categories Cc (bar \n \t), Cf, Co, Cn and Cs, in every string the
# model wrote; schema 2 (the frozen v10 tuple replay re-judges) keeps 17's set
# plus the lone surrogate.

INVISIBLE = [
    "​",  # zero-width space (Cf)
    "‌",  # zero-width non-joiner (Cf)
    "‍",  # zero-width joiner (Cf)
    "﻿",  # BOM / zero-width no-break space (Cf)
    "­",  # soft hyphen (Cf)
    "⁠",  # word joiner (Cf)
    "⁣",  # invisible separator (Cf)
    "‎",  # left-to-right mark (Cf)
    "\x7f",    # DEL (Cc above U+0020)
    "\x85",    # NEXT LINE (C1 control, Cc)
    "",  # private use (Co)
    "͸",  # unassigned (Cn)
]


def _invisible_error(path: str, char: str) -> str:
    return f"{path}: control character U+{ord(char):04X} in emitted string"


@pytest.mark.parametrize("char", INVISIBLE, ids=lambda c: f"U+{ord(c):04X}")
def test_an_invisible_format_character_in_a_topic_is_a_content_error(char: str) -> None:
    emit = make_s3_emit()
    emit["statements"][0]["topic"] = f"Sales experience{char}"
    with pytest.raises(AssembleError) as exc:
        assemble(emit, S3_MD, document_hash=S3_DOC_HASH, observed_model="m", at=AT,
                 schema_version="3")
    assert _invisible_error("emit.statements[0].topic", char) in exc.value.errors


def test_schema_2_assembly_keeps_validator_17s_character_rule() -> None:
    """The frozen (v10, "2") registration is re-judged by replay, which cannot
    retry: widening its scan would turn ~4,600 published documents whose only
    ok records end a topic in zero-width junk into refusals with no ladder to
    recover them. Schema 2 keeps 17's set (plus the lone surrogate, which
    crashes hashing); the migration strips the junk on the way to schema 3."""
    emit = make_emit()
    emit["statements"][0]["topic"] = "Sales experience‌"
    record = assemble(emit, MD, document_hash=DOC_HASH, observed_model="m", at=AT)
    assert record["statements"][0]["topic"] == "Sales experience‌"


@pytest.mark.parametrize("char", ["\x00", "\ud800"], ids=["NUL", "surrogate"])
def test_schema_2_assembly_still_refuses_what_the_store_cannot_hold(char: str) -> None:
    emit = make_emit()
    emit["statements"][0]["topic"] = f"Sales experience{char}"
    with pytest.raises(AssembleError) as exc:
        assemble(emit, MD, document_hash=DOC_HASH, observed_model="m", at=AT)
    assert exc.value.errors == [_invisible_error("emit.statements[0].topic", char)]


def test_schema_3_assembly_refuses_the_junk_tail_schema_2_accepts() -> None:
    emit = make_s3_emit()
    emit["statements"][0]["topic"] = "Sales experience‌"
    with pytest.raises(AssembleError) as exc:
        assemble(emit, S3_MD, document_hash=S3_DOC_HASH, observed_model="m", at=AT,
                 schema_version="3")
    assert _invisible_error("emit.statements[0].topic", "‌") in exc.value.errors


@pytest.mark.parametrize(
    ("where", "path"),
    [
        ("note", "emit.source_assessment.note"),
        ("area", "emit.areas[0].name"),
        ("reason", "emit.statements[0].unresolved[0].reason"),
        ("surface", "emit.mentions[0].surface"),
    ],
)
def test_every_model_written_string_is_scanned_for_invisible_format_characters(
    where: str, path: str
) -> None:
    emit = make_s3_emit()
    whole = {"block_id": "b000002", "text": None, "occurrence": None}
    if where == "note":
        emit["source_assessment"]["note"] = "complete posting​"
    elif where == "area":
        emit["areas"][0]["name"] = "Experience﻿"
    elif where == "reason":
        emit["statements"][0]["unresolved"] = [{"reason": "scope­", "evidence": [whole]}]
    else:
        emit["mentions"] = [{
            "id": "m1", "surface": "sales⁠",
            "evidence": {"block_id": "b000002", "text": "sales", "occurrence": 0},
            "statement_ids": ["s1"], "role": "direct",
        }]
    with pytest.raises(AssembleError) as exc:
        assemble(emit, S3_MD, document_hash=S3_DOC_HASH, observed_model="m", at=AT,
                 schema_version="3")
    assert [e for e in exc.value.errors if e.startswith(f"{path}: control character")]


#: a posting whose own text carries a zero-width space in its heading and body
#: — 1,011 of 44,588 canonical documents carry such a character (md/1 applies
#: NFKC, which does not remove format characters)
ZW_MD = "## Require​ments\nA minimum of 8 years of experience in sales​.\n"
ZW_DOC_HASH = sha256_hex(ZW_MD.encode("utf-8"))


def test_a_quote_of_the_documents_own_invisible_format_character_stands() -> None:
    """A bound quote is the document's bytes, not the model's: binding already
    proves every character of it stands in the cited block, and rejecting it
    would fail a faithful quote the model has no way to fix. So is a heading
    code derives from the document. Both keep validator/17's scan only."""
    emit = make_s3_emit()
    emit["statements"][0]["evidence"] = [
        {"block_id": "b000002", "text": "experience in sales​.", "occurrence": 0}
    ]
    record = assemble(emit, ZW_MD, document_hash=ZW_DOC_HASH, observed_model="m", at=AT,
                      schema_version="3")
    statement = record["statements"][0]
    assert statement["evidence"][0]["text"] == "experience in sales​."
    assert statement["section_heading"] == "Require​ments"
    # the schema-3 scan over the sealed record agrees
    assert character_errors("record", record, schema_version="3") == []


def test_a_quote_still_may_not_carry_a_control_character_below_u0020() -> None:
    """Validator/17's own set stays total: no document can hold a NUL (the
    store cannot), so a quote carrying one is fabricated wherever it sits."""
    emit = make_s3_emit()
    emit["statements"][0]["evidence"] = [
        {"block_id": "b000002", "text": "in sales\x00", "occurrence": 0}
    ]
    with pytest.raises(AssembleError) as exc:
        assemble(emit, S3_MD, document_hash=S3_DOC_HASH, observed_model="m", at=AT,
                 schema_version="3")
    assert _invisible_error("emit.statements[0].evidence[0].text", "\x00") in exc.value.errors


def test_the_schema_3_scan_names_an_invisible_format_character_by_path() -> None:
    record = make_s3_record()
    record["statements"][0]["topic"] += "‌"
    assert character_errors("record", record, schema_version="3") == [
        _invisible_error("record.statements[0].topic", "‌")
    ]
    assert character_errors("record", record, schema_version="2") == []


def test_the_storability_check_is_a_storage_constraint_only() -> None:
    """`control_char_errors` is what replay's storability check and the live
    fold's adoption call (`rebuild._storable_event`, `runner._storable`). It
    refuses only what jsonb or hashing cannot hold — validator/17's set and a
    lone surrogate — so an archived record with a zero-width topic tail, which
    Postgres stores fine, folds exactly as it did before validator 20."""
    record = make_s3_record()
    record["statements"][0]["topic"] += "‌​"
    assert control_char_errors("record", record) == []
    record["statements"][0]["topic"] += "\x00"
    assert control_char_errors("record", record) == [
        _invisible_error("record.statements[0].topic", "\x00")
    ]


# The invisible set is a frozen table, not the running interpreter's Unicode
# database. Category Cn (unassigned) shrinks with every Unicode release: U+2FFC
# and U+31EF are Cn under Python 3.12 (Unicode 15.0.0) and So under 3.14
# (16.0.0). Read live, validator 20 would accept on one interpreter a record it
# refuses on another, with no version bump.


@pytest.mark.parametrize("claimed", ["So", "Lo", "Cf", "Cn"])
def test_the_invisible_set_ignores_the_interpreters_unicode_database(
    monkeypatch: pytest.MonkeyPatch, claimed: str
) -> None:
    """Whatever category the running database reports, the verdict is Unicode
    15.0.0's: a later Python assigning U+2FFC does not make it storable, and a
    database calling every letter unassigned does not refuse "Sales"."""
    import unicodedata

    monkeypatch.setattr(unicodedata, "category", lambda ch: claimed)
    assert character_errors("r", {"topic": "Sales⿼㇯"}, schema_version="3") == [
        "r.topic: control character U+2FFC,U+31EF in emitted string"
    ]
    assert character_errors("r", {"topic": "Sales​"}, schema_version="3") == [
        _invisible_error("r.topic", "​")
    ]
    assert character_errors("r", {"topic": "Sales experience"}, schema_version="3") == []


def test_the_invisible_table_is_frozen_at_unicode_15() -> None:
    """The table's bytes are validator 20's identity: editing them (a newer
    Unicode, another category) is a validator bump, never an edit in place."""
    from jobhunter.hashing import canonical_json
    from jobhunter.l2.v2.invisible import RANGES, UNICODE_VERSION

    assert UNICODE_VERSION == "15.0.0"
    assert len(RANGES) == 713
    assert sha256_hex(canonical_json(RANGES)) == INVISIBLE_TABLE_SHA


#: sha256 of `canonical_json(invisible.RANGES)` — validator 20's frozen table
INVISIBLE_TABLE_SHA = "2e140e6df6863eaea707bb0e1010149c900be7332469ff7fcd4d681c431cb3d2"


def test_the_invisible_table_is_unicode_15s_general_categories() -> None:
    """Derivation check: under a Unicode 15.0.0 database the table is exactly
    categories Cc (bar \\n \\t), Cf, Co, Cn and Cs, code point by code point."""
    import unicodedata

    from jobhunter.l2.v2.invisible import UNICODE_VERSION, invisible

    if unicodedata.unidata_version != UNICODE_VERSION:
        pytest.skip(f"running Unicode {unicodedata.unidata_version}, table is {UNICODE_VERSION}")
    categories = {"Cc", "Cf", "Co", "Cn", "Cs"}
    wrong = [
        cp for cp in range(0x110000)
        if invisible(chr(cp)) != (
            chr(cp) not in "\n\t" and unicodedata.category(chr(cp)) in categories
        )
    ]
    assert wrong == []


# A lone surrogate (category Cs) is what `json.loads` makes of a `\ud800`
# escape in the model's JSON. No UTF-8 encoder can write one, so it passed the
# scan and crashed `candidate_hash` with a UnicodeEncodeError — outside the
# content-retry loop, the crash class validator/17's NUL scan exists to stop.


def test_a_lone_surrogate_the_model_escaped_is_a_content_error_not_a_crash() -> None:
    raw = json.dumps(make_s3_emit()).replace('"Sales experience"', '"Sales experience\\ud800"')
    emit = json.loads(raw)
    assert emit["statements"][0]["topic"] == "Sales experience\ud800"
    with pytest.raises(AssembleError) as exc:
        assemble(emit, S3_MD, document_hash=S3_DOC_HASH, observed_model="m", at=AT,
                 schema_version="3")
    assert _invisible_error("emit.statements[0].topic", "\ud800") in exc.value.errors


def test_a_lone_surrogate_is_refused_in_document_owned_text_too() -> None:
    """No document holds one — canonical text is UTF-8 — so a bound quote
    carrying it was fabricated, and the store could not write it either."""
    record = make_s3_record()
    record["statements"][0]["evidence"][0]["text"] += "\udfff"
    assert control_char_errors("record", record) == [
        _invisible_error("record.statements[0].evidence[0].text", "\udfff")
    ]


# --- schema 3: headings in, verdicts out (parsing contract v3 §2.1) --------


def test_schema_3_assembly_writes_the_heading_and_drops_the_verdicts() -> None:
    record = make_s3_record()
    assert validate_record(record, "3") == []
    statement = record["statements"][0]
    assert statement["section_heading"] == "Requirements"
    assert not {"importance", "importance_evidence",
                "proficiency", "proficiency_evidence"} & set(statement)
    modality = statement["modality_evidence"][0]
    assert modality["text"] == "A minimum of"
    assert S3_MD[slice(*modality["span"])] == modality["text"]
    assert record["extraction"]["schema_version"] == "3"
    assert record["extraction"]["validator_version"] == "20"
    assert record["extraction"]["candidate_hash"] == candidate_hash(record)


def test_schema_3_section_heading_is_null_when_no_heading_precedes() -> None:
    # MD's first block is a bare "Requirements" line — a paragraph, not a
    # heading, so the statement below it sits under nothing
    record = assemble(make_s3_emit(), MD, document_hash=DOC_HASH,
                      observed_model="m", at=AT, schema_version="3")
    assert record["statements"][0]["section_heading"] is None


def test_an_emitted_section_heading_is_never_read() -> None:
    """`section_heading` is code-owned: a model copy is ignored, not trusted
    (the emit schema rejects it outright — see tests/l2/test_schemas.py)."""
    emit = make_s3_emit()
    emit["statements"][0]["section_heading"] = "Benefits"
    record = assemble(emit, S3_MD, document_hash=S3_DOC_HASH,
                      observed_model="m", at=AT, schema_version="3")
    assert record["statements"][0]["section_heading"] == "Requirements"


def test_modality_evidence_is_null_when_the_posting_carries_no_modal_phrase() -> None:
    emit = make_s3_emit()
    emit["statements"][0]["modality_evidence"] = None
    record = assemble(emit, S3_MD, document_hash=S3_DOC_HASH,
                      observed_model="m", at=AT, schema_version="3")
    assert record["statements"][0]["modality_evidence"] is None
    assert validate_record(record, "3") == []


@pytest.mark.parametrize("field", ["evidence", "modality_evidence"])
def test_a_miscited_quote_fails_the_same_way_in_either_reference_family(field: str) -> None:
    """Evidence binding is one mechanism: `modality_evidence` goes through
    `source.resolve` and reports a mis-cite exactly as `evidence` does."""
    emit = make_s3_emit()
    emit["statements"][0][field] = [
        {"block_id": "b000002", "text": "Ruby", "occurrence": 0}
    ]
    with pytest.raises(AssembleError) as exc:
        assemble(emit, S3_MD, document_hash=S3_DOC_HASH,
                 observed_model="m", at=AT, schema_version="3")
    assert exc.value.errors == [
        f"statements[0].{field}[0]: b000002: not a literal substring: 'Ruby'"
    ]


def test_schema_2_assembly_is_untouched_by_the_schema_3_path() -> None:
    record = assemble(make_emit(), MD, document_hash=DOC_HASH,
                      observed_model="gpt-5.6-luna", at=AT)
    statement = record["statements"][0]
    assert record["extraction"]["schema_version"] == "2"
    assert statement["importance"] == "required" and statement["proficiency"] is None
    assert "section_heading" not in statement and "modality_evidence" not in statement
