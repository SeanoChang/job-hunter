"""Pure derivation of an extraction's status from its archived attempts and
review events. Replay (`extract rebuild`), the catch-up scan, the review verbs
and the live runner all go through this one fold, so incremental state and
rebuilt state cannot diverge.

The fold is chronological over the MERGED event streams: a `retry` review
clears only what preceded it, and a later successful attempt re-validates.
Callers must pass attempts/reviews already filtered to one config
(prompt/schema/validator versions) — events from another config are a
different extraction identity and must never contaminate the fold.

Two hooks keep it pure while still deciding on archived evidence: the
agreement gate (`agreement.cohort_hook`) and, under a bundle with an audit
phase, the `semantic-audit/v1` probe. Both are injected, both are applied in
event order, and a fold given neither is validator/15 exactly — which is what
the v1 corpus keeps getting.

Validator/20 narrows what any of that can do. Parsing contract v3 §3 reserves
`needs_review` for the two gate failures a v2 cohort can report and for human
parking, so an exhausted sample budget — the largest review class the v19 queue
had — stops being a verdict and becomes something the record says about itself
(`_budget_parks`, `quality.sample_notes`). A v1 cohort keeps validator/12's
policy: which one applies is read off the cohort's own report, never off the
caller.

The gate also hands over the cohort's DISPUTE SET, and validator/18's policy
is what this module does with it: a disagreement whose audit found nothing
blocking on what the samples split over settles instead of parking, while
those same findings go on gating eligibility. `GATE_DIMENSION_CODES` and
`audit_touches_dispute` are that policy's whole vocabulary.

Validator/20 also changes what an EXHAUSTED LADDER means (T-Q3S9). A third
hook, `recovery_hook`, hands the fold a content failure's candidate re-judged
(`RecoveredCandidate`), and a ladder that ran out holding a candidate that
extracted the document and whose only failing findings are block bookkeeping
settles `validated` on it — flagged `completeness: accounting_gaps`, never
`search_eligible` — instead of quarantining. `BOOKKEEPING_CODES`,
`RecoveredCandidate` and `bookkeeping_gaps` are that rule's whole vocabulary;
a fold given no recovery hook quarantines exactly as before.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from jobhunter.l2.agreement import GATES, Dispute
from jobhunter.l2.attempts import Attempt


def globs_to_regex(globs: Sequence[str]) -> str:
    """Limited glob syntax (* and ? only) -> one anchored regex.

    The SAME translation gates Python-side acceptance (model_matches) and the
    queue's SQL `~`, so the two checks can never disagree about a model id.
    """
    parts = [re.escape(g).replace(r"\*", ".*").replace(r"\?", ".") for g in globs]
    return "^(" + "|".join(parts or ["$^"]) + ")$"


def model_matches(observed: str | None, globs: Sequence[str]) -> bool:
    if not observed:
        return False
    return re.match(globs_to_regex(globs), observed) is not None


@dataclass(frozen=True)
class Review:
    verb: str  # accept | reject | retry | flag | refute
    at: str
    actor: str = "human"
    key: str = ""  # archive review_key: deterministic same-timestamp tiebreak


@dataclass(frozen=True)
class DerivedState:
    status: str | None  # validated | needs_review | quarantined | rejected | None = pending
    chosen_attempt: str | None
    # k-sampling (spec §4.5): distinct sample slots attempted under this
    # config, and the agreement report when the gate ran (k >= 2 ok slots).
    k: int = 1
    agreement: dict[str, Any] | None = None
    # The v2 quality dimensions this fold owns (spec §6, validator/16). They
    # describe the CHOSEN candidate and are the audit half of `quality.assess`;
    # a fold with no `audit_hook` leaves them at the unchecked defaults, which
    # is why nothing a v1 run produces can be eligible.
    sampling: str = "not_requested"  # not_requested|complete|adjudicated|incomplete|disagreement
    semantics: str = "not_checked"  # no_findings | findings | not_checked | error
    # the audit's four values, or ACCOUNTING_GAPS when the chosen candidate is
    # a bookkeeping-only settlement (validator/20): the verifier's gap, which
    # no audit verdict replaces
    completeness: str = "not_checked"
    blocking: int = 0  # blocking findings + blocking unresolved questions
    # The human disposition the review events leave on record (spec §6). It is
    # derived here, with the status, so settlement cannot publish as unreviewed
    # a candidate a human rejected; `status` itself is what keeps a *parked*
    # record out of the aggregates (see `quality.assess`'s `lifecycle`).
    human_review: str = "none"  # none | accepted | rejected
    # The failing bookkeeping findings of a candidate the fold settled on after
    # its ladder ran out (validator/20, `bookkeeping_gaps`), verbatim as the
    # verifier reported them; empty for every other settlement. Non-empty is
    # exactly "the chosen attempt is a content failure, not an `ok`", which is
    # what tells settlement to publish the RECOVERED candidate and the stored
    # quality block to say which blocks the record never accounted for.
    accounting_gaps: tuple[dict[str, Any], ...] = ()


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# The agreement gate settle() injects: (in-glob ok attempts in slot order,
# count of distinct slots attempted) -> (passed, medoid attempt key, report,
# dispute set). Pure derive_state stays LLM- and I/O-free; the hook loads the
# archived records it needs, and the fold applies its verdict IN EVENT ORDER so
# a later human accept lands on needs_review exactly as replay re-derives it.
# The dispute set (validator/18) comes from the same loaded records, so scoping
# a disagreement costs no second read; `None` means the cohort reported none,
# and the fold then asks validator/16's whole-record question instead.
AgreementHook = Callable[
    [list[Attempt], int], tuple[bool, str, dict[str, Any], Dispute | None]
]


class AuditView(Protocol):
    """What settlement needs from one archived `semantic-audit/v1` artifact.

    Read-only and structural: `v2.audit.AuditOutcome` satisfies it as-is, and
    so does whatever minimal object the loader builds for an `audit_error`
    artifact (dimensions `error`). Keeping it a protocol is what stops this
    module — the shared fold — from importing a version-specific contract.
    """

    @property
    def semantics(self) -> str: ...  # no_findings | findings | error

    @property
    def completeness(self) -> str: ...

    @property
    def blocking(self) -> int: ...


@runtime_checkable
class AuditDetail(Protocol):
    """The half of an audit artifact validator/18 needs to SCOPE it.

    Separate from `AuditView` and tested with `isinstance`, because the two
    readers differ: `v2.audit.AuditOutcome` carries both lists, while a reader
    that only ever needed the dimensions and a count does not. A view without
    them is not a view with none — it is a view that cannot show them, and the
    fold must fall back to the whole-record rule rather than read absence as
    "no findings" and clear a disagreement on missing evidence.
    """

    @property
    def findings(self) -> Sequence[Mapping[str, Any]]: ...

    @property
    def unresolved(self) -> Sequence[Mapping[str, Any]]: ...


#: Rule 3 of the validator/18 policy: which finding codes restate which failed
#: gate. A cohort that split on polarity and an auditor reporting a misread
#: polarity are the same disagreement, whatever object each of them points at —
#: the dispute set cannot name a disagreement about the MEANING of text both
#: samples cited, so the gate's own dimension has to.
#:
#: Validator/20 leaves two entries, because a v2 cohort can only report two
#: failures (`agreement.GATES`). The dimensions it demoted — f1, kind, polarity
#: target, scoped values, alternatives, entity links — never reach `failures`,
#: so a mapping for them would be a rule about a gate that cannot fire; they are
#: still computed and still published, in `report["metrics"]`, which is where a
#: reader that wants them goes. An audit finding that restates one of them still
#: counts as a blocking finding and still takes `search_eligible` away; what it
#: no longer does is decide publication for a cohort that never failed.
#:
#: `agreement.LEGACY_GATES` can still report the demoted names, and this map is
#: deliberately silent about them: only a v1 cohort is judged under that set,
#: and v1 has no audit phase (`Bundle.audit_version is None`), so its failures
#: never reach rule 3 at all — `_audit_scoped_clear` returns False the moment
#: the probe is None. An entry here would be a rule for an adjudication that
#: cannot happen.
GATE_DIMENSION_CODES: dict[str, tuple[str, ...]] = {
    "negation": ("polarity_subject",),
    "numeric_conflict": ("numeric_scope_unit",),
}

#: what a dimension may say once its audit completed; `error` is never a pass
_AUDITED = ("no_findings", "findings")

#: The validator/20 bookkeeping rule (T-Q3S9): the `accounting` finding codes
#: that describe INCOMPLETE BLOCK BOOKKEEPING and nothing else. The 2026-09-28
#: review/quarantine analysis found 271 of the 706 quarantined documents under
#: the active tuple exhausted their ladder failing only these — statements,
#: mentions, facts and references all bound and verified — and under parsing
#: contract v3 ("every verified extraction serves") such a candidate is a
#: faithful extraction with a completeness gap, not an unfaithful one.
#:
#: A whitelist, like `quality._REVIEW_OK`: a code this rule does not name never
#: passes by default. That keeps out the three `accounting` codes that are not
#: bookkeeping gaps — `unknown_block` cites a block the source does not have (a
#: reference defect), and `refs_missing`/`exclusion_reason_missing` are shapes
#: no settling event of the 706 showed — and every non-accounting check.
#: The two `*_requirement_language` codes are warnings the verifier reports
#: beside the gaps; they are listed so their presence does not disqualify a
#: candidate, and they are never themselves a gap (`bookkeeping_gaps`).
BOOKKEEPING_CODES: frozenset[str] = frozenset({
    "coverage_unevidenced", "block_unaccounted",
    "context_requirement_language", "exclusion_requirement_language",
})

#: Warnings outside `accounting` that do not disqualify a bookkeeping candidate
#: (validator/23): ones a passing candidate already carries without
#: consequence. `skill_outside_demand` (schema 4) says a skill mention is linked
#: only to non-demand statements, an advisory about the profile and not about
#: whether the extraction is faithful. Tolerated, never itself a gap. Schema 2
#: and 3 never report it, so bundle v2's rule is unchanged.
TOLERATED_WARNINGS: frozenset[tuple[str, str]] = frozenset({
    ("mentions", "skill_outside_demand"),
})

#: The completeness value a bookkeeping settlement carries. `quality.py` owns
#: the vocabulary (`quality.ACCOUNTING_GAPS`); restated here because this is
#: the shared fold and imports no v2 module, and pinned to it by a test.
ACCOUNTING_GAPS = "accounting_gaps"


@dataclass(frozen=True)
class RecoveredCandidate:
    """What the bookkeeping rule reads off one recovered candidate.

    `findings` is every finding the fold's own verifier reports on it, plus the
    verdicts only the ladder could reach and the archive kept (a retry's
    unexplained deletion, as the bare error it was archived as). The three
    counts are the candidate's SIZE against its source, which no finding says:
    `extracted` is its statements plus its fact entries, `blocks` the source's
    annotated blocks, `accounted` how many of those its accounting rows cover
    (the verifier's own tally — the exact complement of its
    `block_unaccounted` findings).
    """

    findings: tuple[Mapping[str, Any], ...]
    extracted: int
    blocks: int
    accounted: int


def bookkeeping_gaps(candidate: RecoveredCandidate) -> tuple[dict[str, Any], ...] | None:
    """The candidate's gaps when block bookkeeping is ALL that is wrong with it.

    Two tests, and a candidate that fails either is `None`, and the ladder
    quarantines.

    It has to be an EXTRACTION OF THE DOCUMENT, with a gap in it — the premise
    the rule rests on (every statement, mention, fact and reference it holds
    bound and verified) is only worth something when there is content to hold.
    So it extracted at least one statement or fact entry, and its accounting
    covers MOST of the source: strictly more than half of the blocks. A
    candidate that extracted nothing fails nothing but `block_unaccounted`, one
    per block, and one that accounted a sliver of the source fails nothing
    else either; both are a failed extraction, not a completeness gap, and the
    ladder's findings test alone would serve them `validated` — a terminal row
    no drain comes back for (2026-09-28 review of T-Q3S9). The floor costs the
    real corpus nothing: every one of the 293 production candidates the rule
    settles holds 19 or more statements and leaves at most 6% of its blocks
    unaccounted. A posting with nothing to extract (an empty or placeholder
    source) settles as an ordinary `ok` once its accounting is complete; one
    whose accounting is not stays quarantined, as it always did.

    And block bookkeeping has to be all that is wrong: every finding — warnings
    included — is an `accounting` finding under one of `BOOKKEEPING_CODES`, and
    at least one fails (a candidate with nothing failing is an `ok`, not this
    rule's business). Anything else is a faithfulness question this rule never
    answers: a reference, mention, binding, schema or control-character
    finding, and any entry that is not a verifier finding at all (a bare
    `{"error": ...}` — an assembly error, or a retry's unexplained deletion).

    Returns the FAILING findings, verbatim: those are the gaps a reader is shown.
    """
    if candidate.extracted < 1 or 2 * candidate.accounted <= candidate.blocks:
        return None
    findings = candidate.findings
    if not findings:
        return None
    for finding in findings:
        if (finding.get("severity") == "warning"
                and (finding.get("check"), finding.get("code")) in TOLERATED_WARNINGS):
            continue
        if finding.get("check") != "accounting" or finding.get("code") not in BOOKKEEPING_CODES:
            return None
    gaps = tuple(dict(f) for f in findings if f.get("severity") != "warning")
    return gaps or None


def _ids_of(node: Mapping[str, Any]) -> set[str]:
    """Every id a finding or question points at: its targets plus the block its
    citation resolved to. The citation counts because an omission's whole
    subject is the source it cites — `targets` may hold nothing else."""
    targets = node.get("targets")
    ids = {t for t in targets if isinstance(t, str)} if isinstance(targets, list) else set()
    evidence = node.get("evidence")
    if isinstance(evidence, Mapping) and isinstance(evidence.get("block_id"), str):
        ids.add(evidence["block_id"])
    return ids


def audit_touches_dispute(
    findings: Sequence[Mapping[str, Any]], dispute: Dispute, failed_gates: Sequence[str]
) -> bool:
    """Does any BLOCKING finding land on what the cohort disagreed about?

    Three ways in, any of which is a touch: the finding's code is the failed
    gate's own dimension (rule 3); one of the ids it points at is disputed; or
    it points at nothing this policy can place, which is not the same as
    pointing somewhere harmless. Warnings never gate — spec §4 calls display
    wording and redundant duplicates warnings precisely so they do not — and an
    unscoped dispute (`scoped=False`) is touched by every blocking finding.
    """
    codes = {code for gate in failed_gates for code in GATE_DIMENSION_CODES.get(gate, ())}
    for f in findings:
        if f.get("severity") == "warning":
            continue
        if not dispute.scoped or f.get("code") in codes:
            return True
        ids = _ids_of(f)
        if not ids or ids & dispute.statement_ids or ids & dispute.block_ids:
            return True
    return False


#: The gate name the agreement hook reports for a sample slot that produced no
#: record. It is not a disagreement — there is nothing it disagrees WITH — and
#: under validator/20 it is not a verdict either (see `_budget_parks`).
_SAMPLE_FAILED = "sample_failed"


def _budget_parks(report: Mapping[str, Any]) -> bool:
    """Does an exhausted sample budget still hold a document back?

    Under validator/20 it does not. Parsing contract v3 §3 reserves
    `needs_review` for the two gate failures and for human parking, and §5
    demotes sampling to monitoring on the evidence that the 373-of-1,000
    "incomplete cohort" review class "was nothing but exhausted sample
    budgets". A document whose one arrived sample assembled and verified has a
    candidate; the samples that never came back are a fact about the drain, not
    about the posting, and the cohort reports them in `quality.sample_notes`
    instead of parking on them.

    Under validator "12" it does, unchanged: `demand-profile/v5` is a shipped
    frozen identity this contract does not bump, and its archived corpus was
    settled — and is replayed — under a policy where an incomplete cohort never
    certifies. Which policy applies is read off the cohort's own report
    (`agreement._gates` stamps `gates`), not off the caller, so live settlement
    and `rebuild` cannot disagree about one mixed archive (review P0-1).

    A report that names no policy is judged as the legacy one. The same
    doctrine as `_gates`: a cohort this fold cannot identify is never the one
    that parks less.
    """
    gates = report.get("gates")
    return not (isinstance(gates, list) and tuple(gates) == GATES)


# The audit probe settle injects: an attempt key -> that candidate's archived
# audit, or None when no artifact exists (the phase never ran, or this bundle
# has no audit phase at all). Pure `derive_state` stays I/O-free; the hook
# does the `exists`/`get`, and the fold applies the verdict IN EVENT ORDER so
# a later human review lands on the settled status exactly as replay re-derives
# it. No hook means validator/15 behaviour, unchanged.
AuditHook = Callable[[str], AuditView | None]

# The candidate recovery settle injects (validator/20): a content-failed
# attempt -> its candidate, re-judged under the fold's own contract
# (`RecoveredCandidate`: its findings and its size against the source), or
# None when the archived response holds no candidate at all (it does not
# parse, or does not assemble). An `attribution_failed` attempt is archived
# WITHOUT a record — only passing attempts keep one — so the runner and the
# replay recover it the same way, from the raw response, through one shared
# function (`runner._Recovery`); this fold only asks, and only once a ladder
# has run out with nothing settled. No hook is validator/19's exhausted ladder,
# which is what every fold at an older validator, and v1, keeps getting.
RecoveryHook = Callable[[Attempt], RecoveredCandidate | None]


def _audit_clean(audit: AuditView | None) -> bool:
    """A completed audit that found nothing blocking on either dimension.

    Absent (`None`) and `error` are not passes (spec §4), and a warning-only
    audit is not clean either: `findings` on a dimension gates eligibility on
    its own, so only `no_findings`/`no_findings` with zero blocking items can
    adjudicate a disagreement.
    """
    return (
        audit is not None
        and audit.semantics == "no_findings"
        and audit.completeness == "no_findings"
        and audit.blocking == 0
    )


def _audit_scoped_clear(
    audit: AuditView | None, dispute: Dispute | None, failed_gates: Sequence[str]
) -> bool:
    """validator/18: a completed audit whose blocking items are all OFF the
    cohort's dispute — the question `_audit_clean` asks of a whole record,
    asked only where the samples actually split.

    Every blocking item has to be accounted for before any of them can be
    placed: the artifact's own `blocking` count is authoritative, so a count
    the visible lists cannot reproduce (a view that does not carry them, an
    unresolved question, a truncated write) means something blocking is
    invisible here, and an invisible item can never be shown to be off-dispute.
    Unresolved questions are then judged like findings but with no code to map:
    one that names a disputed id — or names nothing — keeps the document.
    """
    if audit is None or dispute is None:
        return False
    if audit.semantics not in _AUDITED or audit.completeness not in _AUDITED:
        return False
    if not isinstance(audit, AuditDetail):
        return False
    findings = [f for f in audit.findings if isinstance(f, Mapping)]
    unresolved = [q for q in audit.unresolved if isinstance(q, Mapping)]
    blocking = [f for f in findings if f.get("severity") != "warning"]
    if audit.blocking != len(blocking) + len(unresolved):
        return False
    if any(_question_touches(q, dispute) for q in unresolved):
        return False
    return not audit_touches_dispute(blocking, dispute, failed_gates)


def _question_touches(question: Mapping[str, Any], dispute: Dispute) -> bool:
    if not dispute.scoped:
        return True
    ids = _ids_of(question)
    return not ids or bool(ids & dispute.statement_ids or ids & dispute.block_ids)


def derive_state(
    attempts: Sequence[Attempt],
    reviews: Sequence[Review],
    accepted_globs: Sequence[str],
    agreement_of: AgreementHook | None = None,
    audit_hook: AuditHook | None = None,
    *,
    recovery_hook: RecoveryHook | None = None,
) -> DerivedState:
    events: list[tuple[datetime, int, str, Attempt | Review]] = []
    for a in attempts:
        # attempts sort before reviews on timestamp ties: reviews respond to attempts
        events.append((_ts(a.started_at), 0, f"{a.attempt_no:08d}", a))
    for r in reviews:
        events.append((_ts(r.at), 1, r.key, r))

    status: str | None = None
    chosen: str | None = None
    agreement: dict[str, Any] | None = None
    sampling = "not_requested"
    # what an adjudicated `sampling` was before the audit carried it, so a
    # reviewer who reopens the record gets the cohort's own verdict back
    adjudicated_from = "disagreement"
    human_review = "none"
    ok_in_glob: list[Attempt] = []
    # this cohort's content failures from an accepted model, in event order:
    # the candidates the validator/20 bookkeeping rule may settle on
    failed_in_glob: list[Attempt] = []
    gaps: tuple[dict[str, Any], ...] = ()
    slots_attempted: set[int] = set()
    ruled = False  # a review verdict has spoken; later samples never override it

    audits: dict[str, AuditView | None] = {}  # one probe per candidate, memoized

    def audit_of(attempt_key: str) -> AuditView | None:
        if audit_hook is None:
            return None
        if attempt_key not in audits:
            audits[attempt_key] = audit_hook(attempt_key)
        return audits[attempt_key]

    recovered: dict[str, tuple[dict[str, Any], ...] | None] = {}  # memoized too

    def gaps_of(attempt: Attempt) -> tuple[dict[str, Any], ...] | None:
        assert recovery_hook is not None
        if attempt.attempt_key not in recovered:
            candidate = recovery_hook(attempt)
            recovered[attempt.attempt_key] = (
                bookkeeping_gaps(candidate) if candidate is not None else None
            )
        return recovered[attempt.attempt_key]

    for _, _, _, event in sorted(events, key=lambda e: (e[0], e[1], e[2])):
        if isinstance(event, Attempt):
            slots_attempted.add(event.sample_slot)
            accepted = model_matches(event.observed_model, accepted_globs)
            in_glob = event.outcome == "ok" and accepted
            if event.outcome == "attribution_failed" and accepted:
                failed_in_glob.append(event)
            if in_glob:
                ok_in_glob.append(event)
                # only PENDING work validates: needs_review/rejected/quarantined
                # can be cleared solely by a human retry (human-only promotion)
                if status is None and not ruled:
                    status, chosen = "validated", event.attempt_key
            # k-sampling (spec §4.5, hardened per the 2026-09-06 review P0-2):
            # once a second slot EXISTS — whatever its outcome — the cohort is
            # open and the gate re-settles the verdict on every attempt, at
            # THIS point in the event order. A failed or missing sample is a
            # disagreement (`sample_failed` from the hook), so an incomplete
            # audit can never certify; a review ruling (`ruled`) is final
            # against later automated samples.
            if (
                agreement_of is not None
                and not ruled
                and ok_in_glob
                and len(slots_attempted) >= 2
                and status in ("validated", "needs_review")
            ):
                passed, medoid_key, report, dispute = agreement_of(
                    ok_in_glob, len(slots_attempted)
                )
                status = "validated" if passed else "needs_review"
                chosen = medoid_key
                agreement = report
                failures = report.get("failures") or []
                # the checks this cohort's own contract lets park a document,
                # which is every failure except a slot that produced nothing
                gates = [f for f in failures if f != _SAMPLE_FAILED]
                incomplete = _SAMPLE_FAILED in failures
                # The sampling dimension (spec §6) is a fact about the cohort,
                # so it is derived with or without an audit hook.
                if passed:
                    sampling = "complete"
                elif incomplete and _budget_parks(report):
                    # Validator/12's policy, frozen. An incomplete cohort has no
                    # comparison to scope against — the samples that would have
                    # disagreed do not exist — so validator/18 adjudicates it
                    # only on the conservative whole-record question: an audit of
                    # the medoid that found nothing blocking ANYWHERE. This arm
                    # takes precedence when a cohort is both incomplete and
                    # disagreeing, which is the stricter of the two tests.
                    sampling = "incomplete"
                    if _audit_clean(audit_of(medoid_key)):
                        status, sampling = "validated", "adjudicated"
                        adjudicated_from = "incomplete"
                elif not gates:
                    # Validator/20 (parsing contract v3 §3, §5): the cohort's
                    # budget ran out and nothing that CAN park a document did.
                    # The medoid is an assembled, verified candidate, so the
                    # document is `validated` and the missing samples are
                    # monitoring information — `report["arrived"]` against
                    # `report["k"]`, published as `quality.sample_notes`. The
                    # dimension still says `incomplete`, because what was not
                    # measured was not measured; it simply stops being a
                    # verdict. Nothing is adjudicated here, so a later flag
                    # reopens an incomplete cohort as an incomplete one.
                    status, sampling = "validated", "incomplete"
                else:
                    # validator/16 adjudication: the cohort is COMPLETE and
                    # disagrees, and a full-source audit of the medoid found
                    # nothing. The audit outranks sampling variance (spec §6:
                    # "Sampling is not a substitute for semantic audit"), so
                    # the document settles instead of parking for review. A
                    # ruled record is never touched (this whole branch runs
                    # only while `not ruled`).
                    # validator/18 adds the scoped question underneath it: an
                    # audit that found blocking work to do, none of it on what
                    # the samples split over, adjudicates the DISAGREEMENT just
                    # as well — and its findings go on gating `search_eligible`
                    # through `blocking`, exactly as for a cohort that agreed.
                    # Under validator/20 the cohort may ALSO be incomplete: a
                    # gate that fired over the samples that did arrive parks on
                    # its own, and it is the disagreement — not the missing
                    # slot — that the audit is asked to adjudicate.
                    sampling = "disagreement"
                    audit = audit_of(medoid_key)
                    if _audit_clean(audit) or _audit_scoped_clear(audit, dispute, gates):
                        status, sampling = "validated", "adjudicated"
                        adjudicated_from = "disagreement"
            if event.outcome == "over_budget":
                if status is None:
                    status = "quarantined"
            elif (event.outcome in ("schema_invalid", "attribution_failed")
                  and status is None and event.attempt_no >= 3 and event.ladder_exhausted):
                # Validator/20 (T-Q3S9): the ladder is out of rungs, so this is
                # the one point at which a bookkeeping-only candidate settles
                # instead of quarantining — never earlier, because inside the
                # ladder accounting findings are retry-worthy errors the model
                # is asked to fix (downgrading them would stop it being asked).
                # Of the cohort's candidates whose only defect is bookkeeping,
                # the one with the FEWEST gaps serves: completeness is the
                # dimension being flagged, so the most complete candidate is
                # the most faithful answer to it. A tie goes to the LATEST —
                # under the v10 retry contract each attempt is an edit of the
                # one before, so the later of two equally complete candidates
                # is the one that took more of the verifier's feedback.
                best: tuple[Attempt, tuple[dict[str, Any], ...]] | None = None
                if recovery_hook is not None:
                    for candidate in failed_in_glob:
                        found = gaps_of(candidate)
                        if found is not None and (best is None or len(found) <= len(best[1])):
                            best = (candidate, found)
                if best is None:
                    status = "quarantined"
                else:
                    status, chosen = "validated", best[0].attempt_key
                    gaps = best[1]
            # transport / throttled / model_rejected / engine_fatal say nothing
            # about the document, so they never settle anything
        else:
            if event.verb == "retry":
                # a retry starts a FRESH cohort (review P0-2): old slot
                # successes and their agreement never carry into the re-run
                status, chosen, agreement = None, None, None
                sampling = "not_requested"
                adjudicated_from = "disagreement"
                human_review = "none"
                ok_in_glob.clear()
                failed_in_glob.clear()
                gaps = ()
                slots_attempted.clear()
                ruled = False
            elif event.verb == "reject" and status is not None:
                status = "rejected"
                human_review = "rejected"
                ruled = True
            elif event.verb in ("flag", "refute") and status == "validated":
                # The record is parked, not rejected: `human_review` stays as
                # it stands (spec §6 knows none/accepted/rejected only) and it
                # is the `needs_review` status that takes it out of the
                # aggregates — `quality.assess` gates on the lifecycle.
                status = "needs_review"
                ruled = True
                if sampling == "adjudicated":
                    # a reviewer (or the refuter) reopened an adjudication:
                    # what remains on record is the cohort verdict it settled —
                    # a disagreement, or an incomplete cohort (validator/18) —
                    # never a claim that the audit still carries the cohort
                    sampling = adjudicated_from
            elif event.verb == "accept" and (
                status == "needs_review"
                # validator/16: the audit probe is TIMELESS — `audit_of` answers
                # the same whenever the artifact was written — so an audit
                # archived after this accept was filed retroactively adjudicates
                # the disagreement the operator was ruling on, and the fold
                # reads `validated` here where the human saw `needs_review`.
                # Honour the ruling anyway: dropping it would leave
                # `human_review` at "none" with nothing `ruled`, and the next
                # automated sample would demote the very record a human
                # validated (spec §6: a human accept is bound to the candidate,
                # and a ruling is final against later samples). An adjudicated
                # `validated` is the only status an artifact can move a review
                # onto, so this is also the only arm that needs it: a cohort
                # that agreed on its own reads an accept exactly as
                # validator/15 does — not at all.
                or (status == "validated" and sampling == "adjudicated")
            ):
                status = "validated"
                human_review = "accepted"
                ruled = True
            # accept from quarantined/pending, flag on pending: ignored

    # chosen_attempt is provenance and survives rejection (the row's identity
    # must not move between the live path and replay); it clears only when the
    # fold ends pending or quarantined-without-an-ok.
    k = max(len(slots_attempted), 1)
    if status in ("validated", "needs_review", "rejected"):
        # The settled candidate's audit, reported whatever the verdict: an
        # unsampled (k=1) document is audited too, so eligibility never
        # requires sampling, and a parked one still shows why. Absent means
        # `not_checked` — the phase never ran, which is never a pass.
        audit = audit_of(chosen) if chosen is not None else None
        completeness = audit.completeness if audit is not None else "not_checked"
        if gaps:
            # the verifier's finding, not an audit's: whatever an audit of
            # this candidate says, its bookkeeping failed, and completeness
            # (hence eligibility) stays failed until the accounting is fixed
            completeness = ACCOUNTING_GAPS
        return DerivedState(
            status,
            chosen,
            k=k,
            agreement=agreement,
            sampling=sampling,
            semantics=audit.semantics if audit is not None else "not_checked",
            completeness=completeness,
            blocking=audit.blocking if audit is not None else 0,
            human_review=human_review,
            accounting_gaps=gaps,
        )
    return DerivedState(
        status, None, k=k, agreement=agreement, sampling=sampling,
        human_review=human_review,
    )
