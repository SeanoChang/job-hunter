"""Eligibility policy for a v2 record (spec §6): seven code-owned quality
dimensions plus the `search_eligible` boolean derived from them. Never a
model judgment — the audit phases that could move semantics/completeness off
`not_checked` do not exist in increment 1, so every offline record is
ineligible by construction; increment 2 wires the auditor that can flip it.
"""

from __future__ import annotations

from typing import Any

_SAMPLING_OK = ("not_requested", "complete")


def assess(
    *,
    source: str,
    evidence: str,
    semantics: str = "not_checked",
    completeness: str = "not_checked",
    sampling: str = "not_requested",
    human_review: str = "none",
    blocking_findings: int = 0,
    blocking_unresolved: int = 0,
) -> dict[str, Any]:
    """The record's `quality` object.

    `search_eligible` iff source is usable, evidence passed, both audit
    phases reached `no_findings`, sampling isn't incomplete/disagreeing,
    human review hasn't rejected the candidate, and no blocking findings or
    unresolved items remain. Human rejection is final and cannot be
    outvoted by any other dimension.
    """
    search_eligible = (
        source == "usable"
        and evidence == "pass"
        and semantics == "no_findings"
        and completeness == "no_findings"
        and sampling in _SAMPLING_OK
        and human_review != "rejected"
        and blocking_findings == 0
        and blocking_unresolved == 0
    )
    return {
        "source": source,
        "evidence": evidence,
        "semantics": semantics,
        "completeness": completeness,
        "sampling": sampling,
        "human_review": human_review,
        "search_eligible": search_eligible,
    }
