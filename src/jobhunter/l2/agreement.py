"""Cross-sample agreement, computed by code, never an LLM (harness spec §4.5).

Claims are aligned across samples by span-overlap Jaccard >= 0.5 (greedy,
one-to-one, best overlap first). The consensus record is the medoid sample
chosen whole (max mean pairwise F1, lowest slot on ties) — samples are never
merged, because a merged record is one no engine produced and no archive
object backs.

Validator/20 (parsing contract v3 §3) keeps exactly two checks, `GATES`:

- negation — aligned claims must agree on whether the text is NEGATED. It is
  the attribution gate's documented blind spot, "no sponsorship" read as
  "sponsorship available" is the one extraction error that actively harms the
  reader, and the split is rare and cheap.
- numeric conflict — two aligned claims that both PARSED a number from the
  same span must read the same numbers in the same dimension. 144 months
  against 12 is a misread; the same number under two scope tags is not.

Both were narrowed by the 2026-09-28 amendment (approved by Sean that day),
because the 2026-09-28 review-queue analysis found them firing mostly on
labels too. Negation had been reading v1's `negated` bit, which collapses
`ambiguous` into "not plainly positive": 59% of its splits were a hedge ("may
require travel") against an assertion, most of the positive-vs-negative rest
was one fact framed from opposite ends in hiring-policy boilerplate, and about
7% were the real flips. Numeric compared the whole derivation, so "USD"
against no currency, or "at least 25%" against "25%", was a conflict: 347 of
354 conflicting pairs carried identical numbers. Now negation reads statement
polarity, counts only `negative` as negated, and gates only on the statement
kinds a reader acts on (`NEGATION_KINDS`); the numeric gate compares the set
of numbers and their dimension (`_readings`, `_misread`). Every split either
gate stopped parking on is still counted: `metrics.splits.polarity` and
`metrics.splits.numeric_tags` (`SPLITS`).

Everything else is still computed and, for a v2 cohort, fails nothing. Mean
pairwise claim-set F1 and validator/19's five semantic dimensions — statement
kind, polarity target, scoped values, alternatives, entity links — are
measured on every aligned pair and reported under `report["metrics"]`. The
importance ratio is measured on the aligned pairs where either side says
`required`, and it stays where it always was, at the top-level
`report["required_importance_agreement"]`, not in `metrics`: the field itself
is gone from schema 3, so the ratio is now only ever about the archived
schema-2 corpus.

The evidence for demoting all of that is the 2026-09-22 review-queue analysis:
294 of 300 parked documents split on label variance over identical text — 172
pairs of `compensation_statement` against `employer_context`, 1,399 of 1,536
value splits being one number under two scope tags, `gps` against `global
positioning systems (gps)`. Calibrating those dimensions was tried and refuted
(10 of 267 recovered); what the gate was measuring was how two runs LABEL text
they both read the same way. Demoting them is not a loosening of correctness,
it is deleting a check that was never measuring it.

That demotion is a policy of the v2 CONTRACT, and this module is the one
settlement implementation both bundles share — sharing it is not the same as
merging their policies. A v1 cohort keeps scoring exactly what it scored
before (`LEGACY_GATES`), because `demand-profile/v5` settles under validator
"12", a shipped frozen identity parsing contract v3 does not bump. Which set
applies is read off the cohort itself: see `_gates`.

Validator/21 (parsing contract v4 §3) judges schema-4 cohorts only (`_contract_4`)
with the same two gates. Negation also reads the `sponsorship` and
`citizenship` presence polarities, compared per sample pair without alignment
because presence is one entry per family; mention `type`, track membership,
track `selection` and the derived `authorization` value are metrics
(`V21_SPLITS`). Schema 2 and 3 cohorts report and gate exactly as before.

The thresholds here are part of VALIDATOR_VERSION: wiring this gate into the
runner, or changing any constant, bumps it (a $0 archive replay).

A failing gate also produces a DISPUTE SET (validator/18): the medoid
statements no sibling aligns, plus the source blocks a sibling cites and the
medoid never does. It is what settlement scopes audit findings against, and it
is computed here because this is where the alignment lives.

Pure: no I/O, no LLM, no store imports.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from jobhunter.l2.v2.assemble import derive_authorization
from jobhunter.l2.v2.facts import SCHEMA_4_VALIDATOR_VERSION, validator_version_for

JACCARD_MIN = 0.5
F1_MIN = 0.80
IMPORTANCE_MIN = 0.90

#: The only two checks that FAIL a V2 document (validator/20, parsing contract
#: v3 §3). Everything else this module computes for such a cohort is reported —
#: under `report["metrics"]`, or for the importance ratio at its own top-level
#: key — and decides nothing. v1 keeps `LEGACY_GATES` below.
GATES: tuple[str, ...] = ("negation", "numeric_conflict")

#: Where `v2/serve._value_signature` puts the derived number inside one stored
#: value signature: `family|scope|date_kind|component|state` and then, when a
#: number was derived at all, a marker (`q`/`m`/`d`) and the derivation itself.
#: The numeric check reads from the marker on, which is exactly what makes a
#: scope TAG invisible to it (1,399 of 1,536 value splits in the 2026-09-22
#: sample were one number under two tags) and a misread number visible.
_SIGNATURE_STATE = 4
_SIGNATURE_NUMBER = 5

#: Each derivation's layout after its marker, as `_value_signature` writes it:
#: (field count, the dimension — a field index, or a fixed name — and the
#: indexes of the fields holding its numbers). A quantity is
#: `dimension|comparison|min|max|inclusive_min|inclusive_max|unit`, money
#: `comparison|min|max|currency|period`, a date `date|candidates`. Everything a
#: layout does not list as a number or a dimension is a TAG to the gate.
_LAYOUTS: dict[str, tuple[int, int | str, tuple[int, ...]]] = {
    "q": (7, 0, (2, 3)),
    "m": (5, "money", (1, 2)),
    "d": (2, "date", (0, 1)),
}

#: The statement kinds a negation split parks a document on (2026-09-28
#: amendment): what the candidate needs, what the job forbids or requires of
#: them, and what it pays — the kinds a reader acts on. A negation read one way
#: and not the other in a hiring policy, an employer description or a duty is
#: counted (`metrics.splits.polarity`) and never parks: in the 2026-09-28
#: sample those were one fact framed from opposite ends ("on-site only" /
#: "remote not considered"), not a reading a candidate could be misled by.
NEGATION_KINDS: frozenset[str] = frozenset(
    {"qualification", "employment_constraint", "compensation_statement"}
)

#: The one polarity that negates. `ambiguous` is a hedge ("may require
#: travel"), and a hedge against an assertion is a reading of modality, not of
#: whether the text says no.
_NEGATIVE = "negative"
_POSITIVE = "positive"

#: The v2 semantic dimensions (spec §6, validator/19): dimension name -> the key
#: its disagreement count is reported under. Under validator/20 they are METRICS
#: for a v2 cohort: still computed on every aligned pair, still reported both
#: under their own key and in `report["metrics"]["splits"]`, and never a
#: failure. (They stay in `LEGACY_GATES` because validator/19 listed them there;
#: a v1 claim carries none of these fields, so they cannot fire.) A dimension check
#: asks whether two samples mean the same thing by text they BOTH cited, and the
#: 2026-09-22 analysis answered that question for the corpus: they mean the same
#: thing and label it differently. They are counts, never terms in F1.
DIMENSIONS: dict[str, str] = {
    "kind": "kind_disagreements",
    "polarity_target": "polarity_target_disagreements",
    "scoped_values": "scoped_value_disagreements",
    "alternatives": "alternatives_disagreements",
    "entity_links": "entity_link_disagreements",
}

#: Validator/19's failure set, in the order it reported them — what a v1 cohort
#: is still judged on. `demand-profile/v5` settles under validator "12"
#: (`l2/transforms.VALIDATOR_VERSION`), a shipped corpus partition that parsing
#: contract v3 does not bump and whose archived cohorts `rebuild` replays; if
#: validator/20's demotion reached it, the same identity would mean two
#: settlement policies and a replay would promote documents the live path
#: parked. The five dimensions are in the list and cannot fire on a v1 claim
#: (it carries none of those fields), which is exactly what 19 promised.
LEGACY_GATES: tuple[str, ...] = ("f1", "importance", "negation", *DIMENSIONS)

#: Every count `report["metrics"]["splits"]` carries, zeroes included: the five
#: demoted dimensions, plus what the 2026-09-28 amendment stopped the two gates
#: from parking on — `polarity`, a polarity split that is not a negation on a
#: kind a reader acts on, and `numeric_tags`, derivations that read the same
#: numbers under a different comparator, unit, currency, period or
#: inclusivity. Neither is in `LEGACY_GATES`: validator "12" is frozen, and a
#: count that did not exist when it shipped cannot park a v1 document.
SPLITS: tuple[str, ...] = (*DIMENSIONS, "polarity", "numeric_tags")

#: What picks between them. The stored profile blob declares its own contract —
#: `v2/serve.profile_of` stamps `schema`, `bundles._v1_profile_of` carries only
#: `facts` and `demand_profile` — so the cohort says which policy judges it
#: without this module naming a bundle. That matters because the two callers
#: cannot agree on one any other way: `runner.settle` and `rebuild` both build
#: the hook from whatever bundle owns the tuple, and review P0-1 requires live
#: settlement and replay to derive the identical verdict for a mixed archive.
_CONTRACT_KEY = "schema"


def _gates(samples: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Which settlement policy this cohort is judged under.

    A cohort whose every sample declares a contract (`_CONTRACT_KEY`) is a v2
    one and gets validator/20's two gates. Anything else is judged under the
    older, stricter set — a cohort this module cannot identify is never the one
    that fails less, and the only unidentified producer that exists is v1's
    frozen projection. NO sample is the same answer for the same reason: an
    incomplete cohort whose records would not resolve declares nothing, so it
    cannot be the one whose budget is allowed to run out quietly.

    The answer travels in the report (`gates`) because settlement asks it too:
    parsing contract v3 §3 stops an exhausted sample budget from parking a v2
    document, and `state.derive_state` has no other way to tell which contract
    produced the cohort it is folding.
    """
    return GATES if samples and all(_CONTRACT_KEY in s for s in samples) else LEGACY_GATES


# --- validator/21 (parsing contract v4 §3) ------------------------------------
#
# Schema 4 keeps validator/20's two gates and extends `negation` to the two
# authorization presence polarities. Presence is one entry per family per
# record, so it needs no alignment: two samples that BOTH state `sponsorship`
# (or `citizenship`) and read it `positive` in one and `negative` in the other
# split on negation — "will not sponsor" read as a grant is the error that
# harms the reader most, whatever statement kind carries the sentence. A hedge
# (`ambiguous`) against a definite reading is `metrics.splits.polarity`, exactly
# as a hedged statement is. Everything else schema 4 adds is reported under
# `V21_SPLITS` and parks nothing.

#: The presence families whose polarity the negation gate reads.
AUTHORIZATION_POLARITY_FAMILIES: tuple[str, ...] = ("sponsorship", "citizenship")

#: The metric counts a schema-4 cohort reports beside `SPLITS`, zeroes included:
#: `authorization` — the derived `sponsorship` or `citizenship_required` value
#: differs and the presence polarities did not already split (one sample stated
#: the family, the other found none or left it unresolved); `mention_type` — an
#: aligned mention pair typed differently; `track_membership` — an aligned track
#: whose member blocks differ, or a track the other sample never recorded;
#: `track_selection` — both samples recorded tracks with different `selection`.
V21_SPLITS: tuple[str, ...] = (
    "authorization", "mention_type", "track_membership", "track_selection",
)

_STATED = "stated"


def _contract_4(samples: Sequence[Mapping[str, Any]]) -> bool:
    """True when every sample declares a schema validator/21 judges.

    A cohort mixing shapes cannot come from one engine tuple; it is judged
    under validator/20, which compares less and so cannot park on a field one
    side does not have.
    """
    return bool(samples) and all(
        isinstance(s.get(_CONTRACT_KEY), str)
        and validator_version_for(s[_CONTRACT_KEY]) == SCHEMA_4_VALIDATOR_VERSION
        for s in samples
    )


def _splits(samples: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """The `metrics.splits` keys this cohort reports."""
    return (*SPLITS, *V21_SPLITS) if _contract_4(samples) else SPLITS


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _presence_of(profile: Mapping[str, Any]) -> dict[str, Any]:
    return dict(_mapping(_mapping(profile.get("facts")).get("presence")))


def _authorization_splits(x: Mapping[str, Any], y: Mapping[str, Any]) -> dict[str, int]:
    """One sample pair's authorization disagreement: `negation` (the gate),
    `polarity` (a hedge or a missing polarity against a reading, both stated)
    and `authorization` (the derived value split anywhere else)."""
    px, py = _presence_of(x), _presence_of(y)
    counts = {"negation": 0, "polarity": 0, "authorization": 0}
    split: set[str] = set()
    for family in AUTHORIZATION_POLARITY_FAMILIES:
        ex, ey = _mapping(px.get(family)), _mapping(py.get(family))
        if ex.get("state") != _STATED or ey.get("state") != _STATED:
            continue
        if ex.get("polarity") == ey.get("polarity"):
            continue
        split.add(family)
        flip = {ex.get("polarity"), ey.get("polarity")} == {_POSITIVE, _NEGATIVE}
        counts["negation" if flip else "polarity"] += 1
    dx, dy = derive_authorization(px), derive_authorization(py)
    for family, field in (("sponsorship", "sponsorship"),
                          ("citizenship", "citizenship_required")):
        if family not in split and dx[field] != dy[field]:
            counts["authorization"] += 1
    return counts


def _span_of(refs: Any) -> tuple[int, int] | None:
    """The envelope of bound references, as `serve._claim` spans a claim."""
    spans = [
        (r["span"][0], r["span"][1])
        for r in (refs if isinstance(refs, list) else [refs])
        if isinstance(r, Mapping) and isinstance(r.get("span"), list) and len(r["span"]) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) for v in r["span"])
    ]
    if not spans:
        return None
    lo, hi = min(s[0] for s in spans), max(s[1] for s in spans)
    return (lo, hi) if lo < hi else None


def _blocks_of(refs: Any) -> set[str]:
    return {
        r["block_id"]
        for r in (refs if isinstance(refs, list) else [refs])
        if isinstance(r, Mapping) and isinstance(r.get("block_id"), str)
    }


def _mention_type_splits(x: Mapping[str, Any], y: Mapping[str, Any]) -> int:
    """Aligned mentions (by evidence span, as claims align) typed differently."""
    mx = [m for m in _list(x.get("mentions")) if isinstance(m, Mapping)]
    my = [m for m in _list(y.get("mentions")) if isinstance(m, Mapping)]
    pairs = _align_spans([_span_of(m.get("evidence")) for m in mx],
                         [_span_of(m.get("evidence")) for m in my])
    return sum(int(_differs(mx[i].get("type"), my[j].get("type"))) for i, j in pairs)


def _tracks_of(
    profile: Mapping[str, Any],
) -> tuple[Any, list[tuple[tuple[int, int] | None, frozenset[str]]]] | None:
    """A sample's tracks as (selection, [(span, member blocks)]), or None.

    Members are compared as the BLOCKS their statements and mentions cite:
    statement and mention ids are minted per sample, block ids are shared.
    """
    tracks = _mapping(profile.get("relations")).get("tracks")
    if not isinstance(tracks, Mapping):
        return None
    statements = {s.get("id"): s for s in _list(profile.get("statements"))
                  if isinstance(s, Mapping)}
    mentions = {m.get("id"): m for m in _list(profile.get("mentions"))
                if isinstance(m, Mapping)}
    items: list[tuple[tuple[int, int] | None, frozenset[str]]] = []
    for item in _list(tracks.get("items")):
        if not isinstance(item, Mapping):
            continue
        members: set[str] = set()
        for sid in _list(item.get("statement_ids")):
            members |= _blocks_of(_mapping(statements.get(sid)).get("evidence"))
        for mid in _list(item.get("mention_ids")):
            members |= _blocks_of(_mapping(mentions.get(mid)).get("evidence"))
        items.append((_span_of(item.get("evidence")), frozenset(members)))
    return tracks.get("selection"), items


def _track_splits(x: Mapping[str, Any], y: Mapping[str, Any]) -> dict[str, int]:
    tx, ty = _tracks_of(x), _tracks_of(y)
    if tx is None and ty is None:
        return {"track_membership": 0, "track_selection": 0}
    ix = tx[1] if tx is not None else []
    iy = ty[1] if ty is not None else []
    pairs = _align_spans([span for span, _ in ix], [span for span, _ in iy])
    membership = sum(int(ix[i][1] != iy[j][1]) for i, j in pairs)
    membership += (len(ix) - len(pairs)) + (len(iy) - len(pairs))
    selection = int(tx is not None and ty is not None and tx[0] != ty[0])
    return {"track_membership": membership, "track_selection": selection}


def _contract_4_splits(x: Mapping[str, Any], y: Mapping[str, Any]) -> dict[str, int]:
    """Everything validator/21 compares on one sample pair beyond validator/20."""
    return {**_authorization_splits(x, y), "mention_type": _mention_type_splits(x, y),
            **_track_splits(x, y)}


def cohort_hook(
    records_of: Callable[[Any], Mapping[str, Any] | None],
    *,
    f1_min: float = F1_MIN,
) -> Callable[[list[Any], int], tuple[bool, str, dict[str, Any], Dispute | None]]:
    """The one agreement gate every fold shares (architecture review P0-1/P0-2:
    live settlement and rebuild replay must derive identically, and an
    incomplete cohort must never certify).

    `records_of` resolves an ok attempt to its record — the archived object on
    the live path, the in-memory re-judged record on replay. One record per
    slot (the slot's first ok wins); fewer than two resolvable records means
    the audit never completed, which is itself a failure (`sample_failed`), as
    is any attempted slot beyond the resolvable ones.

    The fourth return value is the cohort's dispute set (validator/18), and it
    travels here rather than in the report because the report is JSON that
    reaches the store: the dispute is policy input, not a published dimension.
    It is `None` when nothing is in dispute to scope — a cohort that agreed, or
    one too incomplete to compare — and the records it is computed from are the
    ones this hook already loaded, so settlement never reads the archive twice.
    """

    def hook(
        ok_attempts: list[Any], slots_attempted: int
    ) -> tuple[bool, str, dict[str, Any], Dispute | None]:
        by_slot: dict[int, Any] = {}
        for a in ok_attempts:
            by_slot.setdefault(a.sample_slot, a)
        slot_order = sorted(by_slot)
        resolved = [
            (s, rec) for s in slot_order if (rec := records_of(by_slot[s])) is not None
        ]
        if len(resolved) < 2:
            medoid_key = by_slot[slot_order[0]].attempt_key
            arrived = [rec for _, rec in resolved]
            report: dict[str, Any] = {
                "k": slots_attempted,
                # how many of those slots produced a record to compare. Under
                # validator/20 the pair is the whole of what `sample_failed`
                # means — a budget that ran out, reported rather than judged
                # (parsing contract v3 §5) — and `serve._sample_notes` carries
                # it into the stored blob as requested/arrived.
                "arrived": len(resolved),
                "gates": list(_gates(arrived)),
                "mean_f1": None,
                "pair_f1": {},
                "required_importance_agreement": None,
                "negation_disagreements": 0,
                **({"authorization_negations": 0} if _contract_4(arrived) else {}),
                "numeric_conflicts": 0,
                # nothing was compared, so no dimension disagreed — the keys are
                # here because this report is read by the same code as the other
                **dict.fromkeys(DIMENSIONS.values(), 0),
                "thresholds": {"jaccard": JACCARD_MIN, "f1": f1_min,
                               "importance": IMPORTANCE_MIN},
                "metrics": {"aligned_pairs": 0, "f1": None,
                            "splits": dict.fromkeys(_splits(arrived), 0)},
                "failures": ["sample_failed"],
                "medoid": 0,
            }
            return (False, medoid_key, report, None)
        records = [rec for _, rec in resolved]
        result = agree(records, f1_min=f1_min)
        report = dict(result.report)
        report["k"] = slots_attempted
        report["arrived"] = len(resolved)
        if slots_attempted > len(resolved):
            report["failures"] = [*report["failures"], "sample_failed"]
        medoid_slot = resolved[result.medoid][0]
        passed = not report["failures"]
        dispute = None
        if not passed:
            dispute = dispute_set(
                records[result.medoid],
                [rec for i, rec in enumerate(records) if i != result.medoid],
            )
        return (passed, by_slot[medoid_slot].attempt_key, report, dispute)

    return hook


@dataclass(frozen=True, slots=True)
class AgreementResult:
    passed: bool
    medoid: int  # index into the samples, in slot order
    report: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Claim:
    span: tuple[int, int] | None
    importance: str | None
    negated: bool
    # the id of the object this claim belongs to, in ITS OWN sample's namespace
    # (v2's claim index areas are keyed by statement id; a v1 area has no id).
    # Alignment never reads it — it exists so a disagreement can be named.
    owner: str | None = None
    # the v2 semantic dimensions (validator/19). Every one of them is empty on a
    # v1 claim, and every check below is silent when either side is empty, so an
    # older or leaner claim surface is compared exactly as it always was.
    kind: str | None = None
    polarity_target: str | None = None
    values: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    entity_links: tuple[str, ...] = ()
    # `values` with the family/scope tags stripped: the derivations themselves,
    # comparator, unit, currency and period included. The gate compared these
    # until the 2026-09-28 amendment; they now decide `numeric_tags`.
    numbers: tuple[str, ...] = ()
    # the statement polarity this claim was read under (`_polarity`), or None
    # for a claim no statement makes — the negation gate reads this, never the
    # stored `negated` bit, which cannot tell a hedge from a denial
    polarity: str | None = None
    # the gating half of `values` (2026-09-28 amendment): per parsed
    # derivation, its numbers with their dimension (`_readings`), every tag gone
    readings: tuple[tuple[str, ...], ...] = ()


def _strings(value: Any) -> tuple[str, ...]:
    """A stored list read as strings — a blob is only ever guaranteed to match
    the schema of the day it was written."""
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def _derivations(value: str) -> list[str] | None:
    """The derivation half of one stored value signature (from the marker
    on), or None when it carries no parsed derivation at all: a `parsed`
    state is what "both PARSED a number" means (spec §3)."""
    parts = value.split("|")
    if len(parts) <= _SIGNATURE_NUMBER or parts[_SIGNATURE_STATE] != "parsed":
        return None
    return parts[_SIGNATURE_NUMBER:]


def _numbers(values: tuple[str, ...]) -> tuple[str, ...]:
    """The DERIVATIONS a claim's stored value signatures carry, scope tags
    stripped.

    The family/scope/date-kind/component prefix is dropped because disagreeing
    about a scope TAG is not disagreeing about a number. What is left still
    carries the comparator, unit, currency and period, which is why the gate
    stopped comparing it (2026-09-28): it is what `numeric_tags` counts.
    """
    out: set[str] = set()
    for value in values:
        if (derivation := _derivations(value)) is not None:
            out.add("|".join(derivation))
    return tuple(sorted(out))


def _canonical(number: str) -> str:
    """A derived number as one spelling: "240000.00", "240000.0" and "240000"
    are one amount. `facts.py` keeps the document's own decimals for money,
    and `serve._number` already folds 144.0 into 144 for quantities; anything
    that is not a number is compared as written."""
    try:
        return format(Decimal(number).normalize(), "f")
    except InvalidOperation:
        return number


def _readings(values: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """What a claim's parsed derivations READ: per derivation, each number
    with its dimension.

    The numeric gate's input under the 2026-09-28 amendment. A quantity reads
    as its own dimension (`duration`, `count`, `percentage`, `frequency`),
    money as `money`, a date as `date`; the numbers are the bounds (and a
    date's locale-ambiguous candidates), as a set. Comparator, unit, currency,
    period and inclusivity are tags, so "at least 25%" and "25%" read the same,
    as do "$240,000–$315,000 USD/year" and the same range with no period. A
    different number, or the same number in another dimension, reads
    differently.

    One reading per stored value signature — one fact entry's derivation — and
    not one flat set per claim, because `_misread` must tell a fact one sample
    never derived (omission, silent) from a bound one derivation dropped ("5-8
    years" read as "5+ years": a different set of numbers, a conflict). Flat,
    both are a strict subset.

    A derivation whose marker or length this module does not know is kept
    whole, so it is compared exactly as the gate always compared it: a layout
    this reader cannot take apart is never the one that fails less.
    """
    out: set[tuple[str, ...]] = set()
    for value in values:
        reading: set[str] = set()
        fields = _derivations(value)
        while fields:
            layout = _LAYOUTS.get(fields[0])
            if layout is None or len(fields) <= layout[0]:
                reading.add("|".join(fields))
                break
            size, dimension, number_at = layout
            body, fields = fields[1:size + 1], fields[size + 1:]
            name = body[dimension] if isinstance(dimension, int) else dimension
            for at in number_at:
                for number in body[at].split(","):
                    if number not in ("", "None"):
                        reading.add(f"{name}|{_canonical(number)}")
        if reading:
            out.add(tuple(sorted(reading)))
    return tuple(sorted(out))


def _polarity(claim: Mapping[str, Any], kind: str | None) -> str | None:
    """The statement polarity a stored claim was read under.

    Read off what a schema-2 claim already stores — the blob does not change
    (`negated` is v1's shape, and readers depend on it). `serve._claim` writes
    `polarity_target` as `polarity:subject` for every non-positive statement
    and null for a positive one, so a claim with a kind (every statement claim
    has one) and no target is positive. A claim with neither is a fact entry no
    statement claims: it asserts no polarity at all, and says None.

    A claim with no `polarity_target` KEY predates the field — every v1 claim
    does — and has only its bit, so the bit is its polarity: it is judged
    exactly as validator/12 always judged it.
    """
    if "polarity_target" not in claim:
        return _NEGATIVE if claim.get("negated") else _POSITIVE
    target = claim["polarity_target"]
    if isinstance(target, str):
        return target.partition(":")[0] or None
    return _POSITIVE if kind is not None else None


def _operators(value: Any) -> tuple[str, ...]:
    """The logical operators this claim sits under, sorted.

    Member IDS are carried in the claim (they name the route in its own sample)
    and deliberately NOT compared: ids are minted per sample and mean nothing
    across them (validator/18's refutation 7b354fd4). Arity is left out too —
    one sample splitting "Kotlin/Scala" into two statements where another keeps
    one is atomization variance, which is what F1 is calibrated to absorb. What
    crosses samples is the operator itself: "A and B" and "A or B" are different
    jobs however either sample chose to atomize them.
    """
    if not isinstance(value, list):
        return ()
    operators = [
        route["operator"]
        for route in value
        if isinstance(route, Mapping) and isinstance(route.get("operator"), str)
    ]
    return tuple(sorted(operators))


def _claims(profile: Mapping[str, Any]) -> list[_Claim]:
    out: list[_Claim] = []
    dp = profile.get("demand_profile")
    areas = dp.get("areas") if isinstance(dp, Mapping) else None
    for area in areas if isinstance(areas, list) else []:
        if not isinstance(area, Mapping):
            continue
        owner = area.get("id")
        for c in area.get("claims") or []:
            if not isinstance(c, Mapping):
                continue
            quote = c.get("quote")
            raw = quote.get("span") if isinstance(quote, Mapping) else None
            span: tuple[int, int] | None = None
            if (
                isinstance(raw, list)
                and len(raw) == 2
                and all(isinstance(v, int) and not isinstance(v, bool) for v in raw)
                and raw[0] < raw[1]
            ):
                span = (raw[0], raw[1])
            # schema 3 has no importance at all; a schema-2 claim still carries
            # one and it is still read, because the ratio is still reported —
            # it simply decides nothing (validator/20)
            imp = c.get("importance")
            raw_kind = c.get("kind")
            kind = raw_kind if isinstance(raw_kind, str) else None
            target = c.get("polarity_target")
            values = _strings(c.get("values"))
            out.append(
                _Claim(
                    span=span,
                    importance=imp if isinstance(imp, str) else None,
                    negated=bool(c.get("negated")),
                    owner=owner if isinstance(owner, str) else None,
                    kind=kind,
                    polarity_target=target if isinstance(target, str) else None,
                    values=values,
                    alternatives=_operators(c.get("alternatives")),
                    entity_links=_strings(c.get("entity_links")),
                    numbers=_numbers(values),
                    polarity=_polarity(c, kind),
                    readings=_readings(values),
                )
            )
    return out


@dataclass(frozen=True, slots=True)
class Dispute:
    """What a disagreeing cohort disagrees ABOUT, as ids settlement can match.

    `statement_ids` are the MEDOID's — statement ids (and the claim index's
    unlinked fact-entry ids) are minted per sample and mean nothing across
    samples, so a dispute is only ever expressed in the namespace of the one
    candidate the audit actually ran on. `block_ids` are the exception that
    proves it: block ids come from annotating the shared document (`blocks/1`),
    so they are the one handle both samples use for the same thing.

    `scoped` is False when some disagreement could not be named at all — a
    claim index with no ids (v1's areas). An unnameable dispute is not an empty
    one: nothing may be cleared against it.
    """

    statement_ids: frozenset[str] = frozenset()
    block_ids: frozenset[str] = frozenset()
    scoped: bool = True


def _cited_blocks(record: Mapping[str, Any]) -> set[str]:
    """Every block id this sample's STATEMENTS cite.

    Statement evidence only, on both sides of rule 2, so the comparison is
    symmetric: a block reached only through a mention, a fact aspect or an
    importance citation is not a statement's claim on the source.
    """
    statements = record.get("statements")
    blocks: set[str] = set()
    for statement in statements if isinstance(statements, list) else []:
        if not isinstance(statement, Mapping):
            continue
        for refs in statement.get("evidence") or []:
            if isinstance(refs, Mapping) and isinstance(refs.get("block_id"), str):
                blocks.add(refs["block_id"])
    return blocks


def dispute_set(
    medoid_record: Mapping[str, Any], sibling_records: Sequence[Mapping[str, Any]]
) -> Dispute:
    """The cohort's disagreement, in the medoid's id namespace (validator/18).

    Rule 1: align each sibling's claims to the medoid's with the same greedy
    span-overlap alignment the gate scores, and take every medoid claim left
    unaligned in ANY pair — an object one sibling matched and another did not
    is still in dispute.
    Rule 2: every block a sibling statement cites that no medoid statement
    cites. This is the omission side of a statement-set F1 split, where the
    medoid's own ids can name nothing because the medoid is what is missing.

    The 2026-09-13 adversarial verification refuted the cheaper method — naive
    statement-id overlap between samples — at 4 false clears in 9 documents.
    Both defects it found are structural: ids repeat across samples without
    meaning the same thing, and the omission side of a disagreement has no
    medoid id at all. Hence alignment, not id matching, and blocks alongside
    statements.

    Pure and threshold-free: the gate owns the thresholds, this owns the names.
    """
    medoid_claims = _claims(medoid_record)
    disputed: set[str] = set()
    scoped = True
    for sibling in sibling_records:
        aligned = {i for i, _ in _align(medoid_claims, _claims(sibling))}
        for i, claim in enumerate(medoid_claims):
            if i in aligned:
                continue
            if claim.owner is None:
                scoped = False
            else:
                disputed.add(claim.owner)
    sibling_blocks: set[str] = set()
    for sibling in sibling_records:
        sibling_blocks |= _cited_blocks(sibling)
    return Dispute(
        frozenset(disputed), frozenset(sibling_blocks - _cited_blocks(medoid_record)), scoped
    )


def _jaccard(a: tuple[int, int], b: tuple[int, int]) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union


def _align(xs: list[_Claim], ys: list[_Claim]) -> list[tuple[int, int]]:
    """Greedy one-to-one alignment, best Jaccard first, threshold JACCARD_MIN."""
    return _align_spans([x.span for x in xs], [y.span for y in ys])


def _align_spans(
    xs: Sequence[tuple[int, int] | None], ys: Sequence[tuple[int, int] | None]
) -> list[tuple[int, int]]:
    """`_align` over bare spans, so schema 4's mentions and tracks align by the
    same rule claims do (validator/21)."""
    scored = [
        (_jaccard(x, y), i, j)
        for i, x in enumerate(xs)
        if x is not None
        for j, y in enumerate(ys)
        if y is not None
    ]
    pairs: list[tuple[int, int]] = []
    used_x: set[int] = set()
    used_y: set[int] = set()
    for score, i, j in sorted(scored, key=lambda t: (-t[0], t[1], t[2])):
        if score < JACCARD_MIN:
            break
        if i in used_x or j in used_y:
            continue
        pairs.append((i, j))
        used_x.add(i)
        used_y.add(j)
    return pairs


def _differs(x: Any, y: Any) -> bool:
    """Both sides said something, and they said different things."""
    return bool(x) and bool(y) and x != y


def _conflicts(x: tuple[str, ...], y: tuple[str, ...]) -> bool:
    """An AGGREGATED dimension disagrees on conflict, never on omission.

    `values` and `entity_links` aggregate everything a claim's statement
    carries, so one sample deriving a fact — or linking a mention — the other
    never emitted leaves a strict subset, which is the omission shape F1 and
    the dispute set already answer for (2026-09-16 review). Two different
    readings of the same content leave neither side containing the other,
    and that is the split these dimensions exist to catch."""
    xs, ys = set(x), set(y)
    return bool(xs) and bool(ys) and not (xs <= ys or ys <= xs)


def _misread(x: tuple[tuple[str, ...], ...], y: tuple[tuple[str, ...], ...]) -> bool:
    """The numeric gate (2026-09-28 amendment): two aligned claims that both
    parsed numbers read different numbers.

    Per derivation first, as `_conflicts` does for a whole value: a claim that
    carries only derivations the other also carries, read the same way, omits
    a fact and disagrees about none (the omission side is F1's and the dispute
    set's). Anything else compares the numbers both claims read as sets, so
    one range derived as its two endpoints is the same reading restructured —
    a comparator difference — while a range read as one of its own bounds
    ("5-8 years" as "5+ years", a pay range as its ceiling) reads a different
    set and conflicts: ac-2's "different numbers on the same span".
    """
    xs, ys = set(x), set(y)
    if not xs or not ys or xs <= ys or ys <= xs:
        return False
    return {n for r in xs for n in r} != {n for r in ys for n in r}


def _polarity_split(x: _Claim, y: _Claim) -> str | None:
    """Where an aligned pair's polarity disagreement is counted, if it has one.

    `negation` — the gate — when exactly one side is `negative` and the other
    read the same text as positive or as a hedge, on a statement kind a reader
    acts on (`NEGATION_KINDS`, either side's kind is enough). A claim that
    names no kind (every v1 claim) cannot be exempted by one, which keeps a v1
    cohort's negation count the `negated`-bit count validator/12 shipped.

    `polarity` — the metric — for every other disagreement: a hedge against an
    assertion on any kind, and a negation in hiring-policy or employer
    boilerplate or a duty. That includes a fact no statement claims (polarity
    None) aligned against a statement that is not plainly positive: the
    `negated` bit always split there, and the pair is aligned, so neither F1
    nor the dispute set reports it — this count is the only place it is still
    measured. It never gates: a claim no statement makes asserts no polarity,
    so it is neither the positive nor the hedge a negation is split against.
    Against a positive statement the bit never split, and nothing is counted.
    """
    if x.polarity == y.polarity:
        return None
    if x.polarity is None or y.polarity is None:
        return "polarity" if (x.polarity or y.polarity) != _POSITIVE else None
    one_negated = (x.polarity == _NEGATIVE) != (y.polarity == _NEGATIVE)
    acted_on = x.kind is None or y.kind is None or bool({x.kind, y.kind} & NEGATION_KINDS)
    return "negation" if one_negated and acted_on else "polarity"


def _dimension_splits(x: _Claim, y: _Claim) -> tuple[tuple[str, int], ...]:
    """What this aligned pair disagrees about, per spec §6 dimension.

    Four of the five are silent when either side carries nothing: a claim with
    no kind, no negation target, no derived value or no linked entity is not
    disagreeing about one — it says nothing about it, and the omission side of a
    split is what F1 and the dispute set already answer for.

    `alternatives` is the exception, because there absence IS an assertion: a
    route in one sample and no relation at all in the other reads as "both of
    these are separately required" (spec §3, "Membership in a route does not
    make its members separately universal requirements"). That is exactly the
    silent change a dropped `relations` block makes.
    """
    return (
        ("kind", int(_differs(x.kind, y.kind))),
        ("polarity_target", int(_differs(x.polarity_target, y.polarity_target))),
        ("scoped_values", int(_conflicts(x.values, y.values))),
        ("alternatives", int(x.alternatives != y.alternatives)),
        ("entity_links", int(_conflicts(x.entity_links, y.entity_links))),
    )


def agree(samples: Sequence[Mapping[str, Any]], *, f1_min: float = F1_MIN) -> AgreementResult:
    """The §4.5 gate over k samples of one document, in slot order.

    Under validator/20 the gate keeps TWO checks (parsing contract v3 §3, as
    narrowed by the 2026-09-28 amendment) and everything else it computes
    becomes a metric:

    - `negation`: aligned claims must agree on whether the text is negated,
      on the statement kinds a reader acts on (`_polarity_split`). "No
      sponsorship" read as "sponsorship available" is the one extraction error
      that actively harms the reader, and it is rare and cheap to catch. A
      hedge against an assertion, and a negation split in boilerplate, are
      `metrics.splits.polarity`.
    - `numeric_conflict`: two aligned claims that BOTH parsed a number from the
      same span must read the same numbers in the same dimension (`_misread`).
      144 months against 12 is a misread; the same number under two scope tags
      is not, and neither is the same number under a different comparator,
      unit, currency or period — `metrics.splits.numeric_tags`.

    `f1_min` is still the bundle's calibration (0.80 for v1's coarse area/claim
    sets, 0.70 for v2's finer statements) and still reported, but for a v2
    cohort it no longer fails anything: the 2026-09-22 review-queue analysis
    found 294 of 300 parked documents split on label variance over identical
    text, so a gate built on those dimensions was a label-variance filter, not
    a correctness check. `metrics` is where they live now, and
    `serve.profile_of` carries them into the stored blob so the reading agent
    sees what the samples disagreed on.

    A V1 COHORT IS UNCHANGED. Validator "12" is frozen and this function is the
    only settlement implementation either bundle has, so the failure list is
    built from `_gates(samples)` rather than from one hardcoded set: a profile
    that declares no contract keeps validator/19's `LEGACY_GATES`, F1 and the
    importance ratio included. Every dimension is measured either way — the
    gate set decides which measurements can park a document, never which ones
    are taken.
    """
    if len(samples) < 2:
        raise ValueError("agreement needs at least two samples")
    claim_sets = [_claims(s) for s in samples]

    f1s: list[float] = []
    pair_f1: dict[tuple[int, int], float] = {}
    negation_splits = 0
    polarity_splits = 0
    numeric_conflicts = 0
    numeric_tags = 0
    aligned_pairs = 0
    required_pairs = 0
    required_agree = 0
    dimensions = dict.fromkeys(DIMENSIONS, 0)
    contract_4 = _contract_4(samples)
    authorization_negations = 0
    v21 = dict.fromkeys(V21_SPLITS, 0)
    for a in range(len(samples)):
        for b in range(a + 1, len(samples)):
            if contract_4:
                pair = _contract_4_splits(samples[a], samples[b])
                authorization_negations += pair["negation"]
                polarity_splits += pair["polarity"]
                for key in V21_SPLITS:
                    v21[key] += pair[key]
            xs, ys = claim_sets[a], claim_sets[b]
            pairs = _align(xs, ys)
            denom = len(xs) + len(ys)
            f1 = (2 * len(pairs) / denom) if denom else 1.0
            f1s.append(f1)
            pair_f1[(a, b)] = f1
            aligned_pairs += len(pairs)
            for i, j in pairs:
                x, y = xs[i], ys[j]
                polarity = _polarity_split(x, y)
                negation_splits += int(polarity == "negation")
                polarity_splits += int(polarity == "polarity")
                if _misread(x.readings, y.readings):
                    numeric_conflicts += 1
                elif _conflicts(x.numbers, y.numbers):
                    # what the gate counted until 2026-09-28: the same numbers
                    # under another comparator, unit, currency or period
                    numeric_tags += 1
                if "required" in (x.importance, y.importance):
                    required_pairs += 1
                    if x.importance == y.importance:
                        required_agree += 1
                for dimension, split in _dimension_splits(x, y):
                    dimensions[dimension] += split

    # validator/21: an authorization polarity flip is a negation split like any
    # other, so it is in the count the gate reads; it is ALSO named on its own
    negation_splits += authorization_negations
    mean_f1 = sum(f1s) / len(f1s)
    imp_agreement = (required_agree / required_pairs) if required_pairs else 1.0

    # Every check is measured for every cohort; the gate set only says which of
    # the measurements is allowed to park the document (`_gates`).
    measured: dict[str, int] = {
        "f1": int(mean_f1 < f1_min),
        "importance": int(imp_agreement < IMPORTANCE_MIN),
        "negation": negation_splits,
        "numeric_conflict": numeric_conflicts,
        **dimensions,
    }
    failures: list[str] = [gate for gate in _gates(samples) if measured[gate]]

    # Medoid: max mean F1 against the other samples; lowest slot on ties. F1
    # stopped gating and did not stop meaning anything — "closest to the others"
    # is still what picks the candidate the audit runs on.
    def mean_against_others(idx: int) -> float:
        vals = [f1 for (a, b), f1 in pair_f1.items() if idx in (a, b)]
        return sum(vals) / len(vals)

    medoid = max(range(len(samples)), key=lambda i: (mean_against_others(i), -i))

    report: dict[str, Any] = {
        "k": len(samples),
        "arrived": len(samples),  # `cohort_hook` restates both against the slots
        # the policy this cohort was judged under, so settlement can ask what
        # its contract does with a failure it did not cause (`_gates`)
        "gates": list(_gates(samples)),
        "mean_f1": mean_f1,
        "pair_f1": {f"{a}-{b}": f1 for (a, b), f1 in sorted(pair_f1.items())},
        "required_importance_agreement": imp_agreement,
        "negation_disagreements": negation_splits,
        **({"authorization_negations": authorization_negations} if contract_4 else {}),
        "numeric_conflicts": numeric_conflicts,
        **{DIMENSIONS[d]: n for d, n in dimensions.items()},
        "thresholds": {"jaccard": JACCARD_MIN, "f1": f1_min, "importance": IMPORTANCE_MIN},
        # the demoted dimensions, gathered where a reader can find them without
        # knowing which report keys used to be gates: `aligned_pairs` is the
        # denominator ("2 of 30 aligned pairs split on kind"), `f1` the whole-
        # cohort ratio, `splits` a count per dimension including the zeroes
        "metrics": {
            "aligned_pairs": aligned_pairs,
            "f1": mean_f1,
            "splits": {**dimensions, "polarity": polarity_splits,
                       "numeric_tags": numeric_tags, **(v21 if contract_4 else {})},
        },
        "failures": failures,
        "medoid": medoid,
    }
    return AgreementResult(passed=not failures, medoid=medoid, report=report)
