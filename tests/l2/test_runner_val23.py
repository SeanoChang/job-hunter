"""validator/23: the bookkeeping rule serves bundle v3 too.

The validator/20 rule (T-Q3S9) settles an exhausted ladder whose only defect is
block bookkeeping as `validated` with `completeness: accounting_gaps`, instead
of quarantining a faithful extraction. Its gate named bundle v2 — the only
bundle of the v2 family when it was written (2026-09-28) — so schema 4 lost it
silently: on the 2026-10-09 v15 run 32 quarantined documents held such a
candidate. 25 more were blocked by the schema-4 skill-link warning
(`skill_outside_demand`), which a passing candidate already carries without
consequence; the rule now tolerates that warning and nothing else outside
`accounting`.
"""

from __future__ import annotations

import copy
from typing import Any

import psycopg

from jobhunter.archive.base import ArchiveStore
from jobhunter.hashing import sha256_hex
from jobhunter.l2.bundles import get_bundle
from jobhunter.l2.runner import _bookkeeping_rule, run
from jobhunter.l2.state import RecoveredCandidate, bookkeeping_gaps
from tests.l2.test_runner import _seed_doc, _settings, store  # noqa: F401
from tests.l2.test_runner_v2 import AuditingEngine, attempts_in, clean_audit, result
from tests.l2.v2.conftest import VISA_MD, make_visa_emit

Conn = psycopg.Connection[dict[str, Any]]


def test_the_rule_runs_for_bundles_v2_and_v3_at_their_own_validator() -> None:
    v2, v3 = get_bundle("v2"), get_bundle("v3")
    assert _bookkeeping_rule(v2, v2.validator_version)
    assert _bookkeeping_rule(v3, "24")
    assert not _bookkeeping_rule(v3, "22")  # a partition at an older validator keeps its rule
    assert not _bookkeeping_rule(get_bundle("v1"), get_bundle("v1").validator_version)


def _finding(check: str, code: str, severity: str = "error") -> dict[str, Any]:
    return {"check": check, "path": "p", "code": code, "severity": severity, "detail": {}}


def _cand(*findings: dict[str, Any]) -> RecoveredCandidate:
    return RecoveredCandidate(findings, extracted=4, blocks=5, accounted=5)


def test_the_skill_link_warning_does_not_disqualify_a_bookkeeping_candidate() -> None:
    gap = _finding("accounting", "coverage_unevidenced")
    skill = _finding("mentions", "skill_outside_demand", "warning")
    assert bookkeeping_gaps(_cand(gap, skill)) == (gap,)
    # it is tolerated, never a gap: on its own there is nothing to settle
    assert bookkeeping_gaps(_cand(skill)) is None
    # every other finding outside `accounting` still disqualifies
    assert bookkeeping_gaps(_cand(gap, _finding("mentions", "mention_ungrounded"))) is None
    assert bookkeeping_gaps(_cand(gap, _finding("mentions", "other_warning", "warning"))) is None


def _gapped_visa() -> dict[str, Any]:
    """VISA with one empty coverage claim (the policy line accounted to the
    duty statement) and Kafka linked only to the policy statement, which is
    the skill-link warning."""
    emit = copy.deepcopy(make_visa_emit())
    for row in emit["block_accounting"]:
        if row["block_id"] == "b000004":
            row["ref_ids"] = ["s_duty"]
    for mention in emit["mentions"]:
        if mention["id"] == "m_kafka":
            mention["statement_ids"] = ["s_visa"]
    return emit


def test_a_bookkeeping_only_v3_ladder_serves_with_accounting_gaps(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    dh = sha256_hex(VISA_MD.encode("utf-8"))
    _seed_doc(pg, dh=dh, markdown=VISA_MD, uid="gh:x:visa")
    engine = AuditingEngine([result(_gapped_visa())] * 3, clean_audit)
    summary = run(_settings(JOB_HUNTER_L2_BUNDLE="v3"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    archived = attempts_in(store)
    assert [a.outcome for a in archived] == ["attribution_failed"] * 3
    assert summary.validated == 1 and summary.quarantined == 0
    row = pg.execute(
        "SELECT status, validator_version, profile FROM extractions WHERE document_hash=%s",
        (dh,)).fetchone()
    assert row is not None
    assert (row["status"], row["validator_version"]) == ("validated", "24")
    assert row["profile"]["quality"]["completeness"] == "accounting_gaps"
