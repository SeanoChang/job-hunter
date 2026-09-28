"""Integration: a bookkeeping-only exhausted ladder serves (validator/20, T-Q3S9).

271 of the 706 documents quarantined under the active tuple on 2026-09-28 ran
their ladder out failing ONLY block-accounting checks — every statement,
mention, fact and reference bound and verified. Under parsing contract v3
("every verified extraction serves") that candidate is a faithful extraction
with a completeness gap, so a drain now settles it `validated`, serves its
mentions, flags `quality.completeness: accounting_gaps` with the gaps beside it,
and keeps it out of `search_eligible`. Inside the ladder nothing changes: the
accounting errors are still fed back for the model to fix.

C04 is the fixture: its `b000002` accounting row is pointed at the CPA
statement, which quotes `b000003`, so the claim "b000002 was extracted into
s_cpa" is `accounting:coverage_unevidenced` and nothing else fails.
"""

from __future__ import annotations

import copy
from typing import Any

import psycopg
import pytest

from jobhunter.archive.base import ArchiveStore
from jobhunter.l2.bundles import get_bundle
from jobhunter.l2.rebuild import rebuild_extractions
from jobhunter.l2.runner import run, settle
from jobhunter.timeutil import iso, utcnow_precise
from tests.l2.test_runner import store  # noqa: F401
from tests.l2.test_runner_v2 import (
    C04_ROWS,
    GLOBS,
    AuditingEngine,
    attempts_in,
    broken_c09,
    clean_audit,
    collapsed_c09,
    emit_of,
    mention_rows_in,
    reaudit_window,
    result,
    row_of,
    seed_case,
    settled_rows,
    v2_settings,
)

Conn = psycopg.Connection[dict[str, Any]]


def bookkeeping_gap(emit: dict[str, Any]) -> dict[str, Any]:
    """C04 with one coverage claim its named object never quotes."""
    gapped = copy.deepcopy(emit)
    for row in gapped["block_accounting"]:
        if row["block_id"] == "b000002" and row["disposition"] == "statements":
            row["ref_ids"] = ["s_cpa"]
    return gapped


def unknown_reference(emit: dict[str, Any]) -> dict[str, Any]:
    """The same gap PLUS a mention linked to a statement that does not exist:
    `references:unknown_reference` is a faithfulness defect, never bookkeeping."""
    broken = bookkeeping_gap(emit)
    broken["mentions"][0]["statement_ids"] = ["s_missing"]
    return broken


def test_a_bookkeeping_only_ladder_settles_validated_and_serves_its_mentions(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    dh = seed_case(pg, "C04")
    engine = AuditingEngine([result(bookkeeping_gap(emit_of("C04")))] * 3, clean_audit)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert (summary.validated, summary.quarantined) == (1, 0)

    archived = attempts_in(store)
    assert [a.outcome for a in archived] == ["attribution_failed"] * 3
    assert archived[-1].ladder_exhausted is True
    assert all(a.record is None for a in archived)  # the archive shape is unchanged

    row = row_of(pg)
    assert row["document_hash"] == dh
    assert row["status"] == "validated"
    # three identical candidates: the tie goes to the latest
    assert row["chosen_attempt"] == archived[-1].attempt_key
    profile = row["profile"]
    assert profile["schema"] == "3"
    quality = profile["quality"]
    assert quality["completeness"] == "accounting_gaps"
    assert quality["search_eligible"] is False
    assert quality["evidence"] == "pass" and quality["semantics"] == "not_checked"
    gaps = quality["accounting_gaps"]
    assert [(g["check"], g["code"], g["severity"]) for g in gaps] == [
        ("accounting", "coverage_unevidenced", "error"),
    ]
    assert gaps[0]["detail"]["block_id"] == "b000002"
    # the skill listing serves for every validated record (two-tier serving)
    assert mention_rows_in(pg) == C04_ROWS
    # never audited — no verdict can lift a verifier's completeness gap, so the
    # call would buy nothing — and nothing is owed for later
    assert engine.audits == []
    assert row["flags"] == {"audit": "not_checked", "audit_version": row["flags"]["audit_version"]}
    assert reaudit_window(pg, limit=10) == []


def test_a_ladder_failing_on_an_unknown_reference_still_quarantines(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    seed_case(pg, "C04")
    engine = AuditingEngine([result(unknown_reference(emit_of("C04")))] * 3, clean_audit)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert (summary.validated, summary.quarantined) == (0, 1)
    row = row_of(pg)
    assert row["status"] == "quarantined"
    assert row["profile"] is None and row["chosen_attempt"] is None
    assert mention_rows_in(pg) == []
    assert engine.audits == []


def test_the_retry_prompt_still_names_the_accounting_errors(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Accounting findings stay retry-worthy: attempts 2 and 3 are asked to fix
    exactly the bookkeeping error, and the drain only settles once they did not."""
    seed_case(pg, "C04")
    engine = AuditingEngine([result(bookkeeping_gap(emit_of("C04")))] * 3, clean_audit)
    run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    archived = attempts_in(store)
    assert archived[0].prior_errors == []
    for later in archived[1:]:
        assert any(e.startswith("accounting:coverage_unevidenced") for e in later.prior_errors)
        assert not any(e.startswith("references:") for e in later.prior_errors)


def test_a_later_clean_attempt_is_still_the_candidate_that_serves(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The rule only ever speaks for an EXHAUSTED ladder: a model that fixes its
    bookkeeping on the second attempt settles as an ordinary `ok`, audited and
    eligible, with no gaps on record."""
    seed_case(pg, "C04")
    engine = AuditingEngine(
        [result(bookkeeping_gap(emit_of("C04"))), result(emit_of("C04"))], clean_audit
    )
    run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    row = row_of(pg)
    assert row["status"] == "validated"
    assert row["chosen_attempt"] == attempts_in(store)[-1].attempt_key
    quality = row["profile"]["quality"]
    assert quality["completeness"] == "no_findings" and quality["search_eligible"] is True
    assert "accounting_gaps" not in quality


def test_a_bookkeeping_settlement_replays_identically_from_the_archive(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """One shared rule: a cold catch-up (the live `settle` over archived
    attempts) and a re-fold of the settled row both reach the drain's verdict,
    with no engine call — the candidate is recovered from the archived raw
    response, never from the drain's memory."""
    dh = seed_case(pg, "C04")
    engine = AuditingEngine([result(bookkeeping_gap(emit_of("C04")))] * 3, clean_audit)
    run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    before = settled_rows(pg)
    status, _, _, quality = before[dh]
    assert (status, quality["completeness"]) == ("validated", "accounting_gaps")
    mentions_before = mention_rows_in(pg)
    assert mentions_before == C04_ROWS
    flags_before = row_of(pg)["flags"]

    settle(pg, store, dh, GLOBS, iso(utcnow_precise()), bundle=get_bundle("v2"))
    pg.commit()
    assert settled_rows(pg) == before and row_of(pg)["flags"] == flags_before

    pg.execute("TRUNCATE extraction_attempts, extraction_reviews, extractions, profile_mentions")
    pg.commit()
    silent = AuditingEngine([], clean_audit)
    summary = run(v2_settings(), pg, store, engine=silent, max_docs=0, max_usd=0.0)
    assert silent.calls == [] and silent.audits == []
    assert summary.replayed > 0
    assert settled_rows(pg) == before
    assert mention_rows_in(pg) == mentions_before


# --- what the rule must NOT settle (2026-09-28 review of T-Q3S9) -------------


def extracted_nothing(emit: dict[str, Any]) -> dict[str, Any]:
    """C04 with every array emptied and no block accounted: the verifier finds
    nothing to fault in content that does not exist, so ALL it reports is one
    `accounting:block_unaccounted` per block. A total extraction failure, not
    a completeness gap."""
    empty = copy.deepcopy(emit)
    empty["statements"], empty["mentions"], empty["areas"] = [], [], []
    empty["facts"]["entries"] = []
    for family in empty["facts"]["presence"].values():
        family["state"], family["evidence"] = "none_found", None
    empty["relations"] = {"groups": [], "conditions": [], "example_sets": []}
    empty["block_accounting"] = []
    return empty


def most_blocks_unaccounted(emit: dict[str, Any]) -> dict[str, Any]:
    """C04 with its content intact but only two of its five blocks accounted:
    three `accounting:block_unaccounted` and nothing else."""
    sparse = copy.deepcopy(emit)
    sparse["block_accounting"] = [
        row for row in sparse["block_accounting"] if row["block_id"] in ("b000002", "b000003")
    ]
    return sparse


@pytest.mark.parametrize("emit", [extracted_nothing, most_blocks_unaccounted],
                         ids=["extracted_nothing", "most_blocks_unaccounted"])
def test_a_candidate_that_did_not_extract_the_document_never_settles(
    pg: Conn, store: ArchiveStore, emit: Any  # noqa: F811
) -> None:
    """Bookkeeping-only is not enough on its own: a candidate that extracted
    nothing, or left most of the source unaccounted, fails ONLY accounting
    checks and is still not a faithful extraction with a completeness gap.
    It quarantines, exactly as validator/19 did, and serves nothing."""
    seed_case(pg, "C04")
    engine = AuditingEngine([result(emit(emit_of("C04")))] * 3, clean_audit)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    archived = attempts_in(store)
    assert [a.outcome for a in archived] == ["attribution_failed"] * 3
    # every finding really is bookkeeping: the floor, not a finding, decides
    assert {(v["check"], v["code"]) for v in archived[-1].validation if "check" in v} == {
        ("accounting", "block_unaccounted")
    }
    assert (summary.validated, summary.quarantined) == (0, 1)
    row = row_of(pg)
    assert row["status"] == "quarantined"
    assert row["profile"] is None and row["chosen_attempt"] is None
    assert mention_rows_in(pg) == []


def c09_coverage_gap(emit: dict[str, Any]) -> dict[str, Any]:
    """C09 claiming its heading block was extracted into a statement that
    quotes only the bullet below it: one `accounting:coverage_unevidenced`."""
    gapped = copy.deepcopy(emit)
    for row in gapped["block_accounting"]:
        if row["block_id"] == "b000001":
            row["disposition"], row["ref_ids"] = "statements", ["s_degree"]
    return gapped


def test_a_bookkeeping_candidate_that_dropped_unnamed_work_never_settles(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The one defect the verifier cannot see. Attempt 1 fails assembly (one
    unbound reference on `statements[0]`); attempts 2 and 3 fix it and silently
    drop the group and condition no error named. The verifier finds nothing on
    them but the bookkeeping gap — the deletion is recorded only in the ARCHIVED
    validation, as a bare `retry:unexplained_deletion` error — so the recovery
    has to carry it across, or the lossy candidate would settle `validated`.
    Both the drain and the replay recover from the archive, so both are pinned."""
    seed_case(pg, "C09")
    engine = AuditingEngine(
        [result(c09_coverage_gap(broken_c09()))]
        + [result(c09_coverage_gap(collapsed_c09()))] * 2,
        clean_audit,
    )
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    archived = attempts_in(store)
    assert [a.outcome for a in archived] == ["attribution_failed"] * 3
    for later in archived[1:]:
        checks = {(v["check"], v["code"]) for v in later.validation if "check" in v}
        assert checks == {("accounting", "coverage_unevidenced")}
        assert [v["error"] for v in later.validation
                if str(v.get("error", "")).startswith("retry:")] == [
            "retry:unexplained_deletion at relations.groups[0] (id=g_education_route)",
            "retry:unexplained_deletion at relations.conditions[0] (id=c_equivalent_route)",
        ]
    assert (summary.validated, summary.quarantined) == (0, 1)
    live = row_of(pg)
    assert live["status"] == "quarantined" and live["profile"] is None

    rebuild_extractions(pg, store, GLOBS)
    pg.commit()
    replayed = row_of(pg)
    assert replayed["status"] == "quarantined" and replayed["profile"] is None
    assert replayed["chosen_attempt"] is None
    assert mention_rows_in(pg) == []


def test_a_validator_19_fold_of_a_bookkeeping_ladder_keeps_quarantining(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The rule is validator 20's settlement policy. A live catch-up that folds
    rows still keyed to validator 19 (`_settle_all` -> `settle(validator_version=
    "19")`) folds under validator 19's rule, which quarantined an exhausted
    ladder — the frozen tuple keeps its verdicts byte for byte."""
    dh = seed_case(pg, "C04")
    engine = AuditingEngine([result(bookkeeping_gap(emit_of("C04")))] * 3, clean_audit)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1  # at validator 20 the ladder serves

    pg.execute("UPDATE extraction_attempts SET validator_version='19' WHERE document_hash=%s",
               (dh,))
    pg.commit()
    state = settle(pg, store, dh, GLOBS, iso(utcnow_precise()),
                   bundle=get_bundle("v2"), validator_version="19")
    pg.commit()
    assert state.status == "quarantined"
    assert state.chosen_attempt is None and state.accounting_gaps == ()
    frozen = pg.execute(
        "SELECT status, profile FROM extractions WHERE validator_version='19'"
    ).fetchone()
    assert frozen is not None
    assert frozen["status"] == "quarantined" and frozen["profile"] is None
