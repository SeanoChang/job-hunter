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
from jobhunter.l2.engines import EngineResult
from jobhunter.l2.runner import run
from tests.l2.test_runner import store  # noqa: F401
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
    the verifier accepts on both sides because both kinds carry importance."""
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
