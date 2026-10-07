"""Validator/21 settlement (parsing contract v4 §3, case V7).

Schema-4 cohorts keep validator/20's two gates. The negation gate also reads
the two authorization presence polarities: two samples that both state
`sponsorship` (or `citizenship`) and read it positive in one and negative in
the other split on negation, and the document parks. Everything else schema 4
adds is a metric and never parks: a hedge against a definite reading, a
derived `authorization` value split that is not a polarity split, mention
`type`, track membership and track `selection`.

Samples here are what settlement actually compares: `serve.profile_of` over a
record assembled from the conftest's schema-4 fixtures. A schema-3 cohort is
judged exactly as before; the last tests pin that.
"""

from __future__ import annotations

import copy
from typing import Any

from jobhunter.l2.agreement import SPLITS, agree, cohort_hook
from jobhunter.l2.state import derive_state
from jobhunter.l2.v2.serve import profile_of
from tests.l2.test_state import GLOBS, HOOK, _slot
from tests.l2.v2.conftest import (
    ANDURIL_MD,
    FIGMA_MD,
    LYFT_MD,
    VISA_MD,
    assemble4,
    make_anduril_emit,
    make_figma_emit,
    make_lyft_emit,
    make_visa_emit,
    quote,
    s4_presence,
    whole,
)

#: what a schema-4 cohort reports under `metrics.splits` beyond validator/20's
V21_SPLITS = ("authorization", "mention_type", "track_membership", "track_selection")


def blob(emit: dict[str, Any], markdown: str) -> dict[str, Any]:
    return profile_of(assemble4(emit, markdown))


def visa(polarity: str = "negative", state: str = "stated") -> dict[str, Any]:
    emit = make_visa_emit()
    evidence = [whole("b000004")] if state != "none_found" else None
    marker = {"negative": "will not sponsor", "positive": "sponsor",
              "ambiguous": "sponsor"}[polarity]
    emit["facts"]["presence"]["sponsorship"] = s4_presence(
        state, evidence, polarity if state != "none_found" else None,
        [quote("b000004", marker)] if state != "none_found" else None)
    return blob(emit, VISA_MD)


def anduril(polarity: str) -> dict[str, Any]:
    emit = make_anduril_emit()
    emit["facts"]["presence"]["citizenship"]["polarity"] = polarity
    return blob(emit, ANDURIL_MD)


def figma(mutate: Any = None) -> dict[str, Any]:
    emit = make_figma_emit()
    if mutate is not None:
        mutate(emit)
    return blob(emit, FIGMA_MD)


def splits(result: Any) -> dict[str, int]:
    out: dict[str, int] = result.report["metrics"]["splits"]
    return out


# --- V7: the negation gate reads the authorization polarities ----------------


def test_v7_a_sponsorship_polarity_split_fails_negation() -> None:
    result = agree([visa("negative"), visa("positive")])
    assert result.passed is False
    assert result.report["failures"] == ["negation"]
    assert result.report["negation_disagreements"] == 1
    assert result.report["authorization_negations"] == 1
    # the split is the gate's, so it is not counted again as a metric
    assert splits(result)["authorization"] == 0
    assert splits(result)["polarity"] == 0


def test_v7_the_split_parks_the_document_in_settlement() -> None:
    events = [_slot(1, 1, record=visa("negative")), _slot(2, 2, record=visa("positive"))]
    state = derive_state(events, [], GLOBS, HOOK)
    assert state.status == "needs_review"
    assert state.sampling == "disagreement"
    assert state.agreement is not None and state.agreement["failures"] == ["negation"]


def test_a_citizenship_polarity_split_fails_negation() -> None:
    result = agree([anduril("positive"), anduril("negative")])
    assert result.report["failures"] == ["negation"]
    assert result.report["authorization_negations"] == 1


def test_agreeing_authorization_passes_with_every_new_split_zero() -> None:
    result = agree([visa("negative"), visa("negative")])
    assert result.passed is True
    assert result.report["authorization_negations"] == 0
    assert set(splits(result)) == {*SPLITS, *V21_SPLITS}
    assert all(splits(result)[key] == 0 for key in V21_SPLITS)


def test_each_sample_pair_counts_its_own_authorization_split() -> None:
    result = agree([visa("negative"), visa("positive"), visa("positive")])
    assert result.report["authorization_negations"] == 2
    assert result.report["failures"] == ["negation"]


# --- reported, never gating --------------------------------------------------


def test_an_ambiguous_against_a_definite_polarity_is_a_polarity_metric() -> None:
    """The existing negation check treats a hedge against an assertion as
    `metrics.splits.polarity`, never a gate; the presence polarity does too."""
    result = agree([visa("negative"), visa("ambiguous")])
    assert result.passed is True
    assert result.report["authorization_negations"] == 0
    assert splits(result)["polarity"] == 1
    assert splits(result)["authorization"] == 0


def test_stated_against_none_found_is_an_authorization_metric() -> None:
    result = agree([visa("negative"), visa(state="none_found")])
    assert result.passed is True
    assert result.report["authorization_negations"] == 0
    assert splits(result)["authorization"] == 1


def test_stated_against_unresolved_is_an_authorization_metric() -> None:
    result = agree([anduril("positive"), blob(_anduril_unresolved(), ANDURIL_MD)])
    assert result.passed is True
    assert splits(result)["authorization"] == 1


def _anduril_unresolved() -> dict[str, Any]:
    emit = make_anduril_emit()
    emit["facts"]["presence"]["citizenship"]["state"] = "unresolved"
    return emit


def test_a_mention_type_split_is_a_metric() -> None:
    result = agree([blob(make_lyft_emit("location"), LYFT_MD),
                    blob(make_lyft_emit("organization"), LYFT_MD)])
    assert result.passed is True
    assert splits(result)["mention_type"] == 1


def _move_security_into_backend(emit: dict[str, Any]) -> None:
    items = {item["id"]: item for item in emit["relations"]["tracks"]["items"]}
    items["t_backend"]["statement_ids"] = ["s_backend", "s_security"]
    items["t_security"]["statement_ids"] = []


def test_a_track_membership_split_is_a_metric() -> None:
    result = agree([figma(), figma(_move_security_into_backend)])
    assert result.passed is True
    # Backend gains a block and Security loses one: two aligned tracks differ
    assert splits(result)["track_membership"] == 2
    assert splits(result)["track_selection"] == 0


def test_tracks_one_sample_never_recorded_are_membership_splits() -> None:
    def no_tracks(emit: dict[str, Any]) -> None:
        emit["relations"]["tracks"] = None

    result = agree([figma(), figma(no_tracks)])
    assert result.passed is True
    assert splits(result)["track_membership"] == 4
    assert splits(result)["track_selection"] == 0


def test_a_track_selection_split_is_a_metric() -> None:
    def team_match(emit: dict[str, Any]) -> None:
        emit["relations"]["tracks"]["selection"] = "team_match"

    result = agree([figma(), figma(team_match)])
    assert result.passed is True
    assert splits(result)["track_selection"] == 1
    assert splits(result)["track_membership"] == 0


def test_an_incomplete_schema_4_cohort_reports_the_new_splits_too() -> None:
    events = [_slot(1, 1, record=visa("negative")),
              _slot(2, 2, outcome="schema_invalid", record=None)]
    hook = cohort_hook(lambda a: a.record)
    passed, _, report, _ = hook([e for e in events if e.outcome == "ok"], 2)
    assert passed is False
    assert set(report["metrics"]["splits"]) == {*SPLITS, *V21_SPLITS}
    assert report["authorization_negations"] == 0


# --- validator/20 and earlier are unchanged -----------------------------------


def _as_schema_3(profile: dict[str, Any]) -> dict[str, Any]:
    relabelled = copy.deepcopy(profile)
    relabelled["schema"] = "3"
    return relabelled


def test_a_schema_3_cohort_never_reads_the_authorization_polarity() -> None:
    """The same two blobs under a schema-3 marker: validator/20 has no
    authorization family, so nothing new is compared and the report keeps its
    validator/20 keys exactly."""
    result = agree([_as_schema_3(visa("negative")), _as_schema_3(visa("positive"))])
    assert result.passed is True
    assert result.report["failures"] == []
    assert "authorization_negations" not in result.report
    assert set(splits(result)) == set(SPLITS)


def test_a_schema_3_incomplete_cohort_keeps_its_report_keys() -> None:
    hook = cohort_hook(lambda a: a.record)
    events = [_slot(1, 1, record=_as_schema_3(visa("negative")))]
    _, _, report, _ = hook(events, 2)
    assert "authorization_negations" not in report
    assert set(report["metrics"]["splits"]) == set(SPLITS)


def test_a_mixed_cohort_is_judged_under_validator_20() -> None:
    result = agree([visa("negative"), _as_schema_3(visa("positive"))])
    assert result.passed is True
    assert "authorization_negations" not in result.report
