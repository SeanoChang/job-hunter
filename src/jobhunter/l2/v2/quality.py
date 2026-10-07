"""Eligibility policy for a v2 record (spec §6): seven code-owned quality
dimensions plus the `search_eligible` boolean derived from them. Never a
model judgment — an offline record's semantics/completeness stay
`not_checked`, which no deterministic check can clear; only a completed
`semantic-audit/v1` phase (validator/16) moves them, which is what makes a
record eligible at all.
"""

from __future__ import annotations

from typing import Any

#: sampling states that do not themselves block eligibility. `adjudicated`
#: (validator/16) is a cohort that DISAGREED and whose audited medoid carried
#: no blocking findings: a full-source semantic audit of the chosen candidate
#: outranks sampling variance, and symmetrically, variance alone is not a
#: semantic defect (spec §6: "Sampling is not a substitute for semantic
#: audit"). `disagreement` still blocks — a disagreement with no clean audit
#: stays for review. `incomplete` stopped blocking with parsing contract v3
#: (§3, §5): a cohort short of samples is monitoring information recorded in
#: `sample_notes`, and the candidate it did verify is judged by the audit
#: exactly like an unsampled document's — the sampler running out of budget is
#: not evidence against the record.
_SAMPLING_OK = ("not_requested", "complete", "adjudicated", "incomplete")

#: `completeness` when the VERIFIER found the gap rather than the auditor
#: (validator/20, T-Q3S9). A ladder that ran out with a candidate whose only
#: failing findings are block bookkeeping — coverage claims its named objects
#: never quote, blocks no row accounts for — settles on that candidate instead
#: of quarantining (`state.derive_state`): everything it extracted bound and
#: verified, so it is a faithful extraction with a completeness gap, not an
#: unfaithful one. Its own value rather than `findings`, because `findings` is
#: the auditor's claim to have READ the record against the source and found an
#: omission, and no audit ran here. It fails the `no_findings` test below like
#: every other value, so such a record is never `search_eligible`; the gaps
#: themselves travel beside it as `quality.accounting_gaps`
#: (`serve.quality_of`). Restated in `state.ACCOUNTING_GAPS` — the shared fold
#: imports no v2 module — and pinned to it by a test.
ACCOUNTING_GAPS = "accounting_gaps"

#: every value the completeness dimension can take: the audit phase's four
#: (spec §6) and the verifier's one
COMPLETENESS = ("no_findings", "findings", "not_checked", "error", ACCOUNTING_GAPS)

#: human dispositions that leave a candidate publishable. A whitelist, not a
#: `!= "rejected"` check: a disposition this policy does not recognise must
#: never pass by default (spec §6: "No human rejection can be overridden
#: automatically").
_REVIEW_OK = ("none", "accepted")


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
    lifecycle: str = "validated",
) -> dict[str, Any]:
    """The record's `quality` object.

    `search_eligible` iff the record settled `validated`, source is usable,
    evidence passed, both audit phases reached `no_findings`, sampling isn't
    incomplete/disagreeing, human review hasn't withheld the candidate, and no
    blocking findings or unresolved items remain. Human rejection is final and
    cannot be outvoted by any other dimension.

    `no_findings` means a completed audit found nothing; it is never a claim
    that the record is correct, and `error`/`not_checked` are never passes.

    `lifecycle` is the settled status (`state.DerivedState.status`), not a
    published dimension — it never appears in the returned object, whose keys
    are exactly the spec's seven. It gates publication because the dimensions
    alone cannot express a parked record: a reviewer's `flag` or a refuter's
    demotion leaves a clean audit clean and an agreeing cohort complete, and
    spec §6 still puts that record out of the aggregates until a human clears
    it. Callers with no settled status (assembly, before any settlement) leave
    it at the default; their `not_checked` dimensions keep them ineligible.
    """
    search_eligible = (
        lifecycle == "validated"
        and source == "usable"
        and evidence == "pass"
        and semantics == "no_findings"
        and completeness == "no_findings"
        and sampling in _SAMPLING_OK
        and human_review in _REVIEW_OK
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
