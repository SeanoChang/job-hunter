"""Pure derivation of an extraction's status from its archived attempts and
review events. Replay (`extract rebuild`) and the live runner both go through
this one function, so incremental state and rebuilt state cannot diverge."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatch

from jobhunter.l2.attempts import Attempt


@dataclass(frozen=True)
class Review:
    verb: str  # accept | reject | retry | flag | refute
    at: str


@dataclass(frozen=True)
class DerivedState:
    status: str | None  # validated | needs_review | quarantined | rejected | None = pending
    chosen_attempt: str | None


def derive_state(
    attempts: Sequence[Attempt],
    reviews: Sequence[Review],
    accepted_globs: Sequence[str],
) -> DerivedState:
    status: str | None = None
    chosen: str | None = None
    for a in sorted(attempts, key=lambda x: (x.started_at, x.attempt_no)):
        if a.outcome == "ok":
            observed = a.observed_model
            in_glob = bool(observed) and any(fnmatch(observed or "", g) for g in accepted_globs)
            if in_glob and status != "validated":
                status, chosen = "validated", a.attempt_key
        elif a.outcome == "over_budget":
            if status is None:
                status = "quarantined"
        elif a.outcome in ("schema_invalid", "attribution_failed"):
            if status is None and a.attempt_no >= 3 and a.ladder_exhausted:
                status = "quarantined"
        # transport / throttled / model_rejected never settle anything

    for r in sorted(reviews, key=lambda x: x.at):
        if r.verb == "retry":
            status, chosen = None, None
        elif r.verb == "reject" and status is not None:
            status = "rejected"
        elif r.verb in ("flag", "refute") and status == "validated":
            status = "needs_review"
        elif r.verb == "accept" and status == "needs_review":
            status = "validated"
        # accept from quarantined/pending, flag on pending: ignored

    return DerivedState(status, chosen if status in ("validated", "needs_review") else None)
