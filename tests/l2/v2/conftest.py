"""Shared v2 record fixtures.

Each fixture builds its own emit + markdown pair rather than mutating a shared
one — spans are line-relative under `blocks/1`, so growing a shared fixture in
place is a silent source of block-id drift across tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.v2.assemble import assemble
from jobhunter.l2.v2.source import annotate, blocks_by_id, resolve

MD = "Requirements\nA minimum of 8 years of experience in sales.\n"
DOC_HASH = sha256_hex(MD.encode("utf-8"))
AT = "2026-09-07T00:00:00+00:00"

MENTIONS_MD = (
    "Requirements\n"
    "Bachelor's degree required.\n"
    "CPA certification preferred.\n"
)
MENTIONS_DOC_HASH = sha256_hex(MENTIONS_MD.encode("utf-8"))


def _emit_with_mentions() -> dict[str, Any]:
    # blocks: b000001 "Requirements" / b000002 "Bachelor's degree required." /
    # b000003 "CPA certification preferred."
    whole_b1 = {"block_id": "b000001", "text": None, "occurrence": None}
    whole_b2 = {"block_id": "b000002", "text": None, "occurrence": None}
    whole_b3 = {"block_id": "b000003", "text": None, "occurrence": None}
    cpa_ref = {"block_id": "b000003", "text": "CPA", "occurrence": 0}
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [
            {
                "id": "s_degree", "kind": "qualification", "subject": "candidate",
                "topic": "Bachelor's degree", "evidence": [whole_b2],
                "importance": "required", "importance_evidence": [whole_b2],
                "polarity": "positive", "polarity_evidence": None,
                "proficiency": None, "proficiency_evidence": None,
                "condition_ids": [], "fact_ids": [], "unresolved": [],
            },
            {
                # the C04 shape: a preferred certification sitting right next to
                # a required degree, inside the same all_of group and the same
                # presentation area — projection must still read "preferred"
                # off this statement, never off the area or its sibling.
                "id": "s_cert", "kind": "qualification", "subject": "candidate",
                "topic": "CPA certification", "evidence": [whole_b3],
                "importance": "preferred", "importance_evidence": [whole_b3],
                "polarity": "positive", "polarity_evidence": None,
                "proficiency": None, "proficiency_evidence": None,
                "condition_ids": [], "fact_ids": [], "unresolved": [],
            },
        ],
        "relations": {
            "groups": [
                {"id": "g1", "operator": "all_of", "members": ["s_degree", "s_cert"],
                 "evidence": [whole_b1]},
            ],
            "conditions": [], "example_sets": [],
        },
        "facts": {
            "presence": {
                "experience": {"state": "none_found", "evidence": None},
                "compensation": {"state": "none_found", "evidence": None},
                "quantities": {"state": "none_found", "evidence": None},
                "dates": {"state": "none_found", "evidence": None},
            },
            "entries": [],
        },
        "mentions": [
            {"id": "m_cpa", "surface": "CPA", "evidence": cpa_ref,
             "statement_ids": ["s_cert"], "role": "direct"},
        ],
        "areas": [
            {"id": "a1", "name": "Requirements", "kind": "credential",
             "statement_ids": ["s_degree", "s_cert"], "evidence": None},
        ],
        "block_accounting": [
            {"block_id": "b000001", "disposition": "context", "ref_ids": [],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s_degree"],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000003", "disposition": "statements", "ref_ids": ["s_cert"],
             "exclusion_reason": None, "evidence": None},
        ],
    }


@pytest.fixture
def v2_record_with_mentions() -> dict[str, Any]:
    """A quality-ineligible (offline, unaudited) record with one mention tied
    to a `preferred` statement that also sits in an `all_of` group and shares
    a presentation area with a `required` sibling statement.
    """
    return assemble(
        _emit_with_mentions(), MENTIONS_MD, document_hash=MENTIONS_DOC_HASH,
        observed_model="gpt-5.6-luna", at="2026-09-07T00:00:00+00:00",
    )


def make_emit() -> dict[str, Any]:
    """The two-block experience document: b000001 "Requirements",
    b000002 "A minimum of 8 years of experience in sales."."""
    ref_cmp = {"block_id": "b000002", "text": "A minimum of", "occurrence": 0}
    ref_val = {"block_id": "b000002", "text": "8 years", "occurrence": 0}
    whole = {"block_id": "b000002", "text": None, "occurrence": None}
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [{
            "id": "s1", "kind": "qualification", "subject": "candidate",
            "topic": "Sales experience", "evidence": [whole],
            "importance": "required",
            "importance_evidence": [{"block_id": "b000001", "text": None, "occurrence": None}],
            "polarity": "positive", "polarity_evidence": None,
            "proficiency": None, "proficiency_evidence": None,
            "condition_ids": [], "fact_ids": ["f1"], "unresolved": [],
        }],
        "relations": {"groups": [], "conditions": [], "example_sets": []},
        "facts": {
            "presence": {
                "experience": {"state": "stated", "evidence": [ref_val]},
                "compensation": {"state": "none_found", "evidence": None},
                "quantities": {"state": "none_found", "evidence": None},
                "dates": {"state": "none_found", "evidence": None},
            },
            "entries": [{
                "id": "f1", "family": "experience", "statement_ids": ["s1"],
                "condition_ids": [],
                "scope": {"kind": "overall", "evidence": None},
                "date_kind": None, "component": None,
                "evidence": {"value": [ref_val], "comparison": [ref_cmp],
                              "unit": None, "currency": None, "component": None,
                              "applicability": None},
            }],
        },
        "mentions": [],
        "areas": [{"id": "a1", "name": "Experience", "kind": "capability",
                    "statement_ids": ["s1"], "evidence": None}],
        "block_accounting": [
            {"block_id": "b000001", "disposition": "context", "ref_ids": ["s1"],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s1"],
             "exclusion_reason": None, "evidence": None},
        ],
    }


def bound_ref(text: str, occurrence: int = 0, block_id: str = "b000002") -> dict[str, Any]:
    """One materialized reference into MD, bound exactly as assembly binds it —
    a corruption test that needs a *valid* reference must not hand-type spans."""
    return resolve(
        {"block_id": block_id, "text": text, "occurrence": occurrence},
        blocks_by_id(annotate(MD)),
    )


def make_record() -> dict[str, Any]:
    return assemble(make_emit(), MD, document_hash=DOC_HASH,
                    observed_model="gpt-5.6-luna", at=AT)


@pytest.fixture
def v2_record() -> dict[str, Any]:
    """A clean assembled record over MD: every verification check passes."""
    return make_record()


# --- schema 3: headings in, verdicts out (parsing contract v3 §2.1) --------

S3_MD = "## Requirements\nA minimum of 8 years of experience in sales.\n"
S3_DOC_HASH = sha256_hex(S3_MD.encode("utf-8"))


def make_s3_emit() -> dict[str, Any]:
    """`make_emit()`'s document under the schema-3 statement shape.

    The same two blocks, with the section marked up as a real ATX heading so
    assembly has one to derive: b000001 "## Requirements" / b000002 the
    requirement line. The statement drops `importance`/`proficiency` and their
    evidence and quotes the posting's own modal phrase instead.
    """
    emit = make_emit()
    for statement in emit["statements"]:
        for verdict in ("importance", "importance_evidence",
                        "proficiency", "proficiency_evidence"):
            statement.pop(verdict)
        statement["modality_evidence"] = [
            {"block_id": "b000002", "text": "A minimum of", "occurrence": 0}
        ]
    return emit


def make_s3_record() -> dict[str, Any]:
    return assemble(make_s3_emit(), S3_MD, document_hash=S3_DOC_HASH,
                    observed_model="gpt-5.6-luna", at=AT, schema_version="3")


@pytest.fixture
def v3_record() -> dict[str, Any]:
    """A clean schema-3 record: `section_heading` derived, modality quoted."""
    return make_s3_record()


S3_MENTIONS_MD = (
    "## Requirements\n"
    "Bachelor's degree required.\n"
    "CPA certification preferred.\n"
)
S3_MENTIONS_DOC_HASH = sha256_hex(S3_MENTIONS_MD.encode("utf-8"))


def make_s3_emit_with_mentions() -> dict[str, Any]:
    """`_emit_with_mentions()` under the schema-3 statement shape.

    The degree quotes the posting's own "required"; the certification quotes
    nothing, so its `modality_evidence` is null — the two halves of the field
    the projections have to carry.
    """
    emit = _emit_with_mentions()
    for statement in emit["statements"]:
        for verdict in ("importance", "importance_evidence",
                        "proficiency", "proficiency_evidence"):
            statement.pop(verdict)
    emit["statements"][0]["modality_evidence"] = [
        {"block_id": "b000002", "text": "required", "occurrence": 0}
    ]
    emit["statements"][1]["modality_evidence"] = None
    return emit


@pytest.fixture
def v3_record_with_mentions() -> dict[str, Any]:
    """The mentions fixture under schema 3: no importance anywhere, one heading,
    one quoted modal phrase and one null."""
    return assemble(
        make_s3_emit_with_mentions(), S3_MENTIONS_MD,
        document_hash=S3_MENTIONS_DOC_HASH, observed_model="gpt-5.6-luna",
        at=AT, schema_version="3",
    )


# --- the serving fixture: two sections, two mentions, one quoted modality ---
#
# What the store and the read surface need that the fixtures above do not give
# them: a mention per statement (so `mention_rows` yields more than one row),
# two DIFFERENT headings (so a heading that reached `q claims` is provably the
# statement's own and not the document's first), and one statement whose
# modality is quoted against one whose is null.

SERVING_MD = (
    "## Requirements\n"
    "Bachelor's degree required.\n"
    "## Nice to have\n"
    "CPA certification preferred.\n"
)
SERVING_DOC_HASH = sha256_hex(SERVING_MD.encode("utf-8"))


def make_serving_emit() -> dict[str, Any]:
    """b000001 "## Requirements" / b000002 the degree line /
    b000003 "## Nice to have" / b000004 the certification line."""
    whole_b2 = {"block_id": "b000002", "text": None, "occurrence": None}
    whole_b4 = {"block_id": "b000004", "text": None, "occurrence": None}
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [
            {
                "id": "s_degree", "kind": "qualification", "subject": "candidate",
                "topic": "Bachelor's degree", "evidence": [whole_b2],
                "modality_evidence": [
                    {"block_id": "b000002", "text": "required", "occurrence": 0}
                ],
                "polarity": "positive", "polarity_evidence": None,
                "condition_ids": [], "fact_ids": [], "unresolved": [],
            },
            {
                "id": "s_cert", "kind": "qualification", "subject": "candidate",
                "topic": "CPA certification", "evidence": [whole_b4],
                "modality_evidence": None,
                "polarity": "positive", "polarity_evidence": None,
                "condition_ids": [], "fact_ids": [], "unresolved": [],
            },
        ],
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
        "mentions": [
            {"id": "m_degree", "surface": "Bachelor's degree",
             "evidence": {"block_id": "b000002", "text": "Bachelor's degree",
                          "occurrence": 0},
             "statement_ids": ["s_degree"], "role": "direct"},
            {"id": "m_cpa", "surface": "CPA",
             "evidence": {"block_id": "b000004", "text": "CPA", "occurrence": 0},
             "statement_ids": ["s_cert"], "role": "direct"},
        ],
        "areas": [
            {"id": "a1", "name": "Requirements", "kind": "credential",
             "statement_ids": ["s_degree"], "evidence": None},
            {"id": "a2", "name": "Nice to have", "kind": "credential",
             "statement_ids": ["s_cert"], "evidence": None},
        ],
        "block_accounting": [
            {"block_id": "b000001", "disposition": "context", "ref_ids": [],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s_degree"],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000003", "disposition": "context", "ref_ids": [],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000004", "disposition": "statements", "ref_ids": ["s_cert"],
             "exclusion_reason": None, "evidence": None},
        ],
    }


def _settled(record: dict[str, Any], lifecycle: str) -> dict[str, Any]:
    """The settlement overlay `runner.settle` attaches, with an agreement report
    whose demoted `kind` dimension split — which is what makes the stored quality
    block carry `sample_notes` (parsing contract v3 §4)."""
    return {
        **record,
        "settlement": {
            "semantics": "clean", "completeness": "clean", "sampling": "sampled",
            "human_review": "none", "blocking": 0, "lifecycle": lifecycle,
            "agreement": {
                "k": 3,
                "metrics": {"aligned_pairs": 7, "f1": 0.82,
                            "splits": {"kind": 2, "alternatives": 0}},
            },
        },
    }


def make_serving_record(*, lifecycle: str = "needs_review") -> dict[str, Any]:
    """A settled schema-3 record: the shape the store is asked to serve."""
    return _settled(
        assemble(make_serving_emit(), SERVING_MD, document_hash=SERVING_DOC_HASH,
                 observed_model="gpt-5.6-luna", at=AT, schema_version="3"),
        lifecycle,
    )


# --- the multi-kind serving fixture: one mention, two statements, two kinds --
#
# `project.mention_rows` emits one row per (mention, statement) pair, so a
# mention supporting statements of two kinds reaches `profile_mentions` as two
# rows that differ only in `area_kind`. Each row's heading and quoted modal
# phrase must come from ITS OWN statement — the two statements here sit under
# different headings and quote different words, so a renderer that answers from
# the mention alone reads visibly wrong.


def make_multi_kind_serving_emit() -> dict[str, Any]:
    """The serving emit with `s_cert` turned into a responsibility and the `CPA`
    mention supporting both statements — the degree one first, so a first-linked-
    statement-wins reader labels the responsibility row "Requirements"."""
    emit = make_serving_emit()
    emit["statements"][1] = {
        **emit["statements"][1],
        "kind": "responsibility",
        "modality_evidence": [{"block_id": "b000004", "text": "preferred", "occurrence": 0}],
    }
    emit["mentions"][1] = {**emit["mentions"][1], "statement_ids": ["s_degree", "s_cert"]}
    return emit


def make_multi_kind_serving_record(*, lifecycle: str = "needs_review") -> dict[str, Any]:
    """`CPA` under two kinds: qualification (heading "Requirements", quote
    "required") and responsibility (heading "Nice to have", quote "preferred")."""
    return _settled(
        assemble(make_multi_kind_serving_emit(), SERVING_MD,
                 document_hash=SERVING_DOC_HASH, observed_model="gpt-5.6-luna",
                 at=AT, schema_version="3"),
        lifecycle,
    )


# --- the headingless serving fixture: a schema-3 record with nothing to quote -
#
# `section_heading` is null for any statement whose first evidence block has no
# heading above it (schema 3 `record.schema.json`), and `modality_evidence` is
# null when the text carries no modal phrase. Both together are the shape that
# tempts a renderer back into printing the `NO_IMPORTANCE` sentinel.

HEADINGLESS_MD = "Bachelor's degree is nice.\nCPA certification.\n"
HEADINGLESS_DOC_HASH = sha256_hex(HEADINGLESS_MD.encode("utf-8"))


def make_headingless_serving_emit() -> dict[str, Any]:
    """b000001 the degree line / b000002 the certification line — no heading
    block anywhere, and neither statement quotes a modal phrase."""
    emit = make_serving_emit()
    emit["statements"] = [
        {**emit["statements"][0],
         "evidence": [{"block_id": "b000001", "text": None, "occurrence": None}],
         "modality_evidence": None},
        {**emit["statements"][1],
         "evidence": [{"block_id": "b000002", "text": None, "occurrence": None}],
         "modality_evidence": None},
    ]
    emit["mentions"] = [
        {"id": "m_degree", "surface": "Bachelor's degree",
         "evidence": {"block_id": "b000001", "text": "Bachelor's degree", "occurrence": 0},
         "statement_ids": ["s_degree"], "role": "direct"},
        {"id": "m_cpa", "surface": "CPA",
         "evidence": {"block_id": "b000002", "text": "CPA", "occurrence": 0},
         "statement_ids": ["s_cert"], "role": "direct"},
    ]
    emit["block_accounting"] = [
        {"block_id": "b000001", "disposition": "statements", "ref_ids": ["s_degree"],
         "exclusion_reason": None, "evidence": None},
        {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s_cert"],
         "exclusion_reason": None, "evidence": None},
    ]
    return emit


def make_headingless_serving_record(*, lifecycle: str = "needs_review") -> dict[str, Any]:
    """A settled schema-3 record whose every statement has a null heading and a
    null modality — nothing for the read surface to print but the absence."""
    return _settled(
        assemble(make_headingless_serving_emit(), HEADINGLESS_MD,
                 document_hash=HEADINGLESS_DOC_HASH, observed_model="gpt-5.6-luna",
                 at=AT, schema_version="3"),
        lifecycle,
    )


# --- schema 4: authorization, typed mentions, tracks (parsing contract v4) --
#
# Small hand-written sources for the spec §7 regression cases. Each builds its
# own emit over its own markdown; block ids are noted beside every source.


def whole(block_id: str) -> dict[str, Any]:
    """An emit reference to a whole block."""
    return {"block_id": block_id, "text": None, "occurrence": None}


def quote(block_id: str, text: str, occurrence: int = 0) -> dict[str, Any]:
    """An emit reference to an exact substring of a block."""
    return {"block_id": block_id, "text": text, "occurrence": occurrence}


def s4_presence(state: str = "none_found", evidence: Any = None,
                polarity: str | None = None, polarity_evidence: Any = None) -> dict[str, Any]:
    """One authorization presence entry (contract v4 §2.1)."""
    return {"state": state, "evidence": evidence,
            "polarity": polarity, "polarity_evidence": polarity_evidence}


def s4_statement(sid: str, kind: str, block_id: str, topic: str = "topic") -> dict[str, Any]:
    """A schema-3/4 statement whose evidence is one whole block."""
    return {
        "id": sid, "kind": kind, "subject": "candidate", "topic": topic,
        "evidence": [whole(block_id)], "modality_evidence": None,
        "polarity": "positive", "polarity_evidence": None,
        "condition_ids": [], "fact_ids": [], "unresolved": [],
    }


def s4_mention(mid: str, surface: str, block_id: str, statement_ids: list[str],
               mtype: str, role: str = "direct") -> dict[str, Any]:
    return {"id": mid, "surface": surface, "evidence": quote(block_id, surface),
            "statement_ids": statement_ids, "role": role, "type": mtype}


def s4_emit(statements: list[dict[str, Any]], accounting: list[tuple[str, list[str]]], *,
            mentions: list[dict[str, Any]] | None = None,
            authorization: dict[str, dict[str, Any]] | None = None,
            tracks: dict[str, Any] | None = None) -> dict[str, Any]:
    """A schema-4 emit. `accounting` is (block_id, ref_ids) per block: an empty
    ref list is a `context` block, a non-empty one a `statements` block."""
    presence: dict[str, Any] = {
        "experience": {"state": "none_found", "evidence": None},
        "compensation": {"state": "none_found", "evidence": None},
        "quantities": {"state": "none_found", "evidence": None},
        "dates": {"state": "none_found", "evidence": None},
        "sponsorship": s4_presence(),
        "citizenship": s4_presence(),
        "work_authorization": s4_presence(),
    }
    presence.update(authorization or {})
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": statements,
        "relations": {"groups": [], "conditions": [], "example_sets": [], "tracks": tracks},
        "facts": {"presence": presence, "entries": []},
        "mentions": mentions or [],
        "areas": [],
        "block_accounting": [
            {"block_id": block_id, "disposition": "statements" if refs else "context",
             "ref_ids": refs, "exclusion_reason": None, "evidence": None}
            for block_id, refs in accounting
        ],
    }


def assemble4(emit: dict[str, Any], markdown: str) -> dict[str, Any]:
    return assemble(emit, markdown, document_hash=sha256_hex(markdown.encode("utf-8")),
                    observed_model="gpt-5.6-luna", at=AT, schema_version="4")


# V1 (Visa sophomore SWE intern): b000001 "## Responsibilities" / b000002 the
# duty line / b000003 "## Additional information" / b000004 the policy line.
VISA_MD = (
    "## Responsibilities\n"
    "Self-serve deployment of Kafka clusters using Docker and Kubernetes.\n"
    "## Additional information\n"
    "Visa will not sponsor applicants for work visas in connection with this position.\n"
)
VISA_POLICY = (
    "Visa will not sponsor applicants for work visas in connection with this position."
)


def make_visa_emit() -> dict[str, Any]:
    return s4_emit(
        [s4_statement("s_duty", "responsibility", "b000002", "Kafka cluster deployment"),
         s4_statement("s_visa", "hiring_policy", "b000004", "No visa sponsorship")],
        [("b000001", []), ("b000002", ["s_duty"]), ("b000003", []), ("b000004", ["s_visa"])],
        mentions=[s4_mention("m_kafka", "Kafka", "b000002", ["s_duty"], "skill"),
                  s4_mention("m_docker", "Docker", "b000002", ["s_duty"], "skill"),
                  s4_mention("m_k8s", "Kubernetes", "b000002", ["s_duty"], "skill")],
        authorization={"sponsorship": s4_presence(
            "stated", [whole("b000004")], "negative",
            [quote("b000004", "will not sponsor")])},
    )


# V2 (Lyft SWE intern, ML, Toronto): b000001 "## Qualifications" / b000002 the
# degree line / b000003 the location line.
LYFT_MD = (
    "## Qualifications\n"
    "Pursuing a degree in Computer Science or a related field.\n"
    "This internship is based in Toronto.\n"
)


def make_lyft_emit(toronto_type: str = "location") -> dict[str, Any]:
    return s4_emit(
        [s4_statement("s_degree", "qualification", "b000002", "Computer Science degree"),
         s4_statement("s_where", "employment_constraint", "b000003", "Based in Toronto")],
        [("b000001", []), ("b000002", ["s_degree"]), ("b000003", ["s_where"])],
        mentions=[
            s4_mention("m_cs", "Computer Science", "b000002", ["s_degree"], "field_of_study"),
            s4_mention("m_toronto", "Toronto", "b000003", ["s_where"], toronto_type,
                       role="contextual"),
        ],
    )


# V3 (Figma SWE intern): b000001 heading / b000002 the selection sentence /
# b000003..b000006 the four track list items.
FIGMA_MD = (
    "## What you'll do\n"
    "When you apply, you'll tell us which areas you're most interested in:\n"
    "- Product: build features used by millions of designers.\n"
    "- Backend/Infrastructure: work on distributed systems and developer tooling.\n"
    "- Security Engineering: protect Figma and its users.\n"
    "- Open: I'm flexible and still exploring.\n"
)


def _track(tid: str, block_id: str, name: str, statement_ids: list[str],
           mention_ids: list[str] | None = None, *, open_: bool = False) -> dict[str, Any]:
    return {"id": tid, "name_evidence": [quote(block_id, name)], "evidence": [whole(block_id)],
            "open": open_, "statement_ids": statement_ids, "mention_ids": mention_ids or []}


def make_figma_emit() -> dict[str, Any]:
    return s4_emit(
        [s4_statement("s_product", "responsibility", "b000003", "Product features"),
         s4_statement("s_backend", "responsibility", "b000004", "Backend systems"),
         s4_statement("s_security", "responsibility", "b000005", "Security"),
         s4_statement("s_open", "responsibility", "b000006", "Undecided area")],
        [("b000001", []), ("b000002", []), ("b000003", ["s_product"]),
         ("b000004", ["s_backend"]), ("b000005", ["s_security"]), ("b000006", ["s_open"])],
        mentions=[
            s4_mention("m_ds", "distributed systems", "b000004", ["s_backend"], "skill"),
            s4_mention("m_tooling", "developer tooling", "b000004", ["s_backend"], "skill"),
        ],
        tracks={
            "selection": "candidate_choice",
            "selection_evidence": [quote("b000002", "When you apply, you'll tell us which areas")],
            "items": [
                _track("t_product", "b000003", "Product", ["s_product"]),
                _track("t_backend", "b000004", "Backend/Infrastructure", ["s_backend"],
                       ["m_ds", "m_tooling"]),
                _track("t_security", "b000005", "Security Engineering", ["s_security"]),
                _track("t_open", "b000006", "Open", ["s_open"], open_=True),
            ],
        },
    )


# V4 (Anduril flight software intern): b000001 heading / b000002 the line.
ANDURIL_MD = (
    "## Requirements\n"
    "Must be a U.S. Person due to required access to U.S. export-controlled "
    "information or facilities.\n"
)


def make_anduril_emit() -> dict[str, Any]:
    return s4_emit(
        [s4_statement("s_person", "hiring_policy", "b000002", "U.S. Person required")],
        [("b000001", []), ("b000002", ["s_person"])],
        authorization={"citizenship": s4_presence(
            "stated", [quote("b000002", "Must be a U.S. Person due to required access to "
                                        "U.S. export-controlled information or facilities")],
            "positive", [quote("b000002", "Must be a U.S. Person")])},
    )


# V5 (synthetic): only a work-authorization line. b000001 heading / b000002.
WORK_AUTH_MD = "## Requirements\nMust be authorized to work in the US.\n"


def make_work_auth_emit() -> dict[str, Any]:
    return s4_emit(
        [s4_statement("s_auth", "hiring_policy", "b000002", "US work authorization")],
        [("b000001", []), ("b000002", ["s_auth"])],
        authorization={"work_authorization": s4_presence("stated", [whole("b000002")])},
    )


# V6 (synthetic): a sponsorship line with a modal grant. b000001 / b000002.
MAY_SPONSOR_MD = "## Additional information\nSponsorship may be available for this role.\n"


def make_may_sponsor_emit(polarity: str = "positive") -> dict[str, Any]:
    return s4_emit(
        [s4_statement("s_sponsor", "hiring_policy", "b000002", "Sponsorship available")],
        [("b000001", []), ("b000002", ["s_sponsor"])],
        authorization={"sponsorship": s4_presence(
            "stated", [whole("b000002")], polarity, [quote("b000002", "may be available")])},
    )


# --- the C02/C07 English-footer shape --------------------------------------

FOOTER_MD = (
    "Responsibilities\n"
    "Own the quarterly close.\n"
    "This position requires the incumbent to have a sufficient knowledge of English "
    "to have professional verbal and written exchanges.\n"
)
FOOTER_DOC_HASH = sha256_hex(FOOTER_MD.encode("utf-8"))


def _footer_emit() -> dict[str, Any]:
    """b000001 "Responsibilities" / b000002 "Own the quarterly close." /
    b000003 the English-proficiency footer, thrown away as EEO boilerplate."""
    whole_b2 = {"block_id": "b000002", "text": None, "occurrence": None}
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [{
            "id": "s1", "kind": "responsibility", "subject": "candidate",
            "topic": "Quarterly close", "evidence": [whole_b2],
            "importance": None, "importance_evidence": None,
            "polarity": "positive", "polarity_evidence": None,
            "proficiency": None, "proficiency_evidence": None,
            "condition_ids": [], "fact_ids": [], "unresolved": [],
        }],
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
        "block_accounting": [
            {"block_id": "b000001", "disposition": "context", "ref_ids": [],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s1"],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000003", "disposition": "excluded", "ref_ids": [],
             "exclusion_reason": "eeo", "evidence": None},
        ],
    }


@pytest.fixture
def v2_footer_record() -> dict[str, Any]:
    """The audit's C02/C07 record: a real English requirement excluded as EEO.

    The footer line carries exactly one word from the tripwire's vocabulary —
    "requires" — which is why this fixture, not a hand-written string, is what
    keeps the warning honest about the class it was written for.
    """
    return assemble(_footer_emit(), FOOTER_MD, document_hash=FOOTER_DOC_HASH,
                    observed_model="gpt-5.6-luna", at=AT)
