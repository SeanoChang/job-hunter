"""Agreement gate tests (spec §4.5): alignment by span-overlap Jaccard ≥ 0.5
(greedy, one-to-one), mean pairwise claim-set F1 ≥ 0.80, importance agreement
on required claims ≥ 0.90, zero negation disagreements, medoid chosen whole
with a deterministic tie-break by sample slot.

Plus validator/18's dispute sets: what a disagreeing cohort actually disagrees
ABOUT, expressed in the medoid's own id namespace plus document-wide block ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from jobhunter.l2.agreement import (
    F1_MIN,
    IMPORTANCE_MIN,
    JACCARD_MIN,
    Dispute,
    agree,
    cohort_hook,
    dispute_set,
)
from jobhunter.l2.v2.serve import claim_index


def claim(
    span: tuple[int, int],
    *,
    importance: str = "required",
    negated: bool = False,
    cid: str = "c",
) -> dict[str, Any]:
    return {
        "id": cid,
        "importance": importance,
        "negated": negated,
        "quote": {"span": list(span), "text": "x" * (span[1] - span[0])},
    }


def profile(*claims: dict[str, Any]) -> dict[str, Any]:
    return {"demand_profile": {"areas": [{"id": "a", "claims": list(claims)}]}}


def test_thresholds_are_the_spec_values() -> None:
    assert JACCARD_MIN == 0.5
    assert F1_MIN == 0.80
    assert IMPORTANCE_MIN == 0.90


def test_identical_samples_pass_with_perfect_scores() -> None:
    s = profile(claim((0, 100)), claim((200, 300), importance="preferred"))
    r = agree([s, s, s])
    assert r.passed
    assert r.report["mean_f1"] == 1.0
    assert r.report["negation_disagreements"] == 0


def test_fewer_than_two_samples_is_a_usage_error() -> None:
    with pytest.raises(ValueError):
        agree([profile(claim((0, 10)))])


def test_low_f1_fails() -> None:
    # Sample B misses most of A's claims: F1 collapses below 0.80.
    a = profile(claim((0, 10)), claim((20, 30)), claim((40, 50)), claim((60, 70)))
    b = profile(claim((0, 10)))
    r = agree([a, b])
    assert not r.passed
    assert r.report["mean_f1"] < F1_MIN
    assert "f1" in r.report["failures"]


def test_a_single_negation_split_fails_even_at_perfect_f1() -> None:
    a = profile(claim((0, 100), negated=False))
    b = profile(claim((0, 100), negated=True))
    r = agree([a, b])
    assert not r.passed
    assert r.report["negation_disagreements"] == 1
    assert "negation" in r.report["failures"]


def test_importance_disagreement_on_required_claims_fails_below_threshold() -> None:
    # One aligned pair, disagreeing importance where one side says required:
    # agreement 0/1 < 0.90.
    a = profile(claim((0, 100), importance="required"))
    b = profile(claim((0, 100), importance="preferred"))
    r = agree([a, b])
    assert not r.passed
    assert r.report["required_importance_agreement"] == 0.0
    assert "importance" in r.report["failures"]


def test_contextual_importance_disagreement_is_not_gated() -> None:
    # Neither side required: the importance gate does not apply.
    a = profile(claim((0, 100), importance="preferred"))
    b = profile(claim((0, 100), importance="contextual"))
    r = agree([a, b])
    assert r.passed


def test_jaccard_boundary_aligns_at_exactly_half() -> None:
    # [0,100) vs [50,150): intersection 50, union 150 -> 1/3, NOT aligned.
    # [0,100) vs [0,200): intersection 100, union 200 -> 0.5, aligned.
    a = profile(claim((0, 100)))
    b = profile(claim((0, 200)))
    r = agree([a, b])
    assert r.passed and r.report["mean_f1"] == 1.0
    c = profile(claim((50, 150)))
    r2 = agree([a, c])
    assert r2.report["mean_f1"] == 0.0


def test_greedy_alignment_is_one_to_one() -> None:
    # Two claims in A both overlap B's single claim; only one may align, so
    # F1 = 2*1/(2+1) = 2/3.
    a = profile(claim((0, 100), cid="a1"), claim((0, 100), cid="a2"))
    b = profile(claim((0, 100), cid="b1"))
    r = agree([a, b])
    assert abs(r.report["mean_f1"] - 2 / 3) < 1e-9


def test_missing_span_never_aligns() -> None:
    broken = {
        "demand_profile": {"areas": [{"id": "a", "claims": [
            {"id": "c", "importance": "required", "negated": False, "quote": {"text": "x"}}
        ]}]}
    }
    r = agree([broken, profile(claim((0, 10)))])
    assert r.report["mean_f1"] == 0.0


# ---- medoid ---------------------------------------------------------------


def test_medoid_is_the_sample_closest_to_the_others() -> None:
    common = [claim((0, 100)), claim((200, 300))]
    a = profile(*common, claim((400, 500)))
    b = profile(*common, claim((400, 500)))
    outlier = profile(common[0])
    r = agree([outlier, a, b])
    assert r.medoid in (1, 2)
    assert r.medoid == 1  # tie between a and b -> lowest slot wins


def test_medoid_tie_break_is_the_lowest_slot() -> None:
    s = profile(claim((0, 100)))
    r = agree([s, s, s])
    assert r.medoid == 0


# ---- dispute sets (validator/18) -------------------------------------------
# The plan's Policy rules 1 and 2
# (docs/superpowers/plans/2026-09-13-repair-and-scoped-adjudication.md): every
# medoid statement left unaligned in ANY medoid-involving pair is disputed, in
# the MEDOID's id namespace, plus every block a sibling statement cites and no
# medoid statement does. The samples here are the real thing the fold compares
# — `serve.profile_of`'s two relevant keys, the claim index built by
# `serve.claim_index` over hand-built v2 statements.


def ref(block: str, span: tuple[int, int]) -> dict[str, Any]:
    return {
        "block_id": block,
        "text": "x" * (span[1] - span[0]),
        "span": [span[0], span[1]],
        "occurrence": 0,
    }


def statement(
    sid: str,
    span: tuple[int, int],
    *,
    block: str = "b1",
    importance: str = "required",
    polarity: str = "positive",
) -> dict[str, Any]:
    return {
        "id": sid,
        "kind": "qualification",
        "subject": "candidate",
        "topic": sid,
        "evidence": [ref(block, span)],
        "importance": importance,
        "importance_evidence": None,
        "polarity": polarity,
        "polarity_evidence": None,
        "proficiency": None,
        "proficiency_evidence": None,
        "condition_ids": [],
        "fact_ids": [],
        "unresolved": [],
    }


def sample(*statements: dict[str, Any]) -> dict[str, Any]:
    """One v2 sample as settlement sees it: the stored slice's `statements` and
    the claim index the gate aligns."""
    record = {
        "statements": list(statements),
        "mentions": [],
        "facts": {"entries": []},
    }
    return {"statements": record["statements"], "demand_profile": claim_index(record)}


@dataclass(frozen=True)
class _Slot:
    """The two attributes `cohort_hook` reads off an attempt, plus its record."""

    sample_slot: int
    attempt_key: str
    record: dict[str, Any] | None


def test_an_agreeing_pair_disputes_nothing() -> None:
    s = sample(statement("m1", (0, 100)))
    assert dispute_set(s, [s]) == Dispute(frozenset(), frozenset(), True)


def test_a_medoid_statement_no_sibling_aligns_is_disputed() -> None:
    medoid = sample(statement("m1", (0, 100)), statement("m2", (200, 300)))
    close = sample(statement("s1", (0, 100)), statement("s2", (200, 300)))
    far = sample(statement("t1", (0, 100)))
    # m2 aligns against `close` and against nothing in `far`: unaligned in ANY
    # medoid-involving pair is the rule, so it is disputed.
    d = dispute_set(medoid, [close, far])
    assert d.statement_ids == frozenset({"m2"})
    assert d.block_ids == frozenset()
    assert d.scoped is True


def test_a_block_only_a_sibling_cites_is_disputed() -> None:
    """Rule 2: the omission side of an f1 statement-set disagreement. Every
    medoid statement aligns, so rule 1 names nothing — what the samples split
    on is source the medoid never cited."""
    medoid = sample(statement("m1", (0, 100), block="b1"))
    sibling = sample(
        statement("s1", (0, 100), block="b1"), statement("s2", (400, 500), block="b7")
    )
    d = dispute_set(medoid, [sibling])
    assert d.statement_ids == frozenset()
    assert d.block_ids == frozenset({"b7"})


def test_statement_ids_are_never_compared_across_samples() -> None:
    """Refutation 7b354fd4: ids are per-sample. The sibling carries both of the
    medoid's id strings, so naive set-difference over id strings disputes
    nothing; alignment is by span, and the medoid's own `s1` — which aligns
    with nothing — is what is actually in dispute."""
    medoid = sample(statement("s1", (0, 100), block="b1"), statement("s2", (200, 300)))
    sibling = sample(statement("s1", (200, 300)), statement("s2", (900, 1000), block="b9"))
    d = dispute_set(medoid, [sibling])
    assert d.statement_ids == frozenset({"s1"})
    assert d.block_ids == frozenset({"b9"})


def test_a_claim_that_cannot_be_attributed_leaves_the_dispute_unscoped() -> None:
    """A claim index with no id namespace (v1's areas) cannot express a dispute
    set at all. Saying so is the conservative answer — an empty dispute would
    read as "nothing is disputed" and clear every finding."""
    v1 = {
        "demand_profile": {
            "areas": [
                {"claims": [claim((0, 100))]},
            ]
        }
    }
    other = {"demand_profile": {"areas": [{"claims": [claim((900, 1000))]}]}}
    d = dispute_set(v1, [other])
    assert d.scoped is False
    assert d.statement_ids == frozenset() and d.block_ids == frozenset()


def test_the_cohort_hook_carries_a_dispute_only_when_the_gate_fails() -> None:
    medoid = sample(statement("m1", (0, 100)))
    split = sample(statement("s1", (0, 100), polarity="negative"))
    hook = cohort_hook(lambda a: a.record)

    passed, key, report, dispute = hook(
        [_Slot(1, "k1", medoid), _Slot(2, "k2", medoid)], 2
    )
    assert passed and dispute is None  # nothing settled, nothing to scope

    passed, key, report, dispute = hook(
        [_Slot(1, "k1", medoid), _Slot(2, "k2", split)], 2
    )
    # a polarity split aligns perfectly: the dispute set is EMPTY and the gate
    # dimension (rule 3) is what carries this failure
    assert not passed and report["failures"] == ["negation"]
    assert dispute == Dispute(frozenset(), frozenset(), True)

    passed, key, report, dispute = hook([_Slot(1, "k1", medoid), _Slot(2, "k2", None)], 2)
    assert not passed and "sample_failed" in report["failures"]
    assert dispute is None  # no comparable cohort exists to dispute


def test_the_dispute_is_computed_against_the_medoid_the_gate_chose() -> None:
    """The medoid is the sample the audit ran on, so the namespace has to be
    its own: a run whose medoid is slot 2 disputes slot 2's ids."""
    outlier = sample(statement("o1", (0, 100)), statement("o2", (600, 700), block="b6"))
    common = sample(statement("c1", (0, 100)), statement("c2", (200, 300), block="b2"))
    hook = cohort_hook(lambda a: a.record)
    passed, key, _report, dispute = hook(
        [_Slot(1, "k1", outlier), _Slot(2, "k2", common), _Slot(3, "k3", common)], 3
    )
    assert not passed and key == "k2"  # the medoid is one of the two agreeing samples
    assert dispute is not None
    assert dispute.statement_ids == frozenset({"c2"})
    assert dispute.block_ids == frozenset({"b6"})
