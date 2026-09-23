"""Agreement gate tests (spec §4.5): alignment by span-overlap Jaccard ≥ 0.5
(greedy, one-to-one), the medoid chosen whole with a deterministic tie-break by
sample slot, and — validator/20, parsing contract v3 §3 — exactly two checks
that GATE: zero negation disagreements and zero numeric conflicts. Everything
else the gate computes (F1, kind, polarity target, scoped values, alternatives,
entity links) is still computed and still reported, now under
`report["metrics"]`, and never fails a v2 document.

A v1 cohort is judged under `LEGACY_GATES` instead, because `demand-profile/v5`
settles at validator "12" and parsing contract v3 bumps nothing below 19; the
cohort says which set applies by whether its profile declares a `schema`.

Plus validator/18's dispute sets: what a disagreeing cohort actually disagrees
ABOUT, expressed in the medoid's own id namespace plus document-wide block ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from jobhunter.l2.agreement import (
    F1_MIN,
    GATES,
    IMPORTANCE_MIN,
    JACCARD_MIN,
    LEGACY_GATES,
    Dispute,
    agree,
    cohort_hook,
    dispute_set,
)
from jobhunter.l2.v2.serve import SCHEMA_VERSION, claim_index


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
    """A cohort sample under the v2 CONTRACT, in the claim shape the gate reads.

    The `schema` key is what `v2/serve.profile_of` stamps on every blob it
    builds, and `agreement._gates` reads it to decide that validator/20's two
    gates apply. Leaving it off is not a shortcut — it is what makes a sample a
    v1 one (`v1_profile` below).
    """
    return {"schema": SCHEMA_VERSION,
            "demand_profile": {"areas": [{"id": "a", "claims": list(claims)}]}}


def v1_profile(*claims: dict[str, Any]) -> dict[str, Any]:
    """A cohort sample as `bundles._v1_profile_of` builds one: no `schema`.

    v1's projection is frozen bytes — `facts` and `demand_profile`, nothing
    else — which is exactly what tells the gate this cohort settles under
    validator "12" and keeps `LEGACY_GATES`.
    """
    return {"facts": {},
            "demand_profile": {"areas": [{"id": "a", "claims": list(claims)}]}}


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


def test_low_f1_is_a_metric_and_never_a_failure() -> None:
    """Validator/20: F1 counts how much two samples found in common, and the
    2026-09-22 analysis showed that number is thoroughness variance, not a
    reading of the document either sample can be wrong about. It is still
    computed, still reported, and no longer gates."""
    a = profile(claim((0, 10)), claim((20, 30)), claim((40, 50)), claim((60, 70)))
    b = profile(claim((0, 10)))
    r = agree([a, b])
    assert r.report["mean_f1"] < F1_MIN
    assert r.report["failures"] == [] and r.passed
    assert r.report["metrics"]["f1"] == r.report["mean_f1"]


def test_a_single_negation_split_fails_even_at_perfect_f1() -> None:
    a = profile(claim((0, 100), negated=False))
    b = profile(claim((0, 100), negated=True))
    r = agree([a, b])
    assert not r.passed
    assert r.report["negation_disagreements"] == 1
    assert "negation" in r.report["failures"]


def test_an_importance_split_is_still_measured_and_never_fails() -> None:
    """The field is gone from schema 3 and the dimension went with it (spec
    §3). A schema-2 profile still carries importance, so the ratio is still
    reported for the archived corpus — it just decides nothing."""
    a = profile(claim((0, 100), importance="required"))
    b = profile(claim((0, 100), importance="preferred"))
    r = agree([a, b])
    assert r.report["required_importance_agreement"] == 0.0
    assert r.report["failures"] == [] and r.passed


def test_contextual_importance_disagreement_is_not_gated() -> None:
    # Neither side required: the importance ratio does not even see this.
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


def statement3(
    sid: str,
    span: tuple[int, int],
    *,
    block: str = "b1",
    polarity: str = "positive",
    kind: str = "qualification",
    subject: str = "candidate",
    fact_ids: tuple[str, ...] = (),
    heading: str | None = "Requirements",
    modality: bool = False,
) -> dict[str, Any]:
    """A SCHEMA-3 statement: a code-derived heading and a quoted modal phrase
    where the verdicts used to be (parsing contract v3 §2.1). The gate fixtures
    are these, because a schema-3 statement has no `importance` key at all and
    the projections must never index one."""
    return {
        "id": sid,
        "kind": kind,
        "subject": subject,
        "topic": sid,
        "evidence": [ref(block, span)],
        "section_heading": heading,
        "modality_evidence": [ref(block, span)] if modality else None,
        "polarity": polarity,
        "polarity_evidence": None,
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
    schema: str = SCHEMA_VERSION,
) -> dict[str, Any]:
    """One v2 sample as settlement sees it: the declared contract, the stored
    slice's `statements`, and the claim index the gate aligns.

    `schema` is here for the same reason `profile_of` stamps it — it is what
    `agreement._gates` reads to judge this cohort under validator/20. Since the
    v20 bump the stamp is the RECORD's own shape ("3" for a v11 record, "2" for
    a frozen replay), so it is a parameter here too.
    """
    record = {
        "statements": list(statements),
        "relations": {"groups": groups or [], "conditions": [], "example_sets": []},
        "mentions": mentions or [],
        "facts": {"entries": entries or []},
    }
    return {"schema": schema, "statements": record["statements"],
            "demand_profile": claim_index(record)}


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


# ---- the two checks that gate (validator/20) --------------------------------
# Parsing contract v3 §3: "The agreement gate keeps two checks. Everything else
# it computes today is reported in `agreement` as a metric and never fails a
# document." Negation, because "no sponsorship" read as "sponsorship available"
# is the one extraction error that actively harms the user; numeric conflict,
# because 144 vs 12 months is a misread rather than variance.


def test_only_negation_and_numeric_conflict_can_fail_a_document() -> None:
    assert GATES == ("negation", "numeric_conflict")


def test_a_numeric_conflict_on_one_span_fails() -> None:
    """144 months vs 12 months off the same cited span: the numbers are code-
    derived, so a split here is a misread of the document, not style."""
    def with_months(months: int) -> dict[str, Any]:
        return sample(
            statement3("s1", (0, 100), fact_ids=("f1",)),
            entries=[experience("f1", (0, 100), months=months, sids=["s1"])],
        )

    r = agree([with_months(144), with_months(12)])
    assert r.report["mean_f1"] == 1.0  # every claim aligns ...
    assert r.report["numeric_conflicts"] == 2  # ... the statement and the fact
    assert r.report["failures"] == ["numeric_conflict"] and r.passed is False


def test_a_number_only_one_sample_derived_is_not_a_conflict() -> None:
    """"BOTH parsed a number from the same span" (spec §3): a claim that
    derived nothing is silence, and the omission side of a split is what the
    F1 metric and the dispute set answer for."""
    derived = sample(
        statement3("s1", (0, 100), fact_ids=("f1",)),
        entries=[experience("f1", (0, 100), months=144, sids=["s1"])],
    )
    bare = sample(statement3("s1", (0, 100)))
    r = agree([derived, bare])
    assert r.report["numeric_conflicts"] == 0 and r.passed


def test_a_number_the_grammar_could_not_read_is_not_a_conflict() -> None:
    """`present_unparsed` means the document said something the grammar could
    not read; two samples that both failed to read it have no numbers to
    disagree about."""
    def unparsed(months: int) -> dict[str, Any]:
        entry = experience("f1", (0, 100), months=months, sids=["s1"])
        entry["derived"] = {"state": "present_unparsed", "money": None, "date": None,
                            "quantity": None}
        return sample(statement3("s1", (0, 100), fact_ids=("f1",)), entries=[entry])

    r = agree([unparsed(144), unparsed(12)])
    assert r.report["numeric_conflicts"] == 0 and r.passed


def test_a_polarity_flip_on_an_aligned_claim_still_fails_negation() -> None:
    a = sample(statement3("s1", (0, 100)))
    b = sample(statement3("s1", (0, 100), polarity="negative"))
    r = agree([a, b])
    assert r.report["negation_disagreements"] == 1
    assert r.report["failures"] == ["negation"] and r.passed is False


# ---- the demoted dimensions (validator/19 -> validator/20 metrics) -----------
# The 2026-09-22 review-queue analysis (spec §3, 300-doc sample): 294 of 300
# review documents failed on label variance over identical text. Every shape
# below is one of those classes. Each is still computed, still named in
# `metrics`, and none of them parks a document any more.


def _adjacent_kinds() -> tuple[dict[str, Any], dict[str, Any]]:
    """172 of the sampled pairs: `compensation_statement` against
    `employer_context` over one sentence about pay."""
    return (sample(statement3("s1", (0, 100), kind="compensation_statement")),
            sample(statement3("s1", (0, 100), kind="employer_context")))


def _same_number_different_scope() -> tuple[dict[str, Any], dict[str, Any]]:
    """1,399 of 1,536 value splits: "5 years overall including 2 managing", the
    same 24 months tagged `overall` in one sample and `management` in the
    other. The NUMBER is identical, so this is a tag split, never a misread."""
    def with_scope(scope: str) -> dict[str, Any]:
        return sample(
            statement3("s1", (0, 100), fact_ids=("f1",)),
            entries=[experience("f1", (0, 100), months=24, sids=["s1"], scope=scope)],
        )

    return with_scope("overall"), with_scope("management")


def _entity_spelling() -> tuple[dict[str, Any], dict[str, Any]]:
    """`gps` against `global positioning systems (gps)` — one entity, two
    surfaces the document itself offers."""
    return (
        sample(statement3("s1", (0, 100)),
               mentions=[mention("m1", "gps", (0, 10), ["s1"])]),
        sample(statement3("s1", (0, 100)),
               mentions=[mention("m1", "global positioning systems (gps)", (0, 10), ["s1"])]),
    )


def _relation_thoroughness() -> tuple[dict[str, Any], dict[str, Any]]:
    """One sample emitting more relations than the other: an `any_of` route in
    one and no relation at all in the other."""
    return (
        sample(statement3("s1", (0, 100)), statement3("s2", (200, 300)),
               groups=[group("g1", "any_of", ["s1", "s2"])]),
        sample(statement3("s1", (0, 100)), statement3("s2", (200, 300))),
    )


REVIEW_SPLITS = [
    ("adjacent-kinds", _adjacent_kinds, "kind"),
    ("scope-tag", _same_number_different_scope, "scoped_values"),
    ("entity-spelling", _entity_spelling, "entity_links"),
    ("relation-thoroughness", _relation_thoroughness, "alternatives"),
]


@pytest.mark.parametrize("name,build,dimension", REVIEW_SPLITS, ids=[r[0] for r in REVIEW_SPLITS])
def test_a_review_split_settles_with_the_dimension_named_in_metrics(
    name: str, build: Any, dimension: str
) -> None:
    a, b = build()
    r = agree([a, b])
    assert r.passed is True and r.report["failures"] == []
    # validator/19 failed exactly this dimension; 20 still counts it and says
    # so, which is what `quality.sample_notes` serves to the reading agent
    assert r.report["metrics"]["splits"][dimension] > 0
    assert r.report["metrics"]["aligned_pairs"] > 0


def test_an_all_of_any_of_split_is_a_metric_at_perfect_f1() -> None:
    """The validator/19 refutation case: two records identical except the
    operator over the same two statements. It is still counted — and under 20
    it is reported rather than parked, because a relation one sample drew and
    the other did not is thoroughness, not a contradiction about the text."""
    def with_operator(operator: str) -> dict[str, Any]:
        return sample(
            statement3("s1", (0, 100)), statement3("s2", (200, 300)),
            groups=[group("g1", operator, ["s1", "s2"])],
        )

    r = agree([with_operator("all_of"), with_operator("any_of")])
    assert r.report["mean_f1"] == 1.0  # every claim aligns ...
    assert r.report["alternatives_disagreements"] == 2  # ... and they still disagree
    assert r.report["metrics"]["splits"]["alternatives"] == 2
    assert r.report["failures"] == [] and r.passed is True


def test_a_kind_flip_is_a_metric() -> None:
    """A duty read as a prerequisite is the C12 conflation; the claim sets align
    perfectly because both samples cite the same sentence."""
    a = sample(statement3("s1", (0, 100)))
    b = sample(statement3("s1", (0, 100), kind="responsibility"))
    r = agree([a, b])
    assert r.report["mean_f1"] == 1.0
    assert r.report["kind_disagreements"] == 1
    assert r.report["metrics"]["splits"]["kind"] == 1
    assert r.report["failures"] == [] and r.passed is True


def test_the_same_number_under_different_scope_tags_never_conflicts() -> None:
    """"5 years overall including 2 managing": the same 24 months scoped to the
    whole role in one sample and to management in the other. The scope TAG
    splits — a metric — and the number does not, so nothing gates."""
    a, b = _same_number_different_scope()
    r = agree([a, b])
    assert r.report["scoped_value_disagreements"] == 2
    assert r.report["numeric_conflicts"] == 0
    assert r.report["failures"] == [] and r.passed is True


def test_a_negation_pointed_at_a_different_subject_is_a_metric() -> None:
    """"No sponsorship available" is an employer constraint, not a candidate
    disqualification. Both samples negate something; they disagree about what,
    which the `negated` boolean cannot see — and under 20 the boolean is the
    only half of polarity that gates."""
    a = sample(statement3("s1", (0, 100), polarity="negative", subject="employer"))
    b = sample(statement3("s1", (0, 100), polarity="negative", subject="candidate"))
    r = agree([a, b])
    assert r.report["negation_disagreements"] == 0  # both read as negated
    assert r.report["polarity_target_disagreements"] == 1
    assert r.report["failures"] == [] and r.passed is True


def test_a_negative_and_an_ambiguous_polarity_split_is_a_metric() -> None:
    """`negated` collapses `negative` and `ambiguous` into "not plainly
    positive", so this pair agrees on the gate and splits on the metric."""
    a = sample(statement3("s1", (0, 100), polarity="negative"))
    b = sample(statement3("s1", (0, 100), polarity="ambiguous"))
    r = agree([a, b])
    assert r.report["negation_disagreements"] == 0
    assert r.report["metrics"]["splits"]["polarity_target"] == 1
    assert r.report["failures"] == [] and r.passed is True


def test_different_entities_on_the_same_claim_are_a_metric() -> None:
    a = sample(statement3("s1", (0, 100)), mentions=[mention("m1", "Qt", (0, 10), ["s1"])])
    b = sample(statement3("s1", (0, 100)), mentions=[mention("m1", "React", (0, 10), ["s1"])])
    r = agree([a, b])
    assert r.report["mean_f1"] == 1.0
    assert r.report["entity_link_disagreements"] == 2  # the statement and the link
    assert r.report["failures"] == [] and r.passed is True


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


def test_the_numeric_signature_is_read_out_of_the_stored_value_signature() -> None:
    """The one coupling the numeric check rests on, pinned in both directions:
    `serve._value_signature` writes `family|scope|date_kind|component|state`
    and then, when a number was derived, a marker and the derivation. The gate
    reads from the marker on, which is exactly what makes a scope TAG invisible
    to it and a misread number visible."""
    from jobhunter.l2.agreement import _numbers
    from jobhunter.l2.v2.serve import _value_signature

    overall = _value_signature(experience("f1", (0, 100), months=24, sids=["s1"]))
    managing = _value_signature(
        experience("f1", (0, 100), months=24, sids=["s1"], scope="management")
    )
    twelve = _value_signature(experience("f1", (0, 100), months=12, sids=["s1"]))
    assert overall != managing  # the stored signature keeps the tag ...
    assert _numbers((overall,)) == _numbers((managing,))  # ... the number ignores it
    assert _numbers((overall,)) != _numbers((twelve,))
    assert _numbers((overall,)) == ("q|duration|gte|24|None|True|None|month",)


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
    """v1 claims carry none of these fields, so the five dimensions cannot fire
    on a v1 cohort however it splits — which is what validator/19 promised when
    it added them."""
    a = v1_profile(claim((0, 100)), claim((200, 300), importance="preferred"))
    b = v1_profile(claim((0, 100)), claim((200, 300), importance="required"))
    r = agree([a, b])
    for key in ("kind", "polarity_target", "scoped_value", "alternatives", "entity_link"):
        assert r.report[f"{key}_disagreements"] == 0
    assert r.report["failures"] == ["importance"]  # the one dimension v1 has


# ---- v1 keeps validator/12 -------------------------------------------------
# `agree` is the ONE settlement implementation both bundles share, and
# `demand-profile/v5` settles under `transforms.VALIDATOR_VERSION = "12"`,
# which parsing contract v3 does not bump. Demoting F1 and the importance ratio
# for v1 too would give one identifier two settlement policies and let
# `rebuild` promote, at that unchanged identifier, documents the live path
# parked. The gate set is read off the cohort instead (`agreement._gates`).


def test_the_two_gate_sets_are_the_two_validator_policies() -> None:
    assert GATES == ("negation", "numeric_conflict")
    assert LEGACY_GATES == ("f1", "importance", "negation", "kind",
                            "polarity_target", "scoped_values",
                            "alternatives", "entity_links")


def test_a_v1_cohort_still_fails_on_f1() -> None:
    """The regression this pins: under validator/12 a 2-of-3 claim-set overlap
    parks the document, and it still must."""
    a = v1_profile(claim((0, 10), cid="c1"), claim((20, 30), cid="c2"),
                   claim((40, 50), cid="c3"))
    b = v1_profile(claim((0, 10), cid="c1"), claim((20, 30), cid="c2"),
                   claim((100, 110), cid="c4"))
    r = agree([a, b], f1_min=F1_MIN)
    assert r.report["mean_f1"] < F1_MIN
    assert r.report["failures"] == ["f1"] and r.passed is False


def test_a_v1_cohort_still_fails_on_the_importance_ratio() -> None:
    a = v1_profile(claim((0, 100), importance="required"))
    b = v1_profile(claim((0, 100), importance="preferred"))
    r = agree([a, b])
    assert r.report["required_importance_agreement"] == 0.0
    assert r.report["failures"] == ["importance"] and r.passed is False


def test_the_same_split_settles_differently_under_the_two_contracts() -> None:
    """One cohort shape, two identities: the v2 blob declares its contract and
    settles, the v1 blob does not and parks. Same claims, same numbers."""
    claims = (claim((0, 10), cid="c1"), claim((20, 30), cid="c2"))
    v2 = agree([profile(*claims), profile(claims[0])])
    v1 = agree([v1_profile(*claims), v1_profile(claims[0])])
    assert v2.report["mean_f1"] == v1.report["mean_f1"] < F1_MIN
    assert v2.report["failures"] == [] and v2.passed is True
    assert v1.report["failures"] == ["f1"] and v1.passed is False


def test_a_schema_3_cohort_is_judged_under_validator_20s_two_gates() -> None:
    """The stamp moved with the bump (`serve.profile_of` now stamps the
    record's own `extraction.schema_version`), and `_gates` has to keep reading
    it as a v2-family contract: a schema-3 cohort that splits on F1 settles,
    and one that splits on polarity still parks. A gate set chosen by the
    literal string "2" would silently hand every v11 document validator/12's
    policy and park the corpus on demoted metrics.
    """
    split = agree([sample(statement3("s1", (0, 10)), statement3("s2", (20, 30)), schema="3"),
                   sample(statement3("s1", (0, 10)), schema="3")])
    assert split.report["mean_f1"] < F1_MIN
    assert split.report["failures"] == [] and split.passed is True

    flipped = agree([sample(statement3("s1", (0, 100)), schema="3"),
                     sample(statement3("s1", (0, 100), polarity="negative"), schema="3")])
    assert flipped.report["failures"] == ["negation"] and flipped.passed is False


def test_a_schema_3_cohort_still_parks_on_a_numeric_conflict() -> None:
    """The other gate, on the new stamp: 144 months against 12 is a misread
    whatever shape the record declares."""
    a = sample(statement3("s1", (0, 100), fact_ids=("f1",)), schema="3",
               entries=[experience("f1", (0, 100), months=144, sids=["s1"])])
    b = sample(statement3("s1", (0, 100), fact_ids=("f1",)), schema="3",
               entries=[experience("f1", (0, 100), months=12, sids=["s1"])])
    result = agree([a, b])
    assert result.report["failures"] == ["numeric_conflict"] and result.passed is False


def test_the_contract_key_is_what_the_v1_projection_actually_emits() -> None:
    """`_gates` is only honest if the two real producers differ this way.

    v1's half is here: `bundles._v1_profile_of` is frozen bytes and declares no
    contract. v2's half is pinned on the real producer in
    `tests/l2/v2/test_serve.py::test_profile_of_is_the_served_slice_under_a_shape_marker`,
    and the same file's F1/importance tests run `serve.profile_of` output
    through `agree` and get validator/20's verdict end to end.
    """
    from jobhunter.l2.agreement import _CONTRACT_KEY
    from jobhunter.l2.bundles import _v1_profile_of
    from jobhunter.l2.v2.serve import _SCHEMA_KEY

    v1_blob = _v1_profile_of({"facts": {}, "demand_profile": {"areas": []}})
    assert "schema" not in v1_blob
    assert sample(statement("s1", (0, 100)))["schema"] == SCHEMA_VERSION
    # the key the gate dispatches on IS the key the projection stamps
    assert _CONTRACT_KEY == _SCHEMA_KEY == "schema"


def test_a_v1_cohort_still_fails_on_negation_and_never_on_numeric_conflict() -> None:
    """The gate set narrows what can park a document, never what is measured:
    `numeric_conflicts` is computed for a v1 cohort too (always 0 — v1 claims
    derive no values) and is simply not one of validator/12's failures."""
    a = v1_profile(claim((0, 100)))
    b = v1_profile(claim((0, 100), negated=True))
    r = agree([a, b])
    assert r.report["failures"] == ["negation"] and r.passed is False
    assert r.report["numeric_conflicts"] == 0
    assert r.report["metrics"]["f1"] == 1.0  # metrics are reported either way


def test_an_incomplete_cohort_reports_every_dimension_key() -> None:
    """The `sample_failed` early return is a report shape too — a reader that
    finds `negation_disagreements` must find the rest, `metrics` included."""
    hook = cohort_hook(lambda a: a.record)
    _, _, report, _ = hook([_Slot(1, "k1", sample(statement("s1", (0, 100)))),
                            _Slot(2, "k2", None)], 2)
    for key in ("kind", "polarity_target", "scoped_value", "alternatives", "entity_link"):
        assert report[f"{key}_disagreements"] == 0
    assert report["numeric_conflicts"] == 0
    assert report["metrics"] == {
        "aligned_pairs": 0, "f1": None,
        "splits": {"kind": 0, "polarity_target": 0, "scoped_values": 0,
                   "alternatives": 0, "entity_links": 0},
    }


def test_the_report_names_the_policy_and_counts_the_samples_that_arrived() -> None:
    """Settlement has to know which contract judged this cohort, and the cohort
    is the only thing that knows (`_gates`). The report carries both halves: the
    gate set that applied, and how many of the requested slots produced a record
    to compare — `k` against `arrived` is what `sample_failed` means.
    """
    v2 = sample(statement("s1", (0, 100)))
    hook = cohort_hook(lambda a: a.record)

    _, _, report, _ = hook([_Slot(1, "k1", v2), _Slot(2, "k2", v2)], 3)
    assert report["gates"] == list(GATES)
    assert (report["k"], report["arrived"]) == (3, 2)
    assert report["failures"] == ["sample_failed"]

    v1 = v1_profile(claim((0, 100)))
    _, _, legacy, _ = hook([_Slot(1, "k1", v1), _Slot(2, "k2", v1)], 2)
    assert legacy["gates"] == list(LEGACY_GATES)
    assert (legacy["k"], legacy["arrived"]) == (2, 2)


def test_an_unresolvable_cohort_reports_the_conservative_policy() -> None:
    """One record resolved names its own contract; none resolved names nothing,
    and a cohort this module cannot identify is never the one that fails less."""
    hook = cohort_hook(lambda a: a.record)
    _, _, one, _ = hook([_Slot(1, "k1", sample(statement("s1", (0, 100)))),
                         _Slot(2, "k2", None)], 2)
    assert one["gates"] == list(GATES) and one["arrived"] == 1

    _, _, none, _ = hook([_Slot(1, "k1", None), _Slot(2, "k2", None)], 2)
    assert none["gates"] == list(LEGACY_GATES) and none["arrived"] == 0


def test_the_dispute_is_computed_against_the_medoid_the_gate_chose() -> None:
    """The medoid is the sample the audit ran on, so the namespace has to be
    its own: a run whose medoid is slot 2 disputes slot 2's ids.

    The two agreeing samples read the shared statement as negated and the
    outlier does not — under validator/20 a polarity split is what opens a
    dispute at all, and the unaligned statements and sibling-only blocks are
    named exactly as they were under 18.
    """
    outlier = sample(statement("o1", (0, 100)), statement("o2", (600, 700), block="b6"))
    common = sample(statement("c1", (0, 100), polarity="negative"),
                    statement("c2", (200, 300), block="b2"))
    hook = cohort_hook(lambda a: a.record)
    passed, key, _report, dispute = hook(
        [_Slot(1, "k1", outlier), _Slot(2, "k2", common), _Slot(3, "k3", common)], 3
    )
    assert not passed and key == "k2"  # the medoid is one of the two agreeing samples
    assert dispute is not None
    assert dispute.statement_ids == frozenset({"c2"})
    assert dispute.block_ids == frozenset({"b6"})
