from jobhunter.l2.state import DerivedState, Review, derive_state
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
