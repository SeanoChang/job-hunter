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
    importance: str | None = "required",
    polarity: str = "positive",
    kind: str = "qualification",
    subject: str = "candidate",
    fact_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "id": sid,
        "kind": kind,
        "subject": subject,
        "topic": sid,
        "evidence": [ref(block, span)],
        "importance": importance,
        "importance_evidence": None,
        "polarity": polarity,
        "polarity_evidence": None,
        "proficiency": None,
        "proficiency_evidence": None,
        "condition_ids": [],
        "fact_ids": list(fact_ids),
        "unresolved": [],
    }


def group(gid: str, operator: str, members: list[str]) -> dict[str, Any]:
    return {"id": gid, "operator": operator, "members": members,
            "evidence": [ref("b1", (0, 4))]}


def mention(mid: str, surface: str, span: tuple[int, int], sids: list[str]) -> dict[str, Any]:
    return {"id": mid, "surface": surface, "evidence": ref("b1", span),
            "statement_ids": sids, "role": "direct", "normalized_key": surface.casefold()}


def experience(fid: str, span: tuple[int, int], *, months: int, sids: list[str],
               scope: str = "overall") -> dict[str, Any]:
    """One derived experience fact — the shape `facts.py` leaves behind."""
    return {
        "id": fid, "family": "experience", "statement_ids": sids, "condition_ids": [],
        "scope": {"kind": scope, "evidence": None}, "date_kind": None, "component": None,
        "evidence": {"value": [ref("b1", span)], "comparison": None, "unit": None,
                     "currency": None, "component": None, "applicability": None},
        "derived": {"state": "parsed", "money": None, "date": None,
                    "quantity": {"dimension": "duration", "comparison": "gte",
                                 "min_value": months, "max_value": None,
                                 "inclusive_min": True, "inclusive_max": None,
                                 "unit": "month"}},
    }


def sample(
    *statements: dict[str, Any],
    groups: list[dict[str, Any]] | None = None,
    mentions: list[dict[str, Any]] | None = None,
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One v2 sample as settlement sees it: the stored slice's `statements` and
    the claim index the gate aligns."""
    record = {
        "statements": list(statements),
        "relations": {"groups": groups or [], "conditions": [], "example_sets": []},
        "mentions": mentions or [],
        "facts": {"entries": entries or []},
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


# ---- the v2 semantic dimensions (validator/19) ------------------------------
# Spec §6: "The v2 comparator includes aligned statement kind, importance,
# polarity, scoped fact values, entity links, and alternatives." Through
# validator/18 the gate read three fields off a claim — span, importance,
# negated — so two records that disagreed about everything else scored F1 1.0
# and certified themselves (external review finding 6). These are dimension
# checks on ALIGNED pairs, beside importance and negation, never folded into F1.


def test_an_all_of_any_of_split_fails_at_perfect_f1() -> None:
    """The refutation case itself: two records identical except the operator
    over the same two statements. "A and B" and "A or B" are different jobs."""
    def with_operator(operator: str) -> dict[str, Any]:
        return sample(
            statement("s1", (0, 100)), statement("s2", (200, 300)),
            groups=[group("g1", operator, ["s1", "s2"])],
        )

    r = agree([with_operator("all_of"), with_operator("any_of")])
    assert r.report["mean_f1"] == 1.0  # every claim aligns ...
    assert r.report["alternatives_disagreements"] == 2  # ... and they still disagree
    assert "alternatives" in r.report["failures"] and r.passed is False


def test_a_group_one_sample_never_emitted_is_a_disagreement() -> None:
    """The absence side: an `any_of` route in one sample and no relation at all
    in the other reads as two unconditional requirements — spec §3's
    "Membership in a route does not make its members separately universal"."""
    grouped = sample(statement("s1", (0, 100)), statement("s2", (200, 300)),
                     groups=[group("g1", "any_of", ["s1", "s2"])])
    flat = sample(statement("s1", (0, 100)), statement("s2", (200, 300)))
    r = agree([grouped, flat])
    assert r.report["alternatives_disagreements"] == 2
    assert "alternatives" in r.report["failures"] and r.passed is False


def test_a_kind_flip_fails() -> None:
    """A duty read as a prerequisite is the C12 conflation; the claim sets align
    perfectly because both samples cite the same sentence."""
    a = sample(statement("s1", (0, 100)))
    b = sample(statement("s1", (0, 100), kind="responsibility", importance=None))
    r = agree([a, b])
    assert r.report["mean_f1"] == 1.0
    assert r.report["kind_disagreements"] == 1
    assert "kind" in r.report["failures"] and r.passed is False


def test_a_scoped_value_disagreement_fails() -> None:
    """144 months vs 12 months off the same cited span: the numbers are code-
    derived, so a split here is a misread of the document, not style."""
    def with_months(months: int) -> dict[str, Any]:
        return sample(
            statement("s1", (0, 100), fact_ids=("f1",)),
            entries=[experience("f1", (0, 100), months=months, sids=["s1"])],
        )

    r = agree([with_months(144), with_months(12)])
    assert r.report["mean_f1"] == 1.0
    assert r.report["scoped_value_disagreements"] == 2  # the statement and the fact
    assert "scoped_values" in r.report["failures"] and r.passed is False


def test_a_scope_split_on_the_same_number_fails() -> None:
    """"5 years overall including 2 managing": the same 24 months scoped to the
    whole role in one sample and to management in the other."""
    def with_scope(scope: str) -> dict[str, Any]:
        return sample(
            statement("s1", (0, 100), fact_ids=("f1",)),
            entries=[experience("f1", (0, 100), months=24, sids=["s1"], scope=scope)],
        )

    r = agree([with_scope("overall"), with_scope("management")])
    assert "scoped_values" in r.report["failures"] and r.passed is False


def test_a_negation_pointed_at_a_different_subject_fails() -> None:
    """"No sponsorship available" is an employer constraint, not a candidate
    disqualification. Both samples negate something; they disagree about what,
    which the `negated` boolean cannot see."""
    a = sample(statement("s1", (0, 100), polarity="negative", subject="employer"))
    b = sample(statement("s1", (0, 100), polarity="negative", subject="candidate"))
    r = agree([a, b])
    assert r.report["negation_disagreements"] == 0  # both read as negated
    assert r.report["polarity_target_disagreements"] == 1
    assert "polarity_target" in r.report["failures"] and r.passed is False


def test_a_negative_and_an_ambiguous_polarity_split_is_caught() -> None:
    """`negated` collapses `negative` and `ambiguous` into "not plainly
    positive"; the target dimension is where that split becomes visible."""
    a = sample(statement("s1", (0, 100), polarity="negative"))
    b = sample(statement("s1", (0, 100), polarity="ambiguous"))
    r = agree([a, b])
    assert r.report["negation_disagreements"] == 0
    assert "polarity_target" in r.report["failures"] and r.passed is False


def test_different_entities_on_the_same_claim_fail() -> None:
    a = sample(statement("s1", (0, 100)), mentions=[mention("m1", "Qt", (0, 10), ["s1"])])
    b = sample(statement("s1", (0, 100)), mentions=[mention("m1", "React", (0, 10), ["s1"])])
    r = agree([a, b])
    assert r.report["mean_f1"] == 1.0
    assert r.report["entity_link_disagreements"] == 2  # the statement and the link
    assert "entity_links" in r.report["failures"] and r.passed is False


def test_entity_links_compare_casefolded() -> None:
    """`aliases/1` casefolds; two samples spelling one brand differently agree
    about the entity, and manufacturing a disagreement there would be noise."""
    a = sample(statement("s1", (0, 100)), mentions=[mention("m1", "Qt", (0, 10), ["s1"])])
    b = sample(statement("s1", (0, 100)), mentions=[mention("m1", "QT", (0, 10), ["s1"])])
    r = agree([a, b])
    assert r.report["entity_link_disagreements"] == 0 and r.passed


def test_a_value_one_sample_never_derived_is_not_a_disagreement() -> None:
    """Subset, never conflict (2026-09-16 review): sample B derived only one
    of A's two facts. The omission side of a split is F1's and the dispute
    set's job; the aggregated dimensions fire on two different readings of
    the SAME value, not on a missing one."""
    a = sample(
        statement("s1", (0, 100), fact_ids=("f1", "f2")),
        entries=[experience("f1", (0, 100), months=144, sids=["s1"]),
                 experience("f2", (0, 100), months=24, sids=["s1"])],
    )
    b = sample(
        statement("s1", (0, 100), fact_ids=("f1",)),
        entries=[experience("f1", (0, 100), months=144, sids=["s1"])],
    )
    r = agree([a, b])
    assert r.report["scoped_value_disagreements"] == 0
    assert "scoped_values" not in r.report["failures"]


def test_aggregated_dimensions_fire_on_conflict_not_omission() -> None:
    """The unit-level pin: strict subset is silence, non-subset is a split —
    for both aggregated dimensions."""
    from jobhunter.l2.agreement import _Claim, _dimension_splits

    x = _Claim(span=(0, 10), importance=None, negated=False,
               values=("a", "b"), entity_links=("python", "go"))
    y = _Claim(span=(0, 10), importance=None, negated=False,
               values=("a",), entity_links=("python",))
    z = _Claim(span=(0, 10), importance=None, negated=False,
               values=("c",), entity_links=("rust",))
    assert dict(_dimension_splits(x, y))["scoped_values"] == 0
    assert dict(_dimension_splits(x, y))["entity_links"] == 0
    assert dict(_dimension_splits(x, z))["scoped_values"] == 1
    assert dict(_dimension_splits(x, z))["entity_links"] == 1


def test_byte_identical_records_still_pass_at_f1_one() -> None:
    """The floor under every new dimension: a record compared with itself agrees
    on all of them, whatever it carries."""
    s = sample(
        statement("s1", (0, 100), fact_ids=("f1",)),
        statement("s2", (200, 300), polarity="negative", subject="employer"),
        groups=[group("g1", "any_of", ["s1", "s2"])],
        mentions=[mention("m1", "Qt", (0, 10), ["s1"])],
        entries=[experience("f1", (0, 100), months=144, sids=["s1"])],
    )
    r = agree([s, s, s])
    assert r.passed and r.report["mean_f1"] == 1.0
    assert r.report["failures"] == []
    for key in ("kind", "polarity_target", "scoped_value", "alternatives", "entity_link"):
        assert r.report[f"{key}_disagreements"] == 0


def test_a_v1_profile_never_reaches_the_v2_dimensions() -> None:
    """v1 claims carry none of these fields and v1's validator version is frozen:
    a v1 cohort must score exactly what it scored before this gate grew."""
    a = profile(claim((0, 100)), claim((200, 300), importance="preferred"))
    b = profile(claim((0, 100)), claim((200, 300), importance="preferred"))
    r = agree([a, b])
    assert r.passed and r.report["failures"] == []
    for key in ("kind", "polarity_target", "scoped_value", "alternatives", "entity_link"):
        assert r.report[f"{key}_disagreements"] == 0


def test_an_incomplete_cohort_reports_every_dimension_key() -> None:
    """The `sample_failed` early return is a report shape too — a reader that
    finds `negation_disagreements` must find the rest."""
    hook = cohort_hook(lambda a: a.record)
    _, _, report, _ = hook([_Slot(1, "k1", sample(statement("s1", (0, 100)))),
                            _Slot(2, "k2", None)], 2)
    for key in ("kind", "polarity_target", "scoped_value", "alternatives", "entity_link"):
        assert report[f"{key}_disagreements"] == 0


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
