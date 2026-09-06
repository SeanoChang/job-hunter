"""Pure derivation of an extraction's status from its archived attempts and
review events. Replay (`extract rebuild`), the catch-up scan, the review verbs
and the live runner all go through this one fold, so incremental state and
rebuilt state cannot diverge.

The fold is chronological over the MERGED event streams: a `retry` review
clears only what preceded it, and a later successful attempt re-validates.
Callers must pass attempts/reviews already filtered to one config
(prompt/schema/validator versions) — events from another config are a
different extraction identity and must never contaminate the fold.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

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


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# The agreement gate settle() injects: (in-glob ok attempts in slot order,
# count of distinct slots attempted) -> (passed, medoid attempt key, report).
# Pure derive_state stays LLM- and I/O-free; the hook loads the archived
# records it needs, and the fold applies its verdict IN EVENT ORDER so a
# later human accept lands on needs_review exactly as replay re-derives it.
AgreementHook = Callable[[list[Attempt], int], tuple[bool, str, dict[str, Any]]]


def derive_state(
    attempts: Sequence[Attempt],
    reviews: Sequence[Review],
    accepted_globs: Sequence[str],
    agreement_of: AgreementHook | None = None,
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
    ok_in_glob: list[Attempt] = []
    slots_attempted: set[int] = set()
    for _, _, _, event in sorted(events, key=lambda e: (e[0], e[1], e[2])):
        if isinstance(event, Attempt):
            slots_attempted.add(event.sample_slot)
            if event.outcome == "ok":
                in_glob = model_matches(event.observed_model, accepted_globs)
                # only PENDING work validates: needs_review/rejected/quarantined
                # can be cleared solely by a human retry (human-only promotion)
                if in_glob:
                    ok_in_glob.append(event)
                if in_glob and status is None:
                    status, chosen = "validated", event.attempt_key
                # k-sampling (spec §4.5): once ok records span more than one
                # slot, the agreement gate re-settles the verdict at THIS point
                # in the event order — a later human accept still lands on the
                # needs_review it produces, live and on replay alike.
                if (
                    in_glob
                    and agreement_of is not None
                    and status in ("validated", "needs_review")
                    and len({a.sample_slot for a in ok_in_glob}) >= 2
                ):
                    passed, medoid_key, report = agreement_of(
                        ok_in_glob, len(slots_attempted)
                    )
                    status = "validated" if passed else "needs_review"
                    chosen = medoid_key
                    agreement = report
            elif event.outcome == "over_budget":
                if status is None:
                    status = "quarantined"
            elif event.outcome in ("schema_invalid", "attribution_failed"):
                if status is None and event.attempt_no >= 3 and event.ladder_exhausted:
                    status = "quarantined"
            # transport / throttled / model_rejected / engine_fatal say nothing
            # about the document, so they never settle anything
        else:
            if event.verb == "retry":
                status, chosen = None, None
            elif event.verb == "reject" and status is not None:
                status = "rejected"
            elif event.verb in ("flag", "refute") and status == "validated":
                status = "needs_review"
            elif event.verb == "accept" and status == "needs_review":
                status = "validated"
            # accept from quarantined/pending, flag on pending: ignored

    # chosen_attempt is provenance and survives rejection (the row's identity
    # must not move between the live path and replay); it clears only when the
    # fold ends pending or quarantined-without-an-ok.
    k = max(len(slots_attempted), 1)
    if status in ("validated", "needs_review", "rejected"):
        return DerivedState(status, chosen, k=k, agreement=agreement)
    return DerivedState(status, None, k=k, agreement=agreement)
