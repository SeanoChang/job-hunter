"""Schema 2 → schema 3, offline (parsing contract v3 §7).

The migration is the whole reason the corpus does not have to be re-extracted:
a schema-2 archived emit or record derives its schema-3 shape with zero engine
calls. What it must never do is invent a modality — the field it replaces was a
verdict the model assigned from descriptor text, and turning "Demonstrable
expertise" into `required` by another name would carry the defect across the
bump. Only a quote the code-owned lexicon recognises survives.

These tests are also the contract the fixture regeneration
(`scripts/migrate_cases_v3.py`) stands on: the case corpus is derived by this
function, never hand-edited.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from jobhunter.hashing import canonical_json, sha256_hex
from jobhunter.l2.schemas import validate_emit, validate_record
from jobhunter.l2.v2 import migrate
from jobhunter.l2.v2.assemble import assemble, candidate_hash, section_heading
from jobhunter.l2.v2.source import annotate
from jobhunter.l2.v2.verify import verify

AT = "2026-09-22T00:00:00+00:00"
MODEL = "fixture-hand-authored"

MD = (
    "## What You Will Bring\n"
    "- Must have 5 years of Python experience.\n"
    "- Demonstrable expertise in distributed systems.\n"
)
DOC_HASH = sha256_hex(MD.encode("utf-8"))


def _whole(block_id: str) -> dict[str, Any]:
    return {"block_id": block_id, "text": None, "occurrence": None}


def _ref(block_id: str, text: str, occurrence: int = 0) -> dict[str, Any]:
    return {"block_id": block_id, "text": text, "occurrence": occurrence}


def emit2() -> dict[str, Any]:
    """Two schema-2 statements: one whose verdict quotes a modal phrase, one
    whose verdict quotes a descriptor and a proficiency the model inferred."""
    absent = {"state": "none_found", "evidence": None}
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [
            {
                "id": "s_python", "kind": "qualification", "subject": "candidate",
                "topic": "Python experience", "evidence": [_whole("b000002")],
                "importance": "required",
                "importance_evidence": [_ref("b000002", "Must have")],
                "polarity": "positive", "polarity_evidence": None,
                "proficiency": None, "proficiency_evidence": None,
                "condition_ids": [], "fact_ids": [], "unresolved": [],
            },
            {
                "id": "s_distributed", "kind": "qualification", "subject": "candidate",
                "topic": "Distributed systems", "evidence": [_whole("b000003")],
                "importance": "required",
                "importance_evidence": [_ref("b000003", "Demonstrable expertise")],
                "polarity": "positive", "polarity_evidence": None,
                "proficiency": "expert",
                "proficiency_evidence": [_ref("b000003", "Demonstrable expertise")],
                "condition_ids": [], "fact_ids": [], "unresolved": [],
            },
        ],
        "relations": {"groups": [], "conditions": [], "example_sets": []},
        "facts": {
            "presence": {"experience": dict(absent), "compensation": dict(absent),
                         "quantities": dict(absent), "dates": dict(absent)},
            "entries": [],
        },
        "mentions": [
            {"id": "m_python", "surface": "Python", "evidence": _ref("b000002", "Python"),
             "statement_ids": ["s_python"], "role": "direct"},
        ],
        "areas": [{"id": "a1", "name": "What You Will Bring", "kind": "capability",
                   "statement_ids": ["s_python", "s_distributed"], "evidence": None}],
        "block_accounting": [
            {"block_id": "b000001", "disposition": "context", "ref_ids": [],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s_python"],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000003", "disposition": "statements", "ref_ids": ["s_distributed"],
             "exclusion_reason": None, "evidence": None},
        ],
    }


def record2() -> dict[str, Any]:
    return assemble(emit2(), MD, document_hash=DOC_HASH, observed_model=MODEL, at=AT)


def by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items}


def errors_of(record: dict[str, Any], markdown: str) -> list[Any]:
    report = verify(record, markdown, schema_version="3")
    return [(f.code, f.path, f.detail) for f in report.findings if f.severity == "error"]


# --- the lexicon rule (spec §7) --------------------------------------------


def test_a_modal_quote_becomes_the_modality_reference() -> None:
    """"Must have" is the posting's own modal phrase: it survives the bump as
    the quote it always was, same block, same text, same occurrence."""
    statements = by_id(migrate.emit3_of(emit2())["statements"])
    assert statements["s_python"]["modality_evidence"] == [_ref("b000002", "Must have")]


def test_a_descriptor_quote_derives_no_modality() -> None:
    """"Demonstrable expertise" is a descriptor the model read a verdict out of
    (spec §2.1). Null over guess: it derives nothing."""
    statements = by_id(migrate.emit3_of(emit2())["statements"])
    assert statements["s_distributed"]["modality_evidence"] is None


@pytest.mark.parametrize("term", migrate.MODAL_LEXICON)
def test_every_lexicon_term_is_recognised_in_a_quote(term: str) -> None:
    assert migrate.quotes_modality(f"A phrase that {term} appears in")


@pytest.mark.parametrize(
    "text",
    ["Demonstrable expertise", "Advanced", "Strong SQL skills", "We ask that you come in", ""],
)
def test_a_quote_with_no_lexicon_term_is_not_a_modality(text: str) -> None:
    assert not migrate.quotes_modality(text)


def test_the_match_is_case_insensitive() -> None:
    assert migrate.quotes_modality("MUST HAVE") and migrate.quotes_modality("Preferred")


def test_the_lexicon_is_the_spec_list() -> None:
    assert migrate.MODAL_LEXICON == (
        "must", "required", "require", "preferred", "prefer", "ideally",
        "plus", "nice to have", "bonus", "minimum", "at least", "strongly",
    )


# --- the emit derivation ---------------------------------------------------


def test_the_verdicts_and_their_evidence_are_gone_from_the_emit() -> None:
    for statement in migrate.emit3_of(emit2())["statements"]:
        for verdict in ("importance", "importance_evidence",
                        "proficiency", "proficiency_evidence"):
            assert verdict not in statement


def test_the_derived_emit_validates_under_schema_3() -> None:
    assert validate_emit(migrate.emit3_of(emit2()), "3") == []


def test_the_derived_emit_carries_no_code_owned_heading() -> None:
    """`section_heading` is assembly's (spec §2.1), so it never appears in an
    emit — schema 3 rejects one, and the derivation must not supply it."""
    assert all("section_heading" not in s
               for s in migrate.emit3_of(emit2())["statements"])


def test_everything_but_the_statements_is_carried_through_untouched() -> None:
    """Spec §2.2: every shape but the statement's is byte-for-byte schema 2's.

    `block_accounting` belongs on this list and moves for exactly one reason,
    which this fixture does not trigger: a row whose coverage the derivation
    itself un-evidenced (see the heading-row tests below).
    """
    source, derived = emit2(), migrate.emit3_of(emit2())
    for key in ("source_assessment", "relations", "facts", "mentions", "areas",
                "block_accounting"):
        assert derived[key] == source[key]


def test_the_derivation_never_mutates_its_input() -> None:
    source = emit2()
    before = canonical_json(source)
    migrate.emit3_of(source)
    assert canonical_json(source) == before


def test_a_whole_block_quote_reads_the_blocks_text_when_the_annotation_is_given() -> None:
    """A whole-block reference quotes the whole block, so whether it carries a
    modal term is a question only the annotation can answer."""
    markdown = "**Required Qualifications**\n- Five years of Python.\n"
    emit = emit2()
    emit["statements"] = [emit["statements"][0]]
    emit["statements"][0]["evidence"] = [_whole("b000002")]
    emit["statements"][0]["importance_evidence"] = [_whole("b000001")]
    emit["block_accounting"] = emit["block_accounting"][:2]

    with_blocks = migrate.emit3_of(emit, blocks=annotate(markdown))
    assert with_blocks["statements"][0]["modality_evidence"] == [_whole("b000001")]


def test_a_whole_block_quote_with_no_annotation_derives_null() -> None:
    """Null over guess: with no blocks to read, the quote's text is unknown."""
    emit = emit2()
    emit["statements"][0]["importance_evidence"] = [_whole("b000001")]
    assert migrate.emit3_of(emit)["statements"][0]["modality_evidence"] is None


# --- the record derivation -------------------------------------------------


def test_the_record_keeps_the_bound_modality_reference() -> None:
    derived = migrate.record3_of(record2(), annotate(MD))
    modality = by_id(derived["statements"])["s_python"]["modality_evidence"]
    assert modality is not None
    assert MD[modality[0]["span"][0]:modality[0]["span"][1]] == "Must have"


def test_the_record_drops_the_descriptor_verdict_and_its_proficiency() -> None:
    statement = by_id(migrate.record3_of(record2(), annotate(MD))["statements"])
    assert statement["s_distributed"]["modality_evidence"] is None
    for verdict in ("importance", "importance_evidence", "proficiency", "proficiency_evidence"):
        assert all(verdict not in s
                   for s in migrate.record3_of(record2(), annotate(MD))["statements"])


def test_the_section_heading_is_assemblys_own_derivation() -> None:
    blocks = annotate(MD)
    derived = migrate.record3_of(record2(), blocks)
    for statement in derived["statements"]:
        assert statement["section_heading"] == section_heading(blocks, statement["evidence"])
    assert derived["statements"][0]["section_heading"] == "What You Will Bring"


def test_the_derived_record_declares_schema_3_and_validator_20() -> None:
    extraction = migrate.record3_of(record2(), annotate(MD))["extraction"]
    assert extraction["schema_version"] == "3"
    assert extraction["validator_version"] == "20"


def test_the_derived_record_verifies_clean_under_20_with_schema_3() -> None:
    derived = migrate.record3_of(record2(), annotate(MD))
    assert validate_record(derived, "3") == []
    assert errors_of(derived, MD) == []


def test_the_derived_record_is_rehashed() -> None:
    """The candidate hash covers the record's shape, so a migrated candidate is
    a different candidate — a stale hash would make it unverifiable."""
    base = record2()
    derived = migrate.record3_of(base, annotate(MD))
    assert derived["extraction"]["candidate_hash"] != base["extraction"]["candidate_hash"]
    shadow = copy.deepcopy(derived)
    shadow["extraction"]["candidate_hash"] = ""
    shadow["quality"] = None
    assert derived["extraction"]["candidate_hash"] == sha256_hex(canonical_json(shadow))


def test_the_record_derivation_never_mutates_its_input() -> None:
    base = record2()
    before = canonical_json(base)
    migrate.record3_of(base, annotate(MD))
    assert canonical_json(base) == before


# --- the input is schema 2, or there is no derivation -----------------------


def test_the_emit_derivation_refuses_an_emit_that_is_already_schema_3() -> None:
    """A no-op migration is loud, never clean.

    Re-deriving reads `importance_evidence`, which a schema-3 statement does
    not have, so every modality would derive null — and the result would still
    validate as a schema-3 emit, so nothing downstream could tell a derivation
    from a silent erasure. [[T-20260922-EBFY]] replays this over an archive
    holding both shapes; a mis-routed emit has to fail, not degrade.
    """
    derived = migrate.emit3_of(emit2())
    with pytest.raises(migrate.MigrateError) as excinfo:
        migrate.emit3_of(derived)
    assert "schema 2" in str(excinfo.value)


def test_the_record_derivation_refuses_a_record_that_already_declares_schema_3() -> None:
    """Same erasure, and worse: `record3_of` re-stamps and re-hashes, so the
    degraded record is a NEW verifiably-clean candidate with the modality
    gone."""
    blocks = annotate(MD)
    derived = migrate.record3_of(record2(), blocks)
    with pytest.raises(migrate.MigrateError) as excinfo:
        migrate.record3_of(derived, blocks)
    assert "schema 2" in str(excinfo.value)


def test_the_record_derivation_refuses_a_record_that_declares_no_schema() -> None:
    """Unknown is not schema 2: an envelope with no shape is refused like any
    other non-2 shape rather than assumed to be the old one."""
    base = record2()
    del base["extraction"]["schema_version"]
    with pytest.raises(migrate.MigrateError):
        migrate.record3_of(base, annotate(MD))


def test_an_emit_with_no_statements_still_derives() -> None:
    """A source the extractor found unusable emits no statements, and that is a
    legitimate schema-2 emit — there is nothing to read a shape off, and
    nothing that could be erased either."""
    empty = emit2()
    empty["statements"] = []
    empty["mentions"] = []
    empty["areas"] = []
    empty["relations"] = {"groups": [], "conditions": [], "example_sets": []}
    assert migrate.emit3_of(empty)["statements"] == []


# --- the accounting rows the derivation itself un-evidences -----------------
#
# The dominant live shape, and the one that cost 6,534 documents their migrated
# row (2026-09-23 replay): schema 2 covered a HEADING block by citing it as the
# importance evidence of the statements underneath it, which is what the v10
# prompt's own worked example does —
#
#   {"block_id": "b000001", "disposition": "statements",
#    "ref_ids": ["s1", "s2", "s3"], ...}   # "this heading carries their importance"
#
# and the v11 prompt makes that same block a "context" row with no ref_ids,
# because a heading is never quoted as a statement's modality. Schema 3 retires
# `importance_evidence`, so the derivation drops the only reference that proved
# the claim — and `accounting:coverage_unevidenced` (validator/19) then fails a
# record that verified clean under the shape it was extracted in.

MD_MODAL = (
    "## Minimum qualifications\n"
    "- 5 years of Python experience.\n"
    "- Distributed systems experience.\n"
)


def heading_emit2() -> dict[str, Any]:
    """`emit2`, with the heading block carrying both statements' importance."""
    emit = emit2()
    for statement in emit["statements"]:
        statement["importance_evidence"] = [_whole("b000001")]
    emit["statements"][1]["proficiency"] = None
    emit["statements"][1]["proficiency_evidence"] = None
    emit["block_accounting"][0] = {
        "block_id": "b000001", "disposition": "statements",
        "ref_ids": ["s_python", "s_distributed"],
        "exclusion_reason": None, "evidence": None,
    }
    return emit


def record2_of(emit: dict[str, Any], markdown: str) -> dict[str, Any]:
    return assemble(emit, markdown, document_hash=sha256_hex(markdown.encode("utf-8")),
                    observed_model=MODEL, at=AT)


def accounting_of(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["block_id"]: entry for entry in record["block_accounting"]}


def test_the_schema_2_record_verifies_clean_before_the_derivation() -> None:
    """The premise: this is not a defective archive row. It is a record the
    frozen bundle judged and published, so anything the derived record fails is
    the derivation's doing."""
    record = record2_of(heading_emit2(), MD)
    report = verify(record, MD, schema_version="2")
    assert [(f.code, f.path) for f in report.findings if f.severity == "error"] == []


def test_a_heading_row_the_derivation_unevidenced_becomes_a_context_row() -> None:
    """The v11 shape of the v10 example: nothing quotes the heading any more,
    so the row says `context` rather than claiming statements that no longer
    cite it."""
    derived = migrate.record3_of(record2_of(heading_emit2(), MD), annotate(MD))
    assert accounting_of(derived)["b000001"] == {
        "block_id": "b000001", "disposition": "context", "ref_ids": [],
        "exclusion_reason": None, "evidence": None,
    }


def test_the_derived_record_verifies_clean_when_the_heading_carried_the_verdict() -> None:
    """The migration's promise (spec §7): offline and COMPLETE. A record that
    verified under the shape it was extracted in derives to one that verifies
    under the shape it is migrated to."""
    derived = migrate.record3_of(record2_of(heading_emit2(), MD), annotate(MD))
    assert validate_record(derived, "3") == []
    assert errors_of(derived, MD) == []


def test_a_row_whose_quote_survives_as_a_modality_is_untouched() -> None:
    """Only the rows the derivation un-evidenced move. "Minimum qualifications"
    carries a lexicon term, so the heading survives as both statements'
    modality quote and goes on covering its own block."""
    record = record2_of(heading_emit2(), MD_MODAL)
    derived = migrate.record3_of(record, annotate(MD_MODAL))
    assert by_id(derived["statements"])["s_python"]["modality_evidence"] is not None
    assert accounting_of(derived)["b000001"] == accounting_of(record)["b000001"]
    assert errors_of(derived, MD_MODAL) == []


def test_a_row_schema_2_had_already_left_unevidenced_is_carried_through() -> None:
    """The derivation re-accounts what IT un-evidenced, never what the archived
    record got wrong on its own: rewriting the latter would hide an extraction
    defect behind a migration."""
    emit = emit2()
    emit["block_accounting"][2]["ref_ids"] = ["s_python"]  # s_python quotes b000002
    record = record2_of(emit, MD)
    derived = migrate.record3_of(record, annotate(MD))
    assert accounting_of(derived)["b000003"] == accounting_of(record)["b000003"]
    assert [code for code, _, _ in errors_of(derived, MD)] == ["coverage_unevidenced"]


def test_the_emit_derivation_re_accounts_the_same_rows() -> None:
    """The fixture corpus is derived from emits and the live archive from
    records, and the two have to land on the same candidate (below) — so the
    emit derivation re-accounts too, on the ids the emit itself names."""
    derived = migrate.emit3_of(heading_emit2(), blocks=annotate(MD))
    assert accounting_of(derived)["b000001"]["disposition"] == "context"
    assert accounting_of(derived)["b000001"]["ref_ids"] == []


def test_the_two_paths_agree_when_a_heading_row_is_re_accounted() -> None:
    blocks = annotate(MD)
    from_emit = assemble(
        migrate.emit3_of(heading_emit2(), blocks=blocks), MD, document_hash=DOC_HASH,
        observed_model=MODEL, at=AT, schema_version="3",
    )
    from_record = migrate.record3_of(record2_of(heading_emit2(), MD), blocks)
    assert canonical_json(from_emit) == canonical_json(from_record)


# --- determinism and the two paths agreeing ---------------------------------


def test_the_derivation_is_deterministic() -> None:
    blocks = annotate(MD)
    assert canonical_json(migrate.emit3_of(emit2())) == canonical_json(migrate.emit3_of(emit2()))
    assert (canonical_json(migrate.record3_of(record2(), blocks))
            == canonical_json(migrate.record3_of(record2(), blocks)))


def test_migrating_the_emit_and_migrating_the_record_derive_the_same_candidate() -> None:
    """The invariant the fixture regeneration rests on: replaying a schema-2
    ARCHIVE through `record3_of` and re-extracting its migrated EMIT through
    assembly land on the same candidate, hash included. If they could differ,
    the migrated case corpus would not be the corpus `rebuild` produces."""
    blocks = annotate(MD)
    from_emit = assemble(
        migrate.emit3_of(emit2(), blocks=blocks), MD, document_hash=DOC_HASH,
        observed_model=MODEL, at=AT, schema_version="3",
    )
    from_record = migrate.record3_of(record2(), blocks)
    assert canonical_json(from_emit) == canonical_json(from_record)


# --- validator/20 amended its quantity grammar in place (2026-09-28) --------

PTO_MD = "## Benefits\n- Up to 30 days per year of paid time off.\n"
PTO_DOC_HASH = sha256_hex(PTO_MD.encode("utf-8"))


def _pto_emit2() -> dict[str, Any]:
    """The archived shape of attempt 21T201613Z-23d6b3dca786-s1a2: a value and
    its unit cited apart, the unit being a per-year rate."""
    emit = emit2()
    emit["statements"] = [{
        "id": "s_pto", "kind": "employer_context", "subject": "employer",
        "topic": "Paid time off", "evidence": [_whole("b000002")],
        "importance": None, "importance_evidence": None,
        "polarity": "positive", "polarity_evidence": None,
        "proficiency": None, "proficiency_evidence": None,
        "condition_ids": [], "fact_ids": ["f_pto"], "unresolved": [],
    }]
    emit["facts"]["entries"] = [{
        "id": "f_pto", "family": "quantity", "statement_ids": ["s_pto"],
        "condition_ids": [], "scope": None, "date_kind": None, "component": None,
        "evidence": {"value": [_ref("b000002", "30")],
                     "comparison": [_ref("b000002", "Up to")],
                     "unit": [_ref("b000002", "days per year")],
                     "currency": None, "component": None, "applicability": None},
    }]
    emit["mentions"] = []
    emit["areas"] = []
    emit["block_accounting"] = [
        {"block_id": "b000001", "disposition": "context", "ref_ids": [],
         "exclusion_reason": None, "evidence": None},
        {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s_pto"],
         "exclusion_reason": None, "evidence": None},
        {"block_id": "b000002", "disposition": "facts", "ref_ids": ["f_pto"],
         "exclusion_reason": None, "evidence": None},
    ]
    return emit


def test_the_record_derivation_re_derives_facts_under_the_amended_grammar() -> None:
    """The live fold ADOPTS an archived schema-2 record — sealed under 19, its
    "30" · "days per year" derived as 360 months — while the replay re-assembles
    the raw emit under 20, where it derives nothing. `derived` is code-owned
    like `section_heading`, so the derivation re-derives it: one candidate, one
    hash, whichever path folded it (`l2/rebuild` docstring), and a live
    re-settle never re-publishes the misread the amendment removed."""
    blocks = annotate(PTO_MD)
    fresh = assemble(_pto_emit2(), PTO_MD, document_hash=PTO_DOC_HASH,
                     observed_model=MODEL, at=AT)
    assert fresh["facts"]["entries"][0]["derived"]["state"] == "present_unparsed"
    sealed19 = copy.deepcopy(fresh)
    sealed19["facts"]["entries"][0]["derived"] = {
        "state": "parsed", "money": None, "date": None,
        "quantity": {"dimension": "duration", "comparison": "lte", "min_value": None,
                     "max_value": 360, "inclusive_min": None, "inclusive_max": True,
                     "unit": "month"},
    }
    sealed19["extraction"]["validator_version"] = "19"
    sealed19["extraction"]["candidate_hash"] = candidate_hash(sealed19)

    adopted = migrate.record3_of(sealed19, blocks)
    replayed = migrate.record3_of(fresh, blocks)
    assert adopted["facts"]["entries"][0]["derived"] == fresh["facts"]["entries"][0]["derived"]
    assert canonical_json(adopted) == canonical_json(replayed)
    assert errors_of(adopted, PTO_MD) == []


# --- validator/20: the derivation strips the model's invisible junk --------
# Schema 2 keeps validator/17's character rule (the frozen tuple replays and
# cannot retry), schema 3 enforces the full invisible table, and the derivation
# between them removes invisible code points from what the MODEL wrote. The
# document's own bytes — a quote carrying the posting's zero-width space — are
# never touched.

ZW_MD = (
    "## What You Will Bring\n"
    "- Must have 5 years of Python​ experience.\n"
    "- Demonstrable expertise in distributed systems.\n"
)
ZW_DOC_HASH = sha256_hex(ZW_MD.encode("utf-8"))


def _junk_emit2() -> dict[str, Any]:
    """`emit2()` with the junk tails v10 left in 16% of served documents, and a
    quote of the document's own U+200B."""
    emit = emit2()
    python = emit["statements"][0]
    python["topic"] = "Python experience‌‍"
    python["evidence"] = [_ref("b000002", "Python​ experience")]
    python["unresolved"] = [{"reason": "years scope⁠", "evidence": [_whole("b000002")]}]
    emit["areas"][0]["name"] = "What You Will Bring﻿"
    emit["source_assessment"]["note"] = "complete posting​"
    return emit


def _junk_record2() -> dict[str, Any]:
    return assemble(_junk_emit2(), ZW_MD, document_hash=ZW_DOC_HASH,
                    observed_model=MODEL, at=AT)


def test_the_record_derivation_strips_invisible_junk_from_what_the_model_wrote() -> None:
    """A topic is the model's paraphrase, not a quote: removing code points no
    reader can see does not misstate the source. The quote keeps the posting's
    own U+200B, the record re-hashes, and it passes schema 3's full rule."""
    from jobhunter.l2.v2.assemble import character_errors

    record = _junk_record2()
    assert record["statements"][0]["topic"] == "Python experience‌‍"
    derived = migrate.record3_of(record, annotate(ZW_MD))
    python = by_id(derived["statements"])["s_python"]
    assert python["topic"] == "Python experience"
    assert python["evidence"][0]["text"] == "Python​ experience"
    assert python["unresolved"][0]["reason"] == "years scope"
    assert derived["areas"][0]["name"] == "What You Will Bring"
    assert derived["source_assessment"]["note"] == "complete posting"
    assert validate_record(derived, "3") == []
    assert errors_of(derived, ZW_MD) == []
    assert character_errors("record", derived, schema_version="3") == []
    assert derived["extraction"]["candidate_hash"] == candidate_hash(derived)


def test_the_emit_derivation_strips_the_same_junk_and_the_two_paths_agree() -> None:
    blocks = annotate(ZW_MD)
    emit3 = migrate.emit3_of(_junk_emit2(), blocks=blocks)
    assert emit3["statements"][0]["topic"] == "Python experience"
    assert emit3["statements"][0]["evidence"][0]["text"] == "Python​ experience"
    from_emit = assemble(emit3, ZW_MD, document_hash=ZW_DOC_HASH, observed_model=MODEL,
                         at=AT, schema_version="3")
    from_record = migrate.record3_of(_junk_record2(), blocks)
    assert canonical_json(from_emit) == canonical_json(from_record)
