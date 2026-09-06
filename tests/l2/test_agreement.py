"""Agreement gate tests (spec §4.5): alignment by span-overlap Jaccard ≥ 0.5
(greedy, one-to-one), mean pairwise claim-set F1 ≥ 0.80, importance agreement
on required claims ≥ 0.90, zero negation disagreements, medoid chosen whole
with a deterministic tie-break by sample slot."""

from __future__ import annotations

from typing import Any

import pytest

from jobhunter.l2.agreement import (
    F1_MIN,
    IMPORTANCE_MIN,
    JACCARD_MIN,
    agree,
)


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
