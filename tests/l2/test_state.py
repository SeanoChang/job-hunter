from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from jobhunter.l2.agreement import DIMENSIONS, GATES, Dispute, agree, cohort_hook
from jobhunter.l2.state import (
    ACCOUNTING_GAPS,
    BOOKKEEPING_CODES,
    GATE_DIMENSION_CODES,
    DerivedState,
    RecoveredCandidate,
    Review,
    audit_touches_dispute,
    bookkeeping_gaps,
    derive_state,
)
from jobhunter.l2.v2.quality import assess
from tests.l2.test_agreement import (
    REVIEW_SPLITS,
    claim,
    sample,
    statement,
    statement3,
    v1_profile,
)
from tests.l2.test_attempts import _attempt

GLOBS = ["z-ai/glm-5.2*", "nvidia/*"]


def test_empty_is_pending() -> None:
    assert derive_state([], [], GLOBS) == DerivedState(None, None)


def test_ok_in_glob_validates() -> None:
    a = _attempt()
    state = derive_state([a], [], GLOBS)
    assert state.status == "validated" and state.chosen_attempt == a.attempt_key


def test_ok_out_of_glob_does_not_settle() -> None:
    a = _attempt(observed_model="claude-haiku-4-5")
    assert derive_state([a], [], GLOBS) == DerivedState(None, None)


def test_first_ok_wins() -> None:
    first = _attempt()
    second = _attempt(
        attempt_key="extractions/attempts/2026/08/27T070000Z-abcdefabcdef-s1a1.json.gz",
        started_at="2026-08-27T07:00:00Z",
    )
    state = derive_state([second, first], [], GLOBS)  # order-insensitive input
    assert state.chosen_attempt == first.attempt_key


def test_ladder_exhaustion_quarantines() -> None:
    fail = _attempt(outcome="attribution_failed", attempt_no=3, ladder_exhausted=True,
                    observed_model=None)
    assert derive_state([fail], [], GLOBS).status == "quarantined"


def test_content_failure_without_exhaustion_stays_pending() -> None:
    fail = _attempt(outcome="schema_invalid", attempt_no=3, ladder_exhausted=False)
    assert derive_state([fail], [], GLOBS).status is None


def test_over_budget_quarantines() -> None:
    a = _attempt(outcome="over_budget", raw_response=None, observed_model=None)
    assert derive_state([a], [], GLOBS).status == "quarantined"


def test_transport_class_never_settles() -> None:
    for outcome in ("transport", "throttled", "model_rejected"):
        a = _attempt(outcome=outcome, raw_response=None, observed_model=None)
        assert derive_state([a], [], GLOBS).status is None


def test_review_flow() -> None:
    ok = _attempt()
    flagged = derive_state([ok], [Review("flag", "2026-08-28T00:00:00Z")], GLOBS)
    assert flagged.status == "needs_review"
    accepted = derive_state(
        [ok],
        [Review("flag", "2026-08-28T00:00:00Z"), Review("accept", "2026-08-29T00:00:00Z")],
        GLOBS,
    )
    assert accepted.status == "validated" and accepted.chosen_attempt == ok.attempt_key
    rejected = derive_state(
        [ok],
        [Review("flag", "2026-08-28T00:00:00Z"), Review("reject", "2026-08-29T00:00:00Z")],
        GLOBS,
    )
    assert rejected.status == "rejected"
    refuted = derive_state([ok], [Review("refute", "2026-08-28T00:00:00Z")], GLOBS)
    assert refuted.status == "needs_review"


def test_retry_clears_quarantine() -> None:
    fail = _attempt(outcome="attribution_failed", attempt_no=3, ladder_exhausted=True,
                    observed_model=None)
    state = derive_state([fail], [Review("retry", "2026-08-28T00:00:00Z")], GLOBS)
    assert state == DerivedState(None, None)


def test_accept_from_quarantine_is_ignored() -> None:
    fail = _attempt(outcome="attribution_failed", attempt_no=3, ladder_exhausted=True,
                    observed_model=None)
    state = derive_state([fail], [Review("accept", "2026-08-28T00:00:00Z")], GLOBS)
    assert state.status == "quarantined"


def test_flag_on_pending_is_ignored() -> None:
    assert derive_state([], [Review("flag", "2026-08-28T00:00:00Z")], GLOBS).status is None


def test_retry_then_later_ok_revalidates() -> None:
    fail = _attempt(outcome="attribution_failed", attempt_no=3, ladder_exhausted=True,
                    observed_model=None)
    retry = Review("retry", "2026-08-28T00:00:00Z")
    later_ok = _attempt(
        attempt_key="extractions/attempts/2026/08/29T000000Z-abcdefabcdef-s1a1.json.gz",
        started_at="2026-08-29T00:00:00Z",
    )
    state = derive_state([fail, later_ok], [retry], GLOBS)
    assert state.status == "validated" and state.chosen_attempt == later_ok.attempt_key


def test_rejected_keeps_chosen_attempt() -> None:
    ok = _attempt()
    state = derive_state([ok], [Review("reject", "2026-08-28T00:00:00Z")], GLOBS)
    assert state.status == "rejected" and state.chosen_attempt == ok.attempt_key


def test_same_timestamp_review_tiebreak_is_deterministic() -> None:
    ok = _attempt()
    flag = Review("flag", "2026-08-28T00:00:00Z", key="a-flag")
    accept = Review("accept", "2026-08-28T00:00:00Z", key="b-accept")
    forward = derive_state([ok], [flag, accept], GLOBS)
    backward = derive_state([ok], [accept, flag], GLOBS)
    assert forward == backward  # key order decides, input order does not
    assert forward.status == "validated"  # flag (a-) folds before accept (b-)


def test_later_ok_never_overrides_human_or_machine_settled_states() -> None:
    later_ok = _attempt(
        attempt_key="extractions/attempts/2026/08/30T000000Z-abcdefabcdef-s1a9.json.gz",
        started_at="2026-08-30T00:00:00Z", attempt_no=9,
    )
    ok = _attempt()
    flagged = derive_state([ok, later_ok], [Review("flag", "2026-08-29T00:00:00Z")], GLOBS)
    assert flagged.status == "needs_review"  # machine result cannot re-promote
    rejected = derive_state([ok, later_ok], [Review("reject", "2026-08-29T00:00:00Z")], GLOBS)
    assert rejected.status == "rejected"
    quarantine = _attempt(outcome="attribution_failed", attempt_no=3, ladder_exhausted=True,
                          observed_model=None)
    still_quarantined = derive_state([quarantine, later_ok], [], GLOBS)
    assert still_quarantined.status == "quarantined"  # only a human retry clears it


# --- cohort integrity (architecture review 2026-09-06, P0-2) ----------------
# A document the audit selected must not certify on an incomplete cohort: the
# gate re-evaluates on EVERY attempt once a second slot exists, and a human
# (or refuter) ruling is final against later automated samples.



def _slot(slot: int, no: int, **over: object):
    key = f"extractions/attempts/2026/08/27T06120{no}Z-abcdefabcdef-s{slot}a{no}.json.gz"
    record = {"facts": {}, "demand_profile": {"areas": [{"id": "a", "claims": [
        {"id": "c", "importance": "required", "negated": False,
         "quote": {"span": [0, 50], "text": "x" * 50}}]}]}}
    base: dict[str, object] = {
        "attempt_key": key, "sample_slot": slot, "attempt_no": no,
        "started_at": f"2026-08-27T06:12:0{no}Z", "record": record,
    }
    base.update(over)
    return _attempt(**base)


HOOK = cohort_hook(lambda a: a.record)


def test_incomplete_cohort_never_certifies() -> None:
    events = [
        _slot(1, 1),
        _slot(2, 2, outcome="schema_invalid", record=None),
        _slot(3, 3, outcome="schema_invalid", record=None),
    ]
    state = derive_state(events, [], GLOBS, HOOK)
    assert state.status == "needs_review"
    assert state.k == 3
    assert state.agreement and "sample_failed" in state.agreement["failures"]


def test_complete_agreeing_cohort_validates() -> None:
    events = [_slot(1, 1), _slot(2, 2), _slot(3, 3)]
    state = derive_state(events, [], GLOBS, HOOK)
    assert state.status == "validated" and state.k == 3
    assert state.agreement and state.agreement["failures"] == []


def test_human_accept_is_final_against_later_samples() -> None:
    events = [
        _slot(1, 1),
        _slot(2, 2, outcome="schema_invalid", record=None),  # gate demotes here
    ]
    reviews = [Review(verb="accept", at="2026-08-27T06:12:08Z", key="r1")]
    late_sample = _slot(3, 9, started_at="2026-08-27T06:12:09Z")
    state = derive_state([*events, late_sample], reviews, GLOBS, HOOK)
    assert state.status == "validated"  # the human ruling stands


def test_retry_starts_a_fresh_cohort() -> None:
    events = [
        _slot(1, 1),
        _slot(2, 2, outcome="schema_invalid", record=None),
    ]
    reviews = [Review(verb="retry", at="2026-08-27T06:12:08Z", key="r1")]
    fresh = _slot(1, 9, started_at="2026-08-27T06:12:09Z")
    state = derive_state([*events, fresh], reviews, GLOBS, HOOK)
    assert state.status == "validated"
    assert state.agreement is None  # no stale cohort report survives the retry


# --- validator/16: the semantic-audit settlement policy ---------------------
# The plan's Policy table is normative
# (docs/superpowers/plans/2026-09-12-semantic-audit-v1.md). Every row below is
# one of its cells: cohort agreement x audit outcome -> (status, the sampling
# dimension, the audit dimensions) and the eligibility `quality.assess`
# derives from them. Without an `audit_hook` the fold must stay exactly
# validator/15.


@dataclass(frozen=True)
class _Audit:
    """The minimal AuditView the hook returns; `audit.AuditOutcome` satisfies it."""

    semantics: str
    completeness: str
    blocking: int


CLEAN = _Audit("no_findings", "no_findings", 0)
BLOCKING_FINDING = _Audit("findings", "no_findings", 1)
WARNING_ONLY = _Audit("findings", "no_findings", 0)  # wording_redundancy only
AUDIT_ERROR = _Audit("error", "error", 0)


def _cohort(passed: bool, failures: tuple[str, ...] = (), dispute=None):
    """The agreement gate, scripted: the comparator is tested in test_agreement.

    `dispute` defaults to None — a hook that reports no dispute set is exactly
    validator/16, and every row of the POLICY table below is still that.
    """

    def hook(ok_attempts, slots_attempted):
        report = {"k": slots_attempted, "failures": list(failures), "mean_f1": None}
        return (passed, ok_attempts[0].attempt_key, report, dispute)

    return hook


def _audits(view: object | None, calls: list[str] | None = None):
    def hook(attempt_key: str) -> object | None:
        if calls is not None:
            calls.append(attempt_key)
        return view

    return hook


def _eligible(state: DerivedState) -> bool:
    """The table's last column: what settle computes from the fold's verdict.

    Every quality input settle has comes from the fold, the lifecycle status
    included — publication follows settlement, so a record this fold parked or
    rejected must not come out eligible no matter how clean its audit is.
    """
    return bool(
        assess(
            source="usable",
            evidence="pass",
            semantics=state.semantics,
            completeness=state.completeness,
            sampling=state.sampling,
            human_review=state.human_review,
            blocking_findings=state.blocking,
            lifecycle=state.status or "pending",
        )["search_eligible"]
    )


# (id, slots, cohort passed, cohort failures, audit,
#  status, sampling, semantics, completeness, blocking, eligible)
POLICY = [
    ("pass+clean", 2, True, (), CLEAN,
     "validated", "complete", "no_findings", "no_findings", 0, True),
    ("pass+blocking", 2, True, (), BLOCKING_FINDING,
     "validated", "complete", "findings", "no_findings", 1, False),
    ("pass+warning", 2, True, (), WARNING_ONLY,
     "validated", "complete", "findings", "no_findings", 0, False),
    ("pass+error", 2, True, (), AUDIT_ERROR,
     "validated", "complete", "error", "error", 0, False),
    ("pass+absent", 2, True, (), None,
     "validated", "complete", "not_checked", "not_checked", 0, False),
    ("fail+clean", 2, False, ("negation",), CLEAN,
     "validated", "adjudicated", "no_findings", "no_findings", 0, True),
    ("fail+blocking", 2, False, ("negation",), BLOCKING_FINDING,
     "needs_review", "disagreement", "findings", "no_findings", 1, False),
    ("fail+warning", 2, False, ("f1",), WARNING_ONLY,
     "needs_review", "disagreement", "findings", "no_findings", 0, False),
    ("fail+error", 2, False, ("f1",), AUDIT_ERROR,
     "needs_review", "disagreement", "error", "error", 0, False),
    ("fail+absent", 2, False, ("importance",), None,
     "needs_review", "disagreement", "not_checked", "not_checked", 0, False),
    # validator/18 moved this cell: an incomplete cohort whose medoid audits
    # clean everywhere is adjudicated, because the audit is a whole-record
    # check and the missing samples had nothing to add to it
    ("incomplete+clean", 2, False, ("sample_failed",), CLEAN,
     "validated", "adjudicated", "no_findings", "no_findings", 0, True),
    ("incomplete+absent", 2, False, ("f1", "sample_failed"), None,
     "needs_review", "incomplete", "not_checked", "not_checked", 0, False),
    ("unsampled+clean", 1, True, (), CLEAN,
     "validated", "not_requested", "no_findings", "no_findings", 0, True),
    ("unsampled+blocking", 1, True, (), BLOCKING_FINDING,
     "validated", "not_requested", "findings", "no_findings", 1, False),
    ("unsampled+absent", 1, True, (), None,
     "validated", "not_requested", "not_checked", "not_checked", 0, False),
]


@pytest.mark.parametrize("row", POLICY, ids=[r[0] for r in POLICY])
def test_the_settlement_policy_table(row) -> None:
    (_id, slots, passed, failures, audit,
     status, sampling, semantics, completeness, blocking, eligible) = row
    events = [_slot(s, s) for s in range(1, slots + 1)]
    state = derive_state(
        events, [], GLOBS, _cohort(passed, failures), _audits(audit)
    )
    assert state.status == status
    assert state.sampling == sampling
    assert (state.semantics, state.completeness, state.blocking) == (
        semantics, completeness, blocking,
    )
    assert _eligible(state) is eligible
    assert state.chosen_attempt == events[0].attempt_key


def test_a_disagreeing_cohort_is_adjudicated_by_the_real_agreement_gate() -> None:
    """The same fold with the production comparator: two slots that split on
    polarity fail the gate, and a clean audit of the medoid settles it."""
    negated = {"facts": {}, "demand_profile": {"areas": [{"id": "a", "claims": [
        {"id": "c", "importance": "required", "negated": True,
         "quote": {"span": [0, 50], "text": "x" * 50}}]}]}}
    events = [_slot(1, 1), _slot(2, 2, record=negated)]
    parked = derive_state(events, [], GLOBS, HOOK)  # validator/15: no audit
    assert parked.status == "needs_review" and parked.sampling == "disagreement"
    assert parked.agreement and parked.agreement["failures"] == ["negation"]
    adjudicated = derive_state(events, [], GLOBS, HOOK, _audits(CLEAN))
    assert adjudicated.status == "validated" and adjudicated.sampling == "adjudicated"
    assert adjudicated.chosen_attempt == events[0].attempt_key  # the medoid, unchanged
    assert _eligible(adjudicated) is True


def test_no_audit_hook_is_exactly_validator_15() -> None:
    """The hook is additive: the v1 path passes none and must fold identically."""
    agreeing = [_slot(1, 1), _slot(2, 2), _slot(3, 3)]
    assert derive_state(agreeing, [], GLOBS, HOOK) == derive_state(
        agreeing, [], GLOBS, HOOK, None
    )
    for events, expect in (
        ([_slot(1, 1)], "validated"),
        (agreeing, "validated"),
        ([_slot(1, 1), _slot(2, 2, outcome="schema_invalid", record=None)], "needs_review"),
    ):
        state = derive_state(events, [], GLOBS, HOOK)
        assert state.status == expect
        # the audit dimensions of an unaudited fold are the unchecked defaults,
        # so nothing it produces can be eligible
        assert (state.semantics, state.completeness, state.blocking) == (
            "not_checked", "not_checked", 0,
        )
        assert _eligible(state) is False


def test_the_audit_is_probed_once_and_only_for_the_chosen_candidate() -> None:
    calls: list[str] = []
    events = [_slot(1, 1), _slot(2, 2)]
    state = derive_state(events, [], GLOBS, _cohort(False, ("f1",)), _audits(CLEAN, calls))
    assert state.status == "validated"
    assert calls == [events[0].attempt_key]  # the medoid only, and memoized


def test_nothing_settled_never_probes_the_audit() -> None:
    calls: list[str] = []
    quarantined = _attempt(outcome="over_budget", raw_response=None, observed_model=None)
    state = derive_state([quarantined], [], GLOBS, HOOK, _audits(CLEAN, calls))
    assert state.status == "quarantined" and calls == []
    pending = derive_state([], [], GLOBS, HOOK, _audits(CLEAN, calls))
    assert pending == DerivedState(None, None) and calls == []


def test_an_audit_never_promotes_a_human_ruled_record() -> None:
    """Spec §6: no automated phase promotes a published terminal state, and a
    reviewer's flag reopens an adjudication rather than being outvoted by it."""
    events = [_slot(1, 1), _slot(2, 2)]
    audit = _audits(CLEAN)
    fail = _cohort(False, ("negation",))
    rejected = derive_state(
        events, [Review("reject", "2026-08-27T06:12:08Z", key="r1")], GLOBS, fail, audit
    )
    assert rejected.status == "rejected"
    flagged = derive_state(
        events, [Review("flag", "2026-08-27T06:12:08Z", key="r1")], GLOBS, fail, audit
    )
    # the flag lands on the adjudicated `validated` and demotes it; the
    # sampling dimension loses the adjudication with it, so no reader can read
    # eligibility off a record a human reopened
    assert flagged.status == "needs_review" and flagged.sampling == "disagreement"
    assert _eligible(flagged) is False
    # a human retry clears the whole cohort, audit included
    retried = derive_state(
        events, [Review("retry", "2026-08-27T06:12:08Z", key="r1")], GLOBS, fail, audit
    )
    assert retried == DerivedState(None, None)


def test_an_audit_credits_only_the_candidate_it_audited() -> None:
    """The probe is per attempt key: a medoid the auditor never saw cannot
    borrow the previous medoid's clean audit (spec §4: findings bind to the
    exact candidate hash). Here slot 1 is audited and adjudicates the k=2
    cohort; a third sample moves the medoid to an unaudited candidate, and the
    document goes back to review."""
    events = [_slot(1, 1), _slot(2, 2), _slot(3, 3)]
    audited = {events[0].attempt_key: CLEAN}

    def moving_medoid(ok_attempts, slots_attempted):
        medoid = ok_attempts[0] if slots_attempted < 3 else ok_attempts[-1]
        return (False, medoid.attempt_key, {"k": slots_attempted, "failures": ["f1"]}, None)

    at_k2 = derive_state(events[:2], [], GLOBS, moving_medoid, audited.get)
    assert at_k2.status == "validated" and at_k2.sampling == "adjudicated"
    at_k3 = derive_state(events, [], GLOBS, moving_medoid, audited.get)
    assert at_k3.status == "needs_review" and at_k3.sampling == "disagreement"
    assert at_k3.chosen_attempt == events[2].attempt_key
    assert (at_k3.semantics, at_k3.completeness) == ("not_checked", "not_checked")


def test_the_real_audit_outcome_is_an_audit_view() -> None:
    """The seam between `v2.audit.judge` and this fold: its `AuditOutcome`
    goes in unconverted, so the two contracts cannot drift apart silently."""
    from jobhunter.l2.v2.audit import AuditOutcome

    clean = AuditOutcome(
        semantics="no_findings", completeness="no_findings",
        blocking=0, warnings=0, findings=[], unresolved=[],
    )
    events = [_slot(1, 1), _slot(2, 2)]
    state = derive_state(events, [], GLOBS, _cohort(False, ("negation",)), _audits(clean))
    assert state.status == "validated" and state.sampling == "adjudicated"
    assert (state.semantics, state.completeness, state.blocking) == (
        "no_findings", "no_findings", 0,
    )


def test_a_human_accept_does_not_fabricate_a_clean_audit() -> None:
    """An accept on a disagreement with no audit artifact validates the record
    (unchanged) but must not present it as eligible."""
    events = [_slot(1, 1), _slot(2, 2)]
    state = derive_state(
        events,
        [Review("accept", "2026-08-27T06:12:08Z", key="r1")],
        GLOBS,
        _cohort(False, ("f1",)),
        _audits(None),
    )
    assert state.status == "validated" and state.sampling == "disagreement"
    assert _eligible(state) is False


def test_an_accept_filed_before_the_audit_artifact_is_still_a_ruling() -> None:
    """The probe is timeless, so an artifact archived AFTER a human ruled
    re-reads the accept's point in the fold as the adjudicated `validated` —
    where an accept written for `needs_review` would land on nothing.

    Reachable without any race: a run that dies inside the audit call leaves
    its attempts committed and no artifact, the next catch-up parks the
    disagreement, the operator accepts it, and any later `--only-doc` run
    audits the same medoid. The accept must still be a ruling there: dropping
    it leaves `human_review` at "none" and nothing `ruled`, so the next
    automated sample overrides the record a human validated (spec §6).
    """
    events = [_slot(1, 1), _slot(2, 2)]
    accept = [Review("accept", "2026-08-27T06:12:08Z", key="r1")]
    late = _slot(3, 9, started_at="2026-08-27T06:12:09Z")
    audited = {events[0].attempt_key: CLEAN}  # only the k=2 medoid was audited

    def moving_medoid(ok_attempts, slots_attempted):
        medoid = ok_attempts[0] if slots_attempted < 3 else ok_attempts[-1]
        return (
            False, medoid.attempt_key, {"k": slots_attempted, "failures": ["negation"]}, None
        )

    # the counterfactual: with no artifact the accept lands on `needs_review`,
    # rules the record and stands against the late sample
    unaudited = derive_state([*events, late], accept, GLOBS, moving_medoid)
    assert unaudited.status == "validated" and unaudited.human_review == "accepted"

    state = derive_state([*events, late], accept, GLOBS, moving_medoid, audited.get)
    assert state.status == "validated" and state.human_review == "accepted"
    assert state.sampling == "adjudicated"
    assert state.chosen_attempt == events[0].attempt_key  # the ruled candidate stands
    assert _eligible(state) is True


def test_an_accept_on_an_agreeing_cohort_is_the_validator_15_no_op() -> None:
    """The scope of that arm: only an ADJUDICATED validation reads an accept,
    because only there does an artifact move the status under a ruling a human
    had already filed. An accept on a cohort that agreed on its own is the same
    no-op the hookless fold performs — which is what keeps v1 byte-identical."""
    events = [_slot(1, 1), _slot(2, 2)]
    accept = [Review("accept", "2026-08-27T06:12:08Z", key="r1")]
    assert derive_state(events, accept, GLOBS, _cohort(True, ()), _audits(CLEAN)) == (
        derive_state(events, [], GLOBS, _cohort(True, ()), _audits(CLEAN))
    )
    assert derive_state(events, accept, GLOBS, _cohort(True, ())) == derive_state(
        events, [], GLOBS, _cohort(True, ())
    )


# (id, slots, cohort passed, cohort failures, the sampling dim before the review)
REOPENED = [
    ("unsampled", 1, True, (), "not_requested"),
    ("agreeing", 2, True, (), "complete"),
    ("adjudicated", 2, False, ("negation",), "adjudicated"),
]


@pytest.mark.parametrize("verb", ["flag", "refute"])
@pytest.mark.parametrize("row", REOPENED, ids=[r[0] for r in REOPENED])
def test_a_reopened_record_is_never_eligible(verb: str, row) -> None:
    """Spec §6: a record parked for review is out of the aggregates, whatever
    its audit said. The demotion is the same event for every cohort shape — an
    unsampled document, an agreeing cohort, an adjudicated disagreement — so
    none of them may keep eligibility once a reviewer (or the refuter) reopens
    it. The audit dimensions stay on record: the auditor's verdict is still a
    fact, it is the settlement that is no longer `validated`."""
    _id, slots, passed, failures, before = row
    events = [_slot(s, s) for s in range(1, slots + 1)]
    settled = derive_state(events, [], GLOBS, _cohort(passed, failures), _audits(CLEAN))
    assert settled.status == "validated" and settled.sampling == before
    assert _eligible(settled) is True  # the record this review demotes

    reopened = derive_state(
        events, [Review(verb, "2026-08-27T06:12:08Z", key="r1")], GLOBS,
        _cohort(passed, failures), _audits(CLEAN),
    )
    assert reopened.status == "needs_review"
    assert (reopened.semantics, reopened.completeness) == ("no_findings", "no_findings")
    assert _eligible(reopened) is False


def test_the_human_disposition_is_part_of_the_fold() -> None:
    """`human_review` (spec §6) is derived here like every other dimension, so
    settlement cannot publish a record as unreviewed that a human rejected —
    the one verdict the policy treats as final on its own."""
    events = [_slot(1, 1), _slot(2, 2)]
    passing, audit = _cohort(True, ()), _audits(CLEAN)
    automated = derive_state(events, [], GLOBS, passing, audit)
    assert automated.human_review == "none" and _eligible(automated) is True

    rejected = derive_state(
        events, [Review("reject", "2026-08-27T06:12:08Z", key="r1")], GLOBS, passing, audit
    )
    assert rejected.status == "rejected" and rejected.human_review == "rejected"
    assert _eligible(rejected) is False
    # final on the dimension alone: a caller that reads the dimensions without
    # the lifecycle still cannot publish a rejected candidate (spec §6, "no
    # human rejection can be overridden automatically")
    assert assess(
        source="usable", evidence="pass", semantics=rejected.semantics,
        completeness=rejected.completeness, sampling=rejected.sampling,
        human_review=rejected.human_review, blocking_findings=rejected.blocking,
    )["search_eligible"] is False

    accepted = derive_state(
        events + [_slot(3, 3, outcome="schema_invalid", record=None)],
        [Review("accept", "2026-08-27T06:12:08Z", key="r1")], GLOBS, HOOK, audit,
    )
    assert accepted.status == "validated" and accepted.human_review == "accepted"

    retried = derive_state(
        events, [Review("retry", "2026-08-27T06:12:08Z", key="r1")], GLOBS, passing, audit
    )
    assert retried.human_review == "none"  # a fresh cohort carries no disposition


# --- validator/18: scoped and incomplete-cohort adjudication ----------------
# The plan's Policy section is normative
# (docs/superpowers/plans/2026-09-13-repair-and-scoped-adjudication.md). Rule 3
# is here (the gate/dimension map); rules 1 and 2 — the dispute set itself —
# are in test_agreement.py, and the refutation fixtures at the bottom of this
# file run the real gate, the real dispute set and this policy together.


@dataclass(frozen=True)
class _DetailedAudit:
    """What `v2.audit.AuditOutcome` shows the fold: the dimensions and blocking
    count `_Audit` carries, plus the two lists validator/18 scopes against."""

    semantics: str
    completeness: str
    blocking: int
    findings: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)


def finding(
    code: str, *targets: str, severity: str = "blocking", block: str | None = None
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "dimension": "semantics",
        "targets": list(targets),
        "evidence": None if block is None else {"block_id": block, "text": "x", "span": [0, 1]},
        "explanation": "e",
    }


DISPUTE = Dispute(frozenset({"s9"}), frozenset({"b9"}))


def test_the_gate_dimension_map_is_the_policy_table() -> None:
    """Validator/20: only two dimensions can fail a cohort, so only two can
    have an audit finding restate them. The map is keyed by what `agree` puts
    in `report["failures"]`, and a demoted dimension never appears there — an
    entry for one would be a rule about a gate that cannot fire. The demoted
    dimensions are still computed and still reported, in `report["metrics"]`,
    which is where the audit's scoping reads them if it ever needs them."""
    assert GATE_DIMENSION_CODES == {
        "negation": ("polarity_subject",),
        "numeric_conflict": ("numeric_scope_unit",),
    }
    assert tuple(GATE_DIMENSION_CODES) == GATES


@pytest.mark.parametrize("gate", sorted(GATE_DIMENSION_CODES))
def test_a_failed_gate_makes_its_own_dimension_touch_the_dispute(gate: str) -> None:
    """Rule 3: a finding in the failed gate's dimension restates the cohort's
    disagreement whatever it points at — the dispute set cannot name a
    disagreement about the MEANING of text both samples cited."""
    for code in GATE_DIMENSION_CODES[gate]:
        off_dispute = [finding(code, "s1")]
        assert audit_touches_dispute(off_dispute, DISPUTE, [gate]) is True
        assert audit_touches_dispute(off_dispute, DISPUTE, []) is False
        # only gates whose dimension set excludes the code must stay silent
        other = [g for g in GATE_DIMENSION_CODES
                 if code not in GATE_DIMENSION_CODES[g]]
        assert audit_touches_dispute(off_dispute, DISPUTE, other) is False


def test_a_demoted_dimension_is_never_a_failed_gate() -> None:
    """The map's silence about a demoted dimension is only safe because `agree`
    can no longer report one as a failure: the pairing is what keeps rule 3
    complete."""
    for dimension in DIMENSIONS:
        assert dimension not in GATE_DIMENSION_CODES
    for _name, build, dimension in REVIEW_SPLITS:
        report = agree(list(build())).report
        assert report["failures"] == []
        assert report["metrics"]["splits"][dimension] > 0


def test_a_blocking_finding_on_a_disputed_target_touches() -> None:
    # a statement id in the medoid's namespace, and a block id from rule 2 —
    # the finding's code is in no failed gate's dimension either time
    assert audit_touches_dispute([finding("mention_linkage", "s9")], DISPUTE, ["f1"]) is True
    assert audit_touches_dispute([finding("bad_exclusion", "b9")], DISPUTE, ["f1"]) is True
    assert audit_touches_dispute([finding("mention_linkage", "s1")], DISPUTE, ["f1"]) is False


def test_a_cited_block_counts_as_a_target() -> None:
    """An omission cites the source it says is missing; that citation is what
    the finding is about, so a disputed block reached through `evidence` is a
    touch exactly as a disputed block in `targets` is."""
    assert audit_touches_dispute(
        [finding("bad_exclusion", "b1", block="b9")], DISPUTE, []
    ) is True
    assert audit_touches_dispute(
        [finding("bad_exclusion", "b1", block="b1")], DISPUTE, []
    ) is False


def test_a_blocking_finding_that_points_at_nothing_touches() -> None:
    assert audit_touches_dispute([finding("unsupported_statement")], DISPUTE, []) is True


def test_warnings_never_touch_the_dispute() -> None:
    assert audit_touches_dispute(
        [finding("omission", "s9", severity="warning")], DISPUTE, ["f1"]
    ) is False
    assert audit_touches_dispute([], DISPUTE, ["f1", "negation", "importance"]) is False


def test_an_unscoped_dispute_is_touched_by_every_blocking_finding() -> None:
    unscoped = Dispute(frozenset(), frozenset(), scoped=False)
    assert audit_touches_dispute([finding("mention_linkage", "s1")], unscoped, []) is True
    assert audit_touches_dispute(
        [finding("mention_linkage", "s1", severity="warning")], unscoped, []
    ) is False


# (id, cohort failures, dispute, audit, status, sampling, eligible)
SCOPED = [
    # the audit found nothing at all: validator/16's row, unchanged
    ("clean", ("f1",), DISPUTE, _DetailedAudit("no_findings", "no_findings", 0),
     "validated", "adjudicated", True),
    # blocking, but off the dispute and out of the failed gate's dimension: the
    # cohort's disagreement is cleared and the finding still gates eligibility
    ("off-dispute", ("f1",), DISPUTE,
     _DetailedAudit("findings", "no_findings", 1, [finding("mention_linkage", "s1")]),
     "validated", "adjudicated", False),
    ("on-dispute", ("f1",), DISPUTE,
     _DetailedAudit("findings", "no_findings", 1, [finding("mention_linkage", "s9")]),
     "needs_review", "disagreement", False),
    ("on-gate-dimension", ("negation",), DISPUTE,
     _DetailedAudit("findings", "no_findings", 1, [finding("polarity_subject", "s1")]),
     "needs_review", "disagreement", False),
    ("warning-only", ("f1",), DISPUTE,
     _DetailedAudit("findings", "no_findings", 0,
                    [finding("wording_redundancy", "s9", severity="warning")]),
     "validated", "adjudicated", False),
    # an unresolved question is a blocking item with no code: it clears only
    # where it names something, and only off the dispute
    ("unresolved-off-dispute", ("f1",), DISPUTE,
     _DetailedAudit("no_findings", "no_findings", 1, [], [{"question": "q?",
                                                           "targets": ["s1"]}]),
     "validated", "adjudicated", False),
    ("unresolved-on-dispute", ("f1",), DISPUTE,
     _DetailedAudit("no_findings", "no_findings", 1, [], [{"question": "q?",
                                                           "targets": ["s9"]}]),
     "needs_review", "disagreement", False),
    ("unresolved-unscoped", ("f1",), DISPUTE,
     _DetailedAudit("no_findings", "no_findings", 1, [], [{"question": "q?", "targets": []}]),
     "needs_review", "disagreement", False),
    # the artifact's own count and its lists disagree: something is not visible
    # here, and an invisible blocking item can never be shown to be off-dispute
    ("uncountable", ("f1",), DISPUTE,
     _DetailedAudit("findings", "no_findings", 3, [finding("mention_linkage", "s1")]),
     "needs_review", "disagreement", False),
    ("error", ("f1",), DISPUTE, _DetailedAudit("error", "error", 0),
     "needs_review", "disagreement", False),
    ("absent", ("f1",), DISPUTE, None, "needs_review", "disagreement", False),
    # no dispute set reported: validator/16's whole-record rule is all there is
    ("no-dispute-clean", ("f1",), None, _DetailedAudit("no_findings", "no_findings", 0),
     "validated", "adjudicated", True),
    ("no-dispute-blocking", ("f1",), None,
     _DetailedAudit("findings", "no_findings", 1, [finding("mention_linkage", "s1")]),
     "needs_review", "disagreement", False),
    # an incomplete cohort adjudicates only on a whole-record clean audit:
    # there is no cohort to scope a dispute against
    ("incomplete-clean", ("sample_failed",), None,
     _DetailedAudit("no_findings", "no_findings", 0), "validated", "adjudicated", True),
    ("incomplete-off-dispute", ("sample_failed",), DISPUTE,
     _DetailedAudit("findings", "no_findings", 1, [finding("mention_linkage", "s1")]),
     "needs_review", "incomplete", False),
    ("incomplete-warning", ("sample_failed",), None,
     _DetailedAudit("findings", "no_findings", 0,
                    [finding("wording_redundancy", "s1", severity="warning")]),
     "needs_review", "incomplete", False),
    ("incomplete-and-disagreeing", ("f1", "sample_failed"), DISPUTE,
     _DetailedAudit("findings", "no_findings", 1, [finding("mention_linkage", "s1")]),
     "needs_review", "incomplete", False),
    ("incomplete-absent", ("sample_failed",), None, None,
     "needs_review", "incomplete", False),
]


@pytest.mark.parametrize("row", SCOPED, ids=[r[0] for r in SCOPED])
def test_the_validator_18_settlement_policy_table(row) -> None:
    _id, failures, dispute, audit, status, sampling, eligible = row
    events = [_slot(1, 1), _slot(2, 2)]
    state = derive_state(
        events, [], GLOBS, _cohort(False, failures, dispute), _audits(audit)
    )
    assert (state.status, state.sampling) == (status, sampling)
    assert _eligible(state) is eligible
    assert state.chosen_attempt == events[0].attempt_key


def test_a_legacy_audit_view_can_never_be_scoped() -> None:
    """`_Audit` is the three-field view the archive reader builds today. It
    cannot enumerate its findings, so its blocking count stands on its own and
    the fold stays at validator/16 — a reader that grows the lists is what
    turns scoped adjudication on, never a missing field read as "no findings"."""
    events = [_slot(1, 1), _slot(2, 2)]
    cohort = _cohort(False, ("f1",), DISPUTE)
    state = derive_state(events, [], GLOBS, cohort, _audits(BLOCKING_FINDING))
    assert (state.status, state.sampling) == ("needs_review", "disagreement")
    clean = derive_state(events, [], GLOBS, cohort, _audits(CLEAN))
    assert (clean.status, clean.sampling) == ("validated", "adjudicated")


def test_a_reopened_incomplete_adjudication_goes_back_to_incomplete() -> None:
    """What the reviewer reopened is what goes back on record: an incomplete
    cohort is not a disagreement, and a flag must not relabel it as one."""
    events = [_slot(1, 1), _slot(2, 2)]
    cohort = _cohort(False, ("sample_failed",))
    settled = derive_state(events, [], GLOBS, cohort, _audits(CLEAN))
    assert (settled.status, settled.sampling) == ("validated", "adjudicated")
    reopened = derive_state(
        events, [Review("flag", "2026-08-27T06:12:08Z", key="r1")], GLOBS,
        cohort, _audits(CLEAN),
    )
    assert (reopened.status, reopened.sampling) == ("needs_review", "incomplete")
    assert _eligible(reopened) is False


# --- the five refutation shapes (2026-09-13 adversarial verification) --------
# Naive statement-id overlap between finder samples false-cleared 4 of 9 docs.
# Each row below is one refuted shape, run through the REAL agreement gate and
# the REAL dispute set over v2 samples — only the audit is scripted.
#
# Under validator/20 a cohort only OPENS a dispute when it fails one of the two
# gates, so every shape below carries a polarity split on the statement both
# samples align, alongside the structural disagreement it was written for. That
# is not a weakening of the shape: the dispute set is still built by rules 1 and
# 2 from the unaligned statements and the sibling-only blocks, and it is still
# what decides whether the audit's blocking finding clears the cohort. What
# changed is only which disagreements are worth asking the question about.


def _v2_cohort(medoid, sibling, audit):
    events = [_slot(1, 1, record=medoid), _slot(2, 2, record=sibling)]
    return derive_state(events, [], GLOBS, HOOK, _audits(audit))


def _blocking(*findings: dict[str, Any]):
    return _DetailedAudit("findings", "no_findings", len(findings), list(findings))


def test_0df0f921_a_finding_on_an_unaligned_medoid_statement_is_not_clearable() -> None:
    """The finder's targets overlap the medoid statement no sibling aligns —
    the dispute in the medoid's own namespace, which cross-sample id matching
    never produced."""
    medoid = sample(statement("m_a", (0, 100)), statement("m_b", (200, 300), block="b2"))
    sibling = sample(statement("s_a", (0, 100), polarity="negative"))
    parked = _v2_cohort(medoid, sibling, _blocking(finding("mention_linkage", "m_b")))
    assert parked.agreement and parked.agreement["failures"] == ["negation"]
    assert (parked.status, parked.sampling) == ("needs_review", "disagreement")
    cleared = _v2_cohort(medoid, sibling, _blocking(finding("mention_linkage", "m_a")))
    assert (cleared.status, cleared.sampling) == ("validated", "adjudicated")


def test_9d59cb88_an_omission_on_a_sibling_only_block_is_not_clearable() -> None:
    """Every medoid statement aligns, so a statement-id dispute set is empty
    and naive matching clears the doc. What the samples split on is a block the
    medoid never cited, and the finding points straight at it."""
    medoid = sample(statement("m_a", (0, 100), block="b1"))
    sibling = sample(
        statement("s_a", (0, 100), block="b1", polarity="negative"),
        statement("s_b", (400, 500), block="b7"),
    )
    parked = _v2_cohort(medoid, sibling, _blocking(finding("bad_exclusion", "b7")))
    assert (parked.status, parked.sampling) == ("needs_review", "disagreement")
    cleared = _v2_cohort(medoid, sibling, _blocking(finding("bad_exclusion", "b1")))
    assert (cleared.status, cleared.sampling) == ("validated", "adjudicated")


def test_7b354fd4_a_shared_id_string_is_not_agreement() -> None:
    """Both samples name a statement `s1`. They are different statements about
    different text, and the medoid's is the one in dispute."""
    medoid = sample(statement("s1", (0, 100), block="b1"),
                    statement("s2", (200, 300)))
    sibling = sample(statement("s1", (200, 300), polarity="negative"),
                     statement("s2", (900, 1000), block="b9"))
    parked = _v2_cohort(medoid, sibling, _blocking(finding("mention_linkage", "s1")))
    assert (parked.status, parked.sampling) == ("needs_review", "disagreement")
    cleared = _v2_cohort(medoid, sibling, _blocking(finding("mention_linkage", "s2")))
    assert (cleared.status, cleared.sampling) == ("validated", "adjudicated")


def test_27c9a9af_a_polarity_split_is_carried_by_the_gate_dimension() -> None:
    """Both samples cite the same text and read its polarity differently: every
    claim aligns, so the dispute set is empty by construction and only rule 3
    stands between the cohort and a false clear.

    This shape was an IMPORTANCE split under validator/19. The field is gone
    with schema 3 and the dimension went with it, so the surviving question is
    the same one about polarity — and `polarity_subject` is the audit code that
    restates it.
    """
    medoid = sample(statement("m_a", (0, 100)))
    sibling = sample(statement("s_a", (0, 100), polarity="negative"))
    parked = _v2_cohort(medoid, sibling, _blocking(finding("polarity_subject", "m_a")))
    assert parked.agreement and parked.agreement["failures"] == ["negation"]
    assert (parked.status, parked.sampling) == ("needs_review", "disagreement")
    # a finding in another dimension over an empty dispute is off-dispute
    cleared = _v2_cohort(medoid, sibling, _blocking(finding("mention_linkage", "m_a")))
    assert (cleared.status, cleared.sampling) == ("validated", "adjudicated")


def test_7f4303c2_an_f1_split_no_longer_parks_a_document_at_all() -> None:
    """Under validator/19 this cohort failed statement-set F1 and `omission`
    was that failure's own dimension, so rule 3 kept the document. Under 20 F1
    is a metric: the medoid found one more statement than its sibling, which is
    thoroughness variance, and the cohort never opens a dispute to scope.

    The auditor's omission has not stopped mattering — it still counts as a
    blocking finding and still takes `search_eligible` away. It stopped
    deciding whether the record publishes at all.
    """
    medoid = sample(statement("m_a", (0, 100), block="b1"), statement("m_b", (200, 300)))
    sibling = sample(statement("s_a", (0, 100), block="b1"))
    settled = _v2_cohort(medoid, sibling, _blocking(finding("omission", "b1")))
    assert settled.agreement and settled.agreement["failures"] == []
    assert settled.agreement["metrics"]["f1"] < 1.0
    assert (settled.status, settled.sampling) == ("validated", "complete")
    assert _eligible(settled) is False  # the finding still gates eligibility


# --- validator/20 settlement: the 2026-09-22 review classes -----------------


@pytest.mark.parametrize(
    "name,build,dimension", REVIEW_SPLITS, ids=[r[0] for r in REVIEW_SPLITS]
)
def test_a_review_split_cohort_settles_validated_under_20(
    name: str, build: Any, dimension: str
) -> None:
    """Each of these parked a document under validator/19 — 294 of the 300
    sampled review documents were exactly this — and each settles under 20 with
    the split carried in the report's metrics instead."""
    a, b = build()
    events = [_slot(1, 1, record=a), _slot(2, 2, record=b)]
    state = derive_state(events, [], GLOBS, HOOK)
    assert (state.status, state.sampling) == ("validated", "complete")
    assert state.agreement and state.agreement["failures"] == []
    assert state.agreement["metrics"]["splits"][dimension] > 0


def test_a_polarity_split_still_settles_needs_review() -> None:
    a = sample(statement3("s1", (0, 100)))
    b = sample(statement3("s1", (0, 100), polarity="negative"))
    events = [_slot(1, 1, record=a), _slot(2, 2, record=b)]
    state = derive_state(events, [], GLOBS, HOOK)
    assert (state.status, state.sampling) == ("needs_review", "disagreement")
    assert state.agreement and state.agreement["failures"] == ["negation"]


def test_a_numeric_conflict_still_settles_needs_review() -> None:
    from tests.l2.test_agreement import experience

    def with_months(months: int) -> dict[str, Any]:
        return sample(
            statement3("s1", (0, 100), fact_ids=("f1",)),
            entries=[experience("f1", (0, 100), months=months, sids=["s1"])],
        )

    events = [_slot(1, 1, record=with_months(144)), _slot(2, 2, record=with_months(12))]
    state = derive_state(events, [], GLOBS, HOOK)
    assert (state.status, state.sampling) == ("needs_review", "disagreement")
    assert state.agreement and state.agreement["failures"] == ["numeric_conflict"]


# --- validator/20, 2026-09-28 amendment: the gates compare meaning -----------
# The five shapes the 2026-09-28 review-queue analysis named, folded through
# the production comparator on schema-3 cohorts (the v11 partition). Three of
# them parked under the label-reading gates and settle now; the two real
# misreads the gates exist for still park. What settles still says what it
# split on, in `metrics.splits`.


def _fold(a: dict[str, Any], b: dict[str, Any]) -> DerivedState:
    events = [_slot(1, 1, record=a), _slot(2, 2, record=b), _slot(3, 3, record=b)]
    return derive_state(events, [], GLOBS, HOOK)


def _travel(polarity: str) -> dict[str, Any]:
    return sample(statement3("s_travel", (0, 29), kind="employment_constraint",
                             subject="role", polarity=polarity), schema="3")


def test_a_may_require_travel_hedge_split_settles_validated() -> None:
    """"This role may require travel": the hedge against the assertion."""
    state = _fold(_travel("positive"), _travel("ambiguous"))
    assert (state.status, state.sampling) == ("validated", "complete")
    assert state.agreement and state.agreement["failures"] == []
    assert state.agreement["metrics"]["splits"]["polarity"] == 2


def test_an_on_site_against_remote_not_considered_split_in_hiring_policy_settles() -> None:
    """"We hire for on-site roles only" and "remote work will not be
    considered" are one policy read from opposite ends."""
    def onsite(polarity: str) -> dict[str, Any]:
        return sample(statement3("s_onsite", (0, 86), kind="hiring_policy",
                                 polarity=polarity), schema="3")

    state = _fold(onsite("positive"), onsite("negative"))
    assert (state.status, state.sampling) == ("validated", "complete")
    assert state.agreement and state.agreement["failures"] == []
    assert state.agreement["metrics"]["splits"]["polarity"] == 2


def test_an_ocaml_not_required_split_read_positive_still_parks() -> None:
    """"We don't expect you to know OCaml" read as an OCaml requirement: the
    real flip, on a qualification. It parks exactly as before."""
    def ocaml(polarity: str) -> dict[str, Any]:
        return sample(statement3("s_ocaml", (0, 60), polarity=polarity), schema="3")

    state = _fold(ocaml("negative"), ocaml("positive"))
    assert (state.status, state.sampling) == ("needs_review", "disagreement")
    assert state.agreement and state.agreement["failures"] == ["negation"]


def test_a_salary_period_tag_split_settles_validated() -> None:
    """"$240,000–$315,000 USD/year" against the same numbers with period
    null: the numbers agree and the tag is reported."""
    from tests.l2.test_agreement import compensation

    def pay(period: str | None) -> dict[str, Any]:
        return sample(
            statement3("s_pay", (0, 67), kind="compensation_statement", fact_ids=("f_pay",)),
            entries=[compensation("f_pay", (39, 57), sids=["s_pay"], lo="240000",
                                  hi="315000", period=period)],
            schema="3",
        )

    state = _fold(pay("year"), pay(None))
    assert (state.status, state.sampling) == ("validated", "complete")
    assert state.agreement and state.agreement["failures"] == []
    assert state.agreement["numeric_conflicts"] == 0
    assert state.agreement["metrics"]["splits"]["numeric_tags"] == 4  # statement + fact, x2


def test_five_against_eight_years_on_one_span_still_splits_the_numeric_gate() -> None:
    """"5+ years ..., or 8+ years for the Staff level": one bullet, two
    different numbers read off it. A misread, and it parks."""
    from tests.l2.test_agreement import experience

    def years(months: int, at: tuple[int, int]) -> dict[str, Any]:
        return sample(statement3("s_years", (0, 93), fact_ids=("f_years",)),
                      entries=[experience("f_years", at, months=months, sids=["s_years"])],
                      schema="3")

    state = _fold(years(60, (2, 10)), years(96, (63, 71)))
    assert (state.status, state.sampling) == ("needs_review", "disagreement")
    assert state.agreement and state.agreement["failures"] == ["numeric_conflict"]


# --- validator/20: an exhausted sample budget is not a verdict --------------
# Parsing contract v3 §3: "a document with at least one assembled-and-verified
# candidate is validated. `needs_review` is reserved for the two gate failures
# above and for human parking" — and §5: "the 373-of-1,000 'incomplete cohort'
# review class was nothing but exhausted sample budgets". A missing sample is
# monitoring information: the cohort says so in `quality.sample_notes`, and the
# document publishes. A V1 cohort keeps validator/12's policy, unchanged.

#: a slot that never came back: no record, nothing in glob, and the slot still
#: counted as attempted — which is exactly what `sample_failed` reports
LOST: dict[str, Any] = {
    "outcome": "transport", "record": None, "raw_response": None, "observed_model": None,
}


def _v3(slot: int, no: int, **over: Any) -> Any:
    """One slot of a SCHEMA-3 cohort: the shape `agreement._gates` reads as
    validator/20's."""
    over = {"record": sample(statement3("s1", (0, 100)), schema="3"), **over}
    return _slot(slot, no, **over)


def test_an_incomplete_v3_cohort_settles_validated() -> None:
    """One of three requested samples arrived, it is assembled and verified,
    and nothing it could be compared against disagreed with it. Under 19 that
    parked the document; under 20 it publishes and the cohort reports what it
    could not measure."""
    events = [_v3(1, 1), _v3(2, 2, **LOST), _v3(3, 3, **LOST)]
    state = derive_state(events, [], GLOBS, HOOK)
    assert (state.status, state.sampling) == ("validated", "incomplete")
    assert state.k == 3
    assert state.agreement and state.agreement["failures"] == ["sample_failed"]
    # what `serve._sample_notes` publishes as requested/arrived
    assert (state.agreement["k"], state.agreement["arrived"]) == (3, 1)
    assert state.agreement["metrics"]["splits"] == {
        **dict.fromkeys(DIMENSIONS, 0), "polarity": 0, "numeric_tags": 0,
    }


def test_an_incomplete_v3_cohort_with_a_demoted_split_settles_too() -> None:
    """Two of three arrived and they labelled the same sentence differently.
    `kind` is a metric under 20, so the only failure is the missing sample and
    the split rides along in the report the blob publishes."""
    a = sample(statement3("s1", (0, 100)), schema="3")
    b = sample(statement3("s1", (0, 100), kind="responsibility"), schema="3")
    events = [_slot(1, 1, record=a), _slot(2, 2, record=b), _v3(3, 3, **LOST)]
    state = derive_state(events, [], GLOBS, HOOK)
    assert (state.status, state.sampling) == ("validated", "incomplete")
    assert state.agreement and state.agreement["failures"] == ["sample_failed"]
    assert state.agreement["metrics"]["splits"]["kind"] > 0
    assert (state.agreement["k"], state.agreement["arrived"]) == (3, 2)


def test_a_gate_failure_among_the_arrived_samples_still_parks() -> None:
    """The gates still run over the samples that DID arrive: two of three read
    the same sentence's polarity differently, and that is a parking reason
    whatever the third slot did."""
    a = sample(statement3("s1", (0, 100)), schema="3")
    b = sample(statement3("s1", (0, 100), polarity="negative"), schema="3")
    events = [_slot(1, 1, record=a), _slot(2, 2, record=b), _v3(3, 3, **LOST)]
    state = derive_state(events, [], GLOBS, HOOK)
    assert (state.status, state.sampling) == ("needs_review", "disagreement")
    assert state.agreement and state.agreement["failures"] == ["negation", "sample_failed"]


def test_a_v1_cohort_still_parks_on_an_exhausted_sample_budget() -> None:
    """Validator "12" is frozen: `demand-profile/v5` keeps the policy its
    archived corpus was settled under, so an incomplete v1 cohort parks and
    only validator/18's whole-record adjudication lets it out."""
    v1 = v1_profile(claim((0, 100)))
    events = [_slot(1, 1, record=v1), _slot(2, 2, **LOST), _slot(3, 3, **LOST)]
    state = derive_state(events, [], GLOBS, HOOK)
    assert (state.status, state.sampling) == ("needs_review", "incomplete")
    adjudicated = derive_state(events, [], GLOBS, HOOK, _audits(CLEAN))
    assert (adjudicated.status, adjudicated.sampling) == ("validated", "adjudicated")


def test_a_cohort_whose_policy_cannot_be_read_parks_as_before() -> None:
    """No sample resolved at all, so nothing declares a contract. A cohort this
    fold cannot identify is never the one that parks less (`agreement._gates`),
    so the conservative set applies and the document stays for review."""
    events = [_v3(1, 1, **LOST), _v3(2, 2, **LOST)]
    state = derive_state(events, [], GLOBS, HOOK)
    assert state.status is None  # nothing in glob ever settled it
    ok_but_unreadable = [_v3(1, 1), _v3(2, 2, **LOST)]
    blind = derive_state(ok_but_unreadable, [], GLOBS, cohort_hook(lambda a: None))
    assert (blind.status, blind.sampling) == ("needs_review", "incomplete")


# --- validator/20: a bookkeeping-only exhausted ladder serves (T-Q3S9) -------
# Parsing contract v3 §3: every assembled-and-verified extraction serves. A
# candidate whose ONLY defect is incomplete block bookkeeping bound and verified
# everything it extracted — it is a faithful extraction with a completeness gap.
# The ladder still asks the model to fix the bookkeeping (accounting findings
# are retry-worthy errors); only a ladder that ran out settles on its best such
# candidate instead of quarantining, flagged `completeness: accounting_gaps` and
# never `search_eligible`. The recovery hook stands in for the runner's and the
# replay's shared candidate recovery: an attempt -> its candidate's re-judged
# findings, or None when no candidate can be recovered at all.


def _failed(no: int, *, exhausted: bool | None = None, **over: object):
    key = f"extractions/attempts/2026/09/28T06120{no}Z-abcdefabcdef-s1a{no}.json.gz"
    base: dict[str, object] = {
        "attempt_key": key, "attempt_no": no, "outcome": "attribution_failed",
        "ladder_exhausted": no >= 3 if exhausted is None else exhausted,
        "started_at": f"2026-09-28T06:12:0{no}Z", "record": None, "raw_response": "{}",
    }
    base.update(over)
    return _attempt(**base)


def _acc(code: str = "coverage_unevidenced", *, severity: str = "error",
         block: str = "b000002") -> dict[str, Any]:
    return {"check": "accounting", "path": "block_accounting[1]", "code": code,
            "severity": severity, "detail": {"block_id": block}}


def _candidate(findings: list[dict[str, Any]] | RecoveredCandidate, *, extracted: int = 4,
               blocks: int = 5, accounted: int = 5) -> RecoveredCandidate:
    """A recovered candidate. By default a C04-sized one that extracted the
    document (four objects, every one of five blocks accounted), so a findings
    list alone decides; the counts are overridden where the floor is the test."""
    if isinstance(findings, RecoveredCandidate):
        return findings
    return RecoveredCandidate(tuple(findings), extracted=extracted, blocks=blocks,
                              accounted=accounted)


Scripted = list[dict[str, Any]] | RecoveredCandidate


def _recovered(findings: dict[str, Scripted | None] | Scripted,
               calls: list[str] | None = None):
    """The recovery hook, scripted: one candidate (or findings list) for every
    attempt, or one per attempt key (None = no candidate could be recovered)."""

    def hook(attempt) -> RecoveredCandidate | None:
        if calls is not None:
            calls.append(attempt.attempt_key)
        if isinstance(findings, dict):
            found = findings.get(attempt.attempt_key)
            return _candidate(found) if found is not None else None
        return _candidate(findings)

    return hook


#: the shape the 271 production documents failed on: coverage claims their
#: named objects never quote, plus the requirement-language tripwire warning
BOOKKEEPING_ONLY = [_acc(), _acc(), _acc("context_requirement_language", severity="warning")]


def _assessed(state: DerivedState) -> dict[str, Any]:
    return assess(source="usable", evidence="pass", semantics=state.semantics,
                  completeness=state.completeness, sampling=state.sampling,
                  human_review=state.human_review, blocking_findings=state.blocking,
                  lifecycle=state.status or "pending")


def test_the_bookkeeping_codes_are_the_four_the_verifier_reports() -> None:
    ticket = {
        "coverage_unevidenced", "block_unaccounted",
        "context_requirement_language", "exclusion_requirement_language",
    }
    assert set(BOOKKEEPING_CODES) == ticket
    assert ACCOUNTING_GAPS == "accounting_gaps"


def test_a_bookkeeping_only_exhausted_ladder_settles_validated_with_accounting_gaps() -> None:
    ladder = [_failed(1), _failed(2), _failed(3)]
    state = derive_state(ladder, [], GLOBS, HOOK, recovery_hook=_recovered(BOOKKEEPING_ONLY))
    assert state.status == "validated"
    # three candidates with the same two gaps: the tie goes to the LATEST, the
    # one that took the most rounds of the verifier's feedback
    assert state.chosen_attempt == ladder[2].attempt_key
    assert state.completeness == ACCOUNTING_GAPS
    # the gaps the reader is shown are the failing findings, verbatim
    assert state.accounting_gaps == (_acc(), _acc())
    assert state.semantics == "not_checked" and state.human_review == "none"
    quality = _assessed(state)
    assert quality["completeness"] == "accounting_gaps"
    assert quality["search_eligible"] is False


def test_the_bookkeeping_candidate_with_the_fewest_accounting_gaps_is_chosen() -> None:
    """Fewest failing bookkeeping findings wins — completeness is the dimension
    being flagged, so the most complete candidate is the one to serve. The
    exhausting attempt need not be a candidate itself: a schema-invalid last
    rung leaves the earlier bookkeeping-only candidates on record."""
    ladder = [_failed(1), _failed(2),
              _failed(3, outcome="schema_invalid", raw_response="not json")]
    calls: list[str] = []
    hook = _recovered({
        ladder[0].attempt_key: [_acc(), _acc(), _acc("block_unaccounted")],
        ladder[1].attempt_key: [_acc("block_unaccounted"),
                                _acc("exclusion_requirement_language", severity="warning")],
    }, calls)
    state = derive_state(ladder, [], GLOBS, HOOK, recovery_hook=hook)
    assert state.status == "validated"
    assert state.chosen_attempt == ladder[1].attempt_key
    assert state.accounting_gaps == (_acc("block_unaccounted"),)
    # only content failures that assembled can hold a candidate
    assert sorted(calls) == sorted([ladder[0].attempt_key, ladder[1].attempt_key])


NOT_BOOKKEEPING = [
    ("unknown_reference",
     [_acc(), {"check": "references", "path": "mentions[0].statement_ids",
               "code": "unknown_reference", "severity": "error", "detail": {}}]),
    ("ungrounded_mention",
     [{"check": "mentions", "path": "mentions[0]", "code": "mention_ungrounded",
       "severity": "error", "detail": {}}]),
    ("an_accounting_code_that_is_not_bookkeeping", [_acc("unknown_block")]),
    ("a_bare_error_the_verifier_did_not_report",
     [_acc(), {"error": "statements[0].topic: control character U+0000"}]),
    ("an_unexplained_deletion",
     [_acc(), {"error": "retry:unexplained_deletion at statements[2] (id=s3)"}]),
    ("a_non_accounting_warning",
     [_acc(), {"check": "mentions", "path": "mentions[0]", "code": "future_warning",
               "severity": "warning", "detail": {}}]),
    ("nothing_failing_at_all",
     [_acc("context_requirement_language", severity="warning")]),
    ("no_findings", []),
]


@pytest.mark.parametrize("findings", [f for _, f in NOT_BOOKKEEPING],
                         ids=[name for name, _ in NOT_BOOKKEEPING])
def test_any_non_accounting_finding_keeps_an_exhausted_bookkeeping_ladder_quarantined(
    findings: list[dict[str, Any]],
) -> None:
    """Faithfulness defects never serve: the rule is a whitelist of the four
    bookkeeping codes, and anything it does not recognise quarantines exactly
    as validator/19 did."""
    ladder = [_failed(1), _failed(2), _failed(3)]
    state = derive_state(ladder, [], GLOBS, HOOK, recovery_hook=_recovered(findings))
    assert state.status == "quarantined"
    assert state.chosen_attempt is None and state.accounting_gaps == ()
    assert state.completeness == "not_checked"


def test_an_unrecoverable_candidate_leaves_the_bookkeeping_ladder_quarantined() -> None:
    """No candidate (the raw response will not re-assemble) is not a candidate
    with no findings: there is nothing to serve."""
    ladder = [_failed(1), _failed(2), _failed(3)]
    state = derive_state(ladder, [], GLOBS, HOOK, recovery_hook=_recovered(
        {a.attempt_key: None for a in ladder}))
    assert state.status == "quarantined" and state.chosen_attempt is None


def _unaccounted(n: int) -> list[dict[str, Any]]:
    return [_acc("block_unaccounted", block=f"b00000{i}") for i in range(1, n + 1)]


def test_a_candidate_that_extracted_nothing_is_not_a_bookkeeping_gap() -> None:
    """2026-09-28 review: a candidate with every array empty and no accounting
    row fails NOTHING but `block_unaccounted`, one per block — the findings
    test alone reads it as bookkeeping-only and serves an empty profile as a
    terminal `validated` row. It is a failed extraction, and it quarantines."""
    ladder = [_failed(1), _failed(2), _failed(3)]
    nothing = _candidate(_unaccounted(5), extracted=0, blocks=5, accounted=0)
    state = derive_state(ladder, [], GLOBS, HOOK, recovery_hook=_recovered(nothing))
    assert state.status == "quarantined"
    assert state.chosen_attempt is None and state.accounting_gaps == ()
    assert state.completeness == "not_checked"


@pytest.mark.parametrize(("extracted", "blocks", "accounted", "settles"), [
    (4, 5, 5, True),    # the whole source accounted: the gap is elsewhere
    (4, 5, 3, True),    # most of the source accounted
    (1, 5, 5, True),    # one extracted object is enough content
    (4, 4, 2, False),   # exactly half: not most
    (4, 5, 2, False),   # most of the source never looked at
    (4, 5, 0, False),   # no accounting at all
    (0, 5, 5, False),   # accounted everything, extracted nothing
    (0, 0, 0, False),   # a source with no blocks has no gap to be incomplete by
], ids=["all", "most", "one_object", "half", "minority", "none", "empty", "no_blocks"])
def test_a_bookkeeping_candidate_must_have_extracted_the_document(
    extracted: int, blocks: int, accounted: int, settles: bool
) -> None:
    """The floor under the findings test: at least one statement or fact entry,
    and accounting rows covering strictly more than half of the source."""
    candidate = _candidate([_acc()], extracted=extracted, blocks=blocks, accounted=accounted)
    assert (bookkeeping_gaps(candidate) is not None) is settles
    ladder = [_failed(1), _failed(2), _failed(3)]
    state = derive_state(ladder, [], GLOBS, HOOK, recovery_hook=_recovered(candidate))
    assert state.status == ("validated" if settles else "quarantined")


def test_the_bookkeeping_floor_is_applied_before_the_fewest_gaps_choice() -> None:
    """A candidate below the floor is no candidate at all — it cannot win on
    having the fewest gaps. The ladder settles on the one that extracted the
    document, even with more gaps."""
    ladder = [_failed(1), _failed(2), _failed(3)]
    hook = _recovered({
        ladder[0].attempt_key: _candidate([_acc()], extracted=4, blocks=5, accounted=1),
        ladder[1].attempt_key: _candidate([_acc(), _acc(), _acc()]),
        ladder[2].attempt_key: _candidate(_unaccounted(1), extracted=0, blocks=5, accounted=4),
    })
    state = derive_state(ladder, [], GLOBS, HOOK, recovery_hook=hook)
    assert state.status == "validated"
    assert state.chosen_attempt == ladder[1].attempt_key
    assert state.accounting_gaps == (_acc(), _acc(), _acc())


def test_no_recovery_hook_is_the_validator_19_bookkeeping_fold() -> None:
    """The hook is additive: v1 and every archived partition folded at an older
    validator pass none, and an exhausted ladder quarantines exactly as before."""
    ladder = [_failed(1), _failed(2), _failed(3)]
    assert derive_state(ladder, [], GLOBS, HOOK).status == "quarantined"
    assert derive_state(ladder, [], GLOBS, HOOK) == derive_state(
        ladder, [], GLOBS, HOOK, None, recovery_hook=None)


def test_accounting_findings_stay_retry_worthy_inside_the_ladder() -> None:
    """Known-bad approach: settling (or downgrading) before the ladder ran out
    would stop the model being asked to fix its bookkeeping. A ladder with rungs
    left is pending, and the recovery hook is never even asked."""
    calls: list[str] = []
    ladder = [_failed(1), _failed(2)]
    state = derive_state(ladder, [], GLOBS, HOOK,
                         recovery_hook=_recovered(BOOKKEEPING_ONLY, calls))
    assert state == DerivedState(None, None)
    assert calls == []


def test_the_bookkeeping_rule_reads_only_in_glob_attempts_of_the_current_cohort() -> None:
    """The same admission test an `ok` attempt passes: an answer from a model
    outside the globs is never a candidate, and a `retry` review starts a fresh
    cohort, so a candidate from before it never settles the ladder after it."""
    outside = [_failed(1, observed_model="claude-haiku-4-5"),
               _failed(2, observed_model="claude-haiku-4-5"),
               _failed(3, observed_model="claude-haiku-4-5")]
    calls: list[str] = []
    state = derive_state(outside, [], GLOBS, HOOK,
                         recovery_hook=_recovered(BOOKKEEPING_ONLY, calls))
    assert state.status == "quarantined" and calls == []

    first = [_failed(1), _failed(2), _failed(3)]
    retry = Review("retry", "2026-09-28T07:00:00Z", key="r1")
    second = [
        _failed(n, attempt_key=f"extractions/attempts/2026/09/28T08000{n}Z-abcdefabcdef-s1a{n}"
                ".json.gz", started_at=f"2026-09-28T08:00:0{n}Z")
        for n in (4, 5, 6)
    ]
    second = [replace(a, ladder_exhausted=a.attempt_no == 6) for a in second]
    hook = _recovered({**{a.attempt_key: BOOKKEEPING_ONLY for a in first},
                       **{a.attempt_key: NOT_BOOKKEEPING[0][1] for a in second}})
    before = derive_state(first, [], GLOBS, HOOK, recovery_hook=hook)
    assert before.status == "validated" and before.completeness == ACCOUNTING_GAPS
    after = derive_state([*first, *second], [retry], GLOBS, HOOK, recovery_hook=hook)
    assert after.status == "quarantined" and after.accounting_gaps == ()


def test_human_dispositions_stay_senior_over_a_bookkeeping_settlement() -> None:
    ladder = [_failed(1), _failed(2), _failed(3)]
    hook = _recovered(BOOKKEEPING_ONLY)
    rejected = derive_state(ladder, [Review("reject", "2026-09-28T07:00:00Z", key="r1")],
                            GLOBS, HOOK, recovery_hook=hook)
    assert rejected.status == "rejected" and rejected.human_review == "rejected"
    assert rejected.chosen_attempt == ladder[2].attempt_key  # provenance survives
    assert _assessed(rejected)["search_eligible"] is False
    flagged = derive_state(ladder, [Review("flag", "2026-09-28T07:00:00Z", key="r1")],
                           GLOBS, HOOK, recovery_hook=hook)
    assert flagged.status == "needs_review"
    assert flagged.completeness == ACCOUNTING_GAPS  # still says why it is incomplete
    accepted = derive_state(
        ladder,
        [Review("flag", "2026-09-28T07:00:00Z", key="r1"),
         Review("accept", "2026-09-28T08:00:00Z", key="r2")],
        GLOBS, HOOK, recovery_hook=hook,
    )
    assert accepted.status == "validated" and accepted.human_review == "accepted"
    # a human accept is a ruling on the lifecycle, not a repair of the bookkeeping
    assert accepted.completeness == ACCOUNTING_GAPS
    assert _assessed(accepted)["search_eligible"] is False


def test_an_audit_cannot_lift_an_accounting_gaps_completeness() -> None:
    """Invariant: the audit judges fidelity, and a clean one is still a verdict
    on a record whose bookkeeping failed the verifier. Its semantics are
    reported; completeness stays the verifier's, and eligibility stays off."""
    ladder = [_failed(1), _failed(2), _failed(3)]
    state = derive_state(ladder, [], GLOBS, HOOK, _audits(CLEAN),
                         recovery_hook=_recovered(BOOKKEEPING_ONLY))
    assert state.status == "validated"
    assert state.semantics == "no_findings"
    assert state.completeness == ACCOUNTING_GAPS
    assert _assessed(state)["search_eligible"] is False


def test_an_over_budget_document_has_no_bookkeeping_candidate() -> None:
    calls: list[str] = []
    too_big = _attempt(outcome="over_budget", raw_response=None, observed_model=None,
                       ladder_exhausted=True)
    state = derive_state([too_big], [], GLOBS, HOOK,
                         recovery_hook=_recovered(BOOKKEEPING_ONLY, calls))
    assert state.status == "quarantined" and calls == []
