"""Integration: what validator/20 changes about a drain (parsing contract v3).

Two things, and they are the same thing seen from either end. Sampling stops
being adjudication and becomes monitoring — only the deterministic 5% slot
(`JOB_HUNTER_L2_AUDIT_MOD`) takes extra samples, and a reprompted first pass
takes none — and the cohort the 5% slot does take publishes what it split on,
in `quality.sample_notes`, instead of parking the document.

The evidence for dropping the reprompt branch is the 2026-09-22 analysis: the
373-of-1,000 "incomplete cohort" review class was nothing but exhausted sample
budgets on documents the reprompt predictor had escalated. Expected effect,
spec §5: 2-3x fewer engine calls per document.

Everything else about the drain — ladder, breaker, caps, catch-up, the audit
phase — is `test_runner.py`'s and `test_runner_v2.py`'s.
"""

from __future__ import annotations

import copy
from typing import Any

import psycopg

from jobhunter.archive.base import ArchiveStore
from jobhunter.hashing import sha256_hex
from jobhunter.l2.engines import EngineResult, EngineTransportError
from jobhunter.l2.runner import run
from tests.l2.test_runner import _seed_doc, store  # noqa: F401
from tests.l2.test_runner_v2 import (
    MODEL,
    AuditingEngine,
    attempts_in,
    clean_audit,
    emit_of,
    result,
    row_of,
    seed_case,
    unparseable_audit,
    v2_settings,
)

Conn = psycopg.Connection[dict[str, Any]]

# C01's document hash starts 5ae491ea: 1521353706 mod 20 == 2, so the default
# audit mod MISSES this document and mod 1 always takes it.
OFF_SLOT = {}
ON_SLOT = {"JOB_HUNTER_L2_AUDIT_MOD": "1"}


def _unparseable() -> EngineResult:
    """A slot-1 answer that is not JSON: the ladder feeds the error back and
    the next attempt is a REPROMPT, which used to escalate to k=3."""
    return EngineResult("not json", MODEL, 4, 1, 0.0)


def _kind_variant() -> dict[str, Any]:
    """A second reading of C01 that differs only in statement kind — the
    single largest review class in the 2026-09-22 sample (172 pairs), and one
    the verifier accepts on both sides: the only statement field schema 3 lets
    code re-derive is `section_heading`, and both kinds derive the same one
    from the same evidence span (parsing contract v3 §2.1)."""
    emit = copy.deepcopy(emit_of("C01"))
    emit["statements"][0]["kind"] = "employment_constraint"
    return emit


def test_a_reprompted_first_pass_takes_no_extra_samples(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Spec §5: the retry contract (v10) already makes a reprompt an edit of
    the prior candidate, so a document that needed one is not evidence of a
    cohort worth paying for."""
    seed_case(pg, "C01")
    engine = AuditingEngine([_unparseable(), result(emit_of("C01"))], clean_audit)
    summary = run(v2_settings(**OFF_SLOT), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1

    # two attempts, both in slot 1: the ladder ran, the sampler did not
    assert sorted(a.sample_slot for a in attempts_in(store)) == [1, 1]
    row = row_of(pg)
    assert row["status"] == "validated" and row["k"] == 1
    assert row["agreement"] is None
    assert "sample_notes" not in row["profile"]["quality"]


def test_the_deterministic_audit_slot_still_takes_k_samples(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The 5% slot is the whole of k-sampling now, and it is unchanged."""
    seed_case(pg, "C01")
    good = result(emit_of("C01"))
    engine = AuditingEngine([good, good, good], clean_audit)
    summary = run(v2_settings(**ON_SLOT), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1

    assert sorted(a.sample_slot for a in attempts_in(store)) == [1, 2, 3]
    row = row_of(pg)
    assert row["status"] == "validated" and row["k"] == 3
    assert row["agreement"]["failures"] == []


def test_the_audit_slot_samples_even_when_its_first_pass_was_reprompted(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The branch that went away is the reprompt one, not the slot one: a
    document ON the 5% slot is sampled whatever its ladder did."""
    seed_case(pg, "C01")
    good = result(emit_of("C01"))
    engine = AuditingEngine([_unparseable(), good, good, good], clean_audit)
    summary = run(v2_settings(**ON_SLOT), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1
    assert sorted(a.sample_slot for a in attempts_in(store)) == [1, 1, 2, 3]
    assert row_of(pg)["k"] == 3


def test_a_kind_split_cohort_settles_and_serves_its_sample_notes(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The headline of validator/20 on a real drain: two of three samples call
    C01's requirement an employment constraint and one calls it a
    qualification. Under 19 that parked the document; under 20 it settles and
    the blob says what the samples split on."""
    seed_case(pg, "C01")
    variant = result(_kind_variant())
    engine = AuditingEngine([result(emit_of("C01")), variant, variant], clean_audit)
    summary = run(v2_settings(**ON_SLOT), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1

    row = row_of(pg)
    assert row["status"] == "validated" and row["k"] == 3
    assert row["agreement"]["failures"] == []
    splits = row["agreement"]["metrics"]["splits"]
    assert splits["kind"] > 0

    notes = row["profile"]["quality"]["sample_notes"]
    assert notes["k"] == 3
    assert notes["splits"] == {"kind": splits["kind"]}
    assert notes["aligned_pairs"] == row["agreement"]["metrics"]["aligned_pairs"]


def test_an_incomplete_cohort_settles_and_notes_what_it_could_not_measure(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Spec §3 and §5 on a real drain: slot 1 produced an assembled, verified,
    candidate and slots 2 and 3 never came back. Under 19 that was the
    373-of-1,000 review class; under 20 the document publishes and the blob
    says how many samples it asked for against how many arrived.

    Each sample slot gets one transport retry, so four failures empty both. The
    auditor is scripted to error so that validator/18's whole-record
    adjudication — a clean audit of the medoid outranking a missing sample — is
    out of the way and what is being read is the settlement rule itself. That
    is also the production shape: every one of the 4,338 documents parked on
    `sample_failed` alone carries `semantics: not_checked`.
    """
    seed_case(pg, "C01")
    lost = [EngineTransportError("codex flake") for _ in range(4)]
    engine = AuditingEngine([result(emit_of("C01")), *lost], unparseable_audit)
    summary = run(v2_settings(**ON_SLOT), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1

    assert sorted(a.sample_slot for a in attempts_in(store)) == [1, 2, 2, 3, 3]
    row = row_of(pg)
    assert row["status"] == "validated" and row["k"] == 3
    assert row["agreement"]["failures"] == ["sample_failed"]

    notes = row["profile"]["quality"]["sample_notes"]
    assert (notes["requested"], notes["arrived"]) == (3, 1)
    assert notes["splits"] == {}
    assert row["profile"]["quality"]["sampling"] == "incomplete"


def test_a_polarity_split_still_parks_the_document_after_sampling(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The gate that survived, end to end: two samples read C01's requirement
    as negated, the third does not, and the document parks for review.

    The auditor is scripted to error so that validator/16 adjudication — a
    clean full-source audit outranking sampling variance — is out of the way
    and what is being read is the gate itself.
    """
    seed_case(pg, "C01")
    flipped = copy.deepcopy(emit_of("C01"))
    flipped["statements"][0]["polarity"] = "negative"
    flipped["statements"][0]["polarity_evidence"] = None
    variant = result(flipped)
    engine = AuditingEngine([result(emit_of("C01")), variant, variant], unparseable_audit)
    run(v2_settings(**ON_SLOT), pg, store, engine=engine, max_docs=10, max_usd=5.0)

    row = row_of(pg)
    assert row["status"] == "needs_review"
    assert row["agreement"]["failures"] == ["negation"]
    assert row["profile"]["quality"]["sampling"] == "disagreement"


# --- validator/20, 2026-09-28 amendment: the gates compare meaning -----------
# The 2026-09-28 review-queue analysis found both surviving gates parking
# documents on labels: a hedge read as a denial, one hiring policy framed from
# opposite ends, one salary with and without its period tag. Sean approved
# (2026-09-28) negation reading only `negative` on the statement kinds a reader
# acts on, and the numeric gate comparing numbers and their dimension. These
# drive the real drain over one posting assembled from the sentences the
# analysis named: slot 1 reads it as below, slots 2 and 3 move exactly one
# reading. The auditor errors, so validator/16 adjudication is out of the way
# and what settles each document is the gate itself.

POSTING = "\n".join([
    "## Senior Software Engineer, Trading Systems",
    "We build the exchange connectivity layer for a global trading firm.",
    "## What we look for",
    "- 5+ years of professional software engineering experience, or 8+ years for the Staff"
    " level.",
    "- We don't expect you to know OCaml; we will teach you here.",
    "## Compensation",
    "The base salary range for this role is $240,000–$315,000 USD/year.",
    "## Working here",
    "We hire for on-site roles only; applications for remote work will not be considered.",
    "This role may require travel.",
])


def _ref(block: str, text: str | None) -> dict[str, Any]:
    return {"block_id": block, "text": text, "occurrence": None if text is None else 0}


def _statement(sid: str, kind: str, subject: str, topic: str, block: str, *,
               polarity: str = "positive", polarity_evidence: str | None = None,
               modality: str | None = None, fact_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "id": sid, "kind": kind, "subject": subject, "topic": topic,
        "evidence": [_ref(block, None)],
        "modality_evidence": [_ref(block, modality)] if modality else None,
        "polarity": polarity,
        "polarity_evidence": [_ref(block, polarity_evidence)] if polarity_evidence else None,
        "condition_ids": [], "fact_ids": list(fact_ids), "unresolved": [],
    }


def _accounted(block: str, disposition: str, *ref_ids: str,
               reason: str | None = None) -> dict[str, Any]:
    return {"block_id": block, "disposition": disposition, "ref_ids": list(ref_ids),
            "exclusion_reason": reason, "evidence": None}


def _posting_emit() -> dict[str, Any]:
    """Slot 1's schema-3 reading of `POSTING` — every reading the right one."""
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [
            _statement("s_years", "qualification", "candidate",
                       "Professional software engineering experience", "b000004",
                       fact_ids=("f_years",)),
            _statement("s_ocaml", "qualification", "candidate", "OCaml knowledge", "b000005",
                       polarity="negative", polarity_evidence="We don't expect you to"),
            _statement("s_pay", "compensation_statement", "role", "Base salary range",
                       "b000007", fact_ids=("f_pay",)),
            _statement("s_onsite", "hiring_policy", "candidate", "On-site roles only",
                       "b000009"),
            _statement("s_travel", "employment_constraint", "role", "Travel", "b000010",
                       modality="may require"),
        ],
        "relations": {"groups": [], "conditions": [], "example_sets": []},
        "facts": {
            "presence": {
                "experience": {"state": "stated", "evidence": [_ref("b000004", "5+ years")]},
                "compensation": {"state": "stated",
                                 "evidence": [_ref("b000007", "$240,000–$315,000")]},
                "quantities": {"state": "none_found", "evidence": None},
                "dates": {"state": "none_found", "evidence": None},
            },
            "entries": [
                {"id": "f_years", "family": "experience", "statement_ids": ["s_years"],
                 "condition_ids": [],
                 "scope": {"kind": "domain", "evidence": [
                     _ref("b000004", "professional software engineering")]},
                 "date_kind": None, "component": None,
                 "evidence": {"value": [_ref("b000004", "5+ years")], "comparison": None,
                              "unit": None, "currency": None, "component": None,
                              "applicability": None}},
                {"id": "f_pay", "family": "compensation", "statement_ids": ["s_pay"],
                 "condition_ids": [], "scope": None, "date_kind": None, "component": "base",
                 "evidence": {"value": [_ref("b000007", "$240,000–$315,000")],
                              "comparison": None, "unit": [_ref("b000007", "/year")],
                              "currency": [_ref("b000007", "USD")],
                              "component": [_ref("b000007", "base salary")],
                              "applicability": None}},
            ],
        },
        "mentions": [{"id": "m_ocaml", "surface": "OCaml", "evidence": _ref("b000005", "OCaml"),
                      "statement_ids": ["s_ocaml"], "role": "direct"}],
        "areas": [
            {"id": "a_fit", "name": "What we look for", "kind": "capability",
             "statement_ids": ["s_years", "s_ocaml"], "evidence": [_ref("b000003", None)]},
            {"id": "a_terms", "name": "Compensation and working here", "kind": "constraint",
             "statement_ids": ["s_pay", "s_onsite", "s_travel"],
             "evidence": [_ref("b000006", None)]},
        ],
        "block_accounting": [
            _accounted("b000001", "context"),
            _accounted("b000002", "excluded", reason="employer_description"),
            _accounted("b000003", "context"),
            _accounted("b000004", "statements", "s_years"),
            _accounted("b000004", "facts", "f_years"),
            _accounted("b000005", "statements", "s_ocaml"),
            _accounted("b000006", "context"),
            _accounted("b000007", "statements", "s_pay"),
            _accounted("b000007", "facts", "f_pay"),
            _accounted("b000008", "context"),
            _accounted("b000009", "statements", "s_onsite"),
            _accounted("b000010", "statements", "s_travel"),
        ],
    }


def _reread(sid: str, **changes: Any) -> dict[str, Any]:
    """`_posting_emit` with one statement read differently."""
    emit = _posting_emit()
    next(s for s in emit["statements"] if s["id"] == sid).update(changes)
    return emit


def _drain_posting(pg: Conn, archive: ArchiveStore, variant: dict[str, Any]) -> dict[str, Any]:
    _seed_doc(pg, dh=sha256_hex(POSTING.encode("utf-8")), markdown=POSTING,
              uid="gh:x:2026-09-28")
    engine = AuditingEngine([result(_posting_emit()), result(variant), result(variant)],
                            unparseable_audit)
    run(v2_settings(**ON_SLOT), pg, archive, engine=engine, max_docs=10, max_usd=5.0)
    row = row_of(pg)
    assert row["k"] == 3
    return row


def test_a_may_require_travel_hedge_split_settles_validated_end_to_end(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """"This role may require travel" read as a hedge by two samples and as an
    assertion by the first. `ambiguous` is not a negation; the split is served
    as a polarity note."""
    hedge = _reread("s_travel", polarity="ambiguous",
                    polarity_evidence=[_ref("b000010", "may")])
    row = _drain_posting(pg, store, hedge)
    assert row["status"] == "validated"
    assert row["agreement"]["failures"] == []
    assert row["profile"]["quality"]["sample_notes"]["splits"]["polarity"] == 2


def test_an_on_site_remote_framing_split_in_hiring_policy_settles_end_to_end(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """"On-site roles only" against "remote work will not be considered": one
    hiring policy from two ends, which a reader acts on no differently."""
    framed = _reread("s_onsite", polarity="negative", topic="Remote work not considered",
                     polarity_evidence=[_ref("b000009", "will not be considered")])
    row = _drain_posting(pg, store, framed)
    assert row["status"] == "validated"
    assert row["agreement"]["failures"] == []
    assert row["profile"]["quality"]["sample_notes"]["splits"]["polarity"] == 2


def test_an_ocaml_negation_split_read_positive_still_parks_end_to_end(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """"We don't expect you to know OCaml" read as an OCaml requirement — the
    real flip, on a qualification, and the reason the gate exists."""
    required = _reread("s_ocaml", polarity="positive", polarity_evidence=None)
    row = _drain_posting(pg, store, required)
    assert row["status"] == "needs_review"
    assert row["agreement"]["failures"] == ["negation"]
    assert row["profile"]["quality"]["sampling"] == "disagreement"


def test_a_salary_period_tag_split_settles_validated_end_to_end(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """"$240,000–$315,000 USD/year" with and without its period anchor: the
    same two amounts, one tag apart."""
    untagged = _posting_emit()
    untagged["facts"]["entries"][1]["evidence"]["unit"] = None
    row = _drain_posting(pg, store, untagged)
    assert row["status"] == "validated"
    assert row["agreement"]["failures"] == []
    assert row["agreement"]["numeric_conflicts"] == 0
    assert row["profile"]["quality"]["sample_notes"]["splits"]["numeric_tags"] == 4


def test_five_against_eight_years_still_splits_the_numeric_gate_end_to_end(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """"5+ years ..., or 8+ years for the Staff level": two samples link the
    requirement to the other number in the same bullet. A different number is
    a misread, and it parks."""
    staff = _posting_emit()
    staff["facts"]["entries"][0]["evidence"]["value"] = [_ref("b000004", "8+ years")]
    staff["facts"]["presence"]["experience"]["evidence"] = [_ref("b000004", "8+ years")]
    row = _drain_posting(pg, store, staff)
    assert row["status"] == "needs_review"
    assert row["agreement"]["failures"] == ["numeric_conflict"]
    assert row["profile"]["quality"]["sampling"] == "disagreement"
