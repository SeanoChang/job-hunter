"""Cross-sample agreement, computed by code, never an LLM (harness spec §4.5).

Claims are aligned across samples by span-overlap Jaccard >= 0.5 (greedy,
one-to-one, best overlap first); the gate is mean pairwise claim-set F1 >=
0.80, importance agreement on required claims >= 0.90, and ZERO negation
disagreements — polarity is the attribution gate's documented blind spot, so
any split escalates unconditionally. The consensus record is the medoid
sample chosen whole (max mean pairwise F1, lowest slot on ties) — samples are
never merged, because a merged record is one no engine produced and no
archive object backs.

The thresholds here are part of VALIDATOR_VERSION: wiring this gate into the
runner, or changing any constant, bumps it (a $0 archive replay).

Pure: no I/O, no LLM, no store imports.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

JACCARD_MIN = 0.5
F1_MIN = 0.80
IMPORTANCE_MIN = 0.90


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


def _claims(profile: Mapping[str, Any]) -> list[_Claim]:
    out: list[_Claim] = []
    dp = profile.get("demand_profile")
    areas = dp.get("areas") if isinstance(dp, Mapping) else None
    for area in areas if isinstance(areas, list) else []:
        if not isinstance(area, Mapping):
            continue
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
            imp = c.get("importance")
            out.append(
                _Claim(
                    span=span,
                    importance=imp if isinstance(imp, str) else None,
                    negated=bool(c.get("negated")),
                )
            )
    return out


def _jaccard(a: tuple[int, int], b: tuple[int, int]) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union


def _align(xs: list[_Claim], ys: list[_Claim]) -> list[tuple[int, int]]:
    """Greedy one-to-one alignment, best Jaccard first, threshold JACCARD_MIN."""
    scored = [
        (_jaccard(x.span, y.span), i, j)
        for i, x in enumerate(xs)
        if x.span is not None
        for j, y in enumerate(ys)
        if y.span is not None
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


def agree(samples: Sequence[Mapping[str, Any]]) -> AgreementResult:
    """The §4.5 gate over k samples of one document, in slot order."""
    if len(samples) < 2:
        raise ValueError("agreement needs at least two samples")
    claim_sets = [_claims(s) for s in samples]

    f1s: list[float] = []
    pair_f1: dict[tuple[int, int], float] = {}
    negation_splits = 0
    required_pairs = 0
    required_agree = 0
    for a in range(len(samples)):
        for b in range(a + 1, len(samples)):
            xs, ys = claim_sets[a], claim_sets[b]
            pairs = _align(xs, ys)
            denom = len(xs) + len(ys)
            f1 = (2 * len(pairs) / denom) if denom else 1.0
            f1s.append(f1)
            pair_f1[(a, b)] = f1
            for i, j in pairs:
                if xs[i].negated != ys[j].negated:
                    negation_splits += 1
                if "required" in (xs[i].importance, ys[j].importance):
                    required_pairs += 1
                    if xs[i].importance == ys[j].importance:
                        required_agree += 1

    mean_f1 = sum(f1s) / len(f1s)
    imp_agreement = (required_agree / required_pairs) if required_pairs else 1.0

    failures: list[str] = []
    if mean_f1 < F1_MIN:
        failures.append("f1")
    if imp_agreement < IMPORTANCE_MIN:
        failures.append("importance")
    if negation_splits:
        failures.append("negation")

    # Medoid: max mean F1 against the other samples; lowest slot on ties.
    def mean_against_others(idx: int) -> float:
        vals = [f1 for (a, b), f1 in pair_f1.items() if idx in (a, b)]
        return sum(vals) / len(vals)

    medoid = max(range(len(samples)), key=lambda i: (mean_against_others(i), -i))

    report: dict[str, Any] = {
        "k": len(samples),
        "mean_f1": mean_f1,
        "pair_f1": {f"{a}-{b}": f1 for (a, b), f1 in sorted(pair_f1.items())},
        "required_importance_agreement": imp_agreement,
        "negation_disagreements": negation_splits,
        "thresholds": {"jaccard": JACCARD_MIN, "f1": F1_MIN, "importance": IMPORTANCE_MIN},
        "failures": failures,
        "medoid": medoid,
    }
    return AgreementResult(passed=not failures, medoid=medoid, report=report)
