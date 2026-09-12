from dataclasses import dataclass

import pytest

from jobhunter.l2.agreement import cohort_hook
from jobhunter.l2.state import DerivedState, Review, derive_state
from jobhunter.l2.v2.quality import assess
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


def _cohort(passed: bool, failures: tuple[str, ...] = ()):
    """The agreement gate, scripted: the comparator is tested in test_agreement."""

    def hook(ok_attempts, slots_attempted):
        report = {"k": slots_attempted, "failures": list(failures), "mean_f1": None}
        return (passed, ok_attempts[0].attempt_key, report)

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
    ("incomplete+clean", 2, False, ("sample_failed",), CLEAN,
     "needs_review", "incomplete", "no_findings", "no_findings", 0, False),
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
        return (False, medoid.attempt_key, {"k": slots_attempted, "failures": ["f1"]})

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
        return (False, medoid.attempt_key, {"k": slots_attempted, "failures": ["negation"]})

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
