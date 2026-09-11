"""The extraction engine tuple as one selectable object.

A run is defined by six things that only make sense together: the prompt (its
version, frozen bytes and renderer), the emit schema version, the validator
version, emit->record assembly, the record verifier, and the two projections
that reach storage — the profile blob `extractions.profile` holds and the rows
behind the `profile_mentions` aggregate. v1 hardwired all six into `runner.py`;
the v2 contract changes every one of them while the loop around them (ladder,
breaker, caps, catch-up, k-sampling, settle) stays exactly as it is. So the six
travel together as a `Bundle` the runner is handed, and the loop stops naming
any of them.

A bundle is data, never state: it is passed as a parameter, because the runner
is re-entrant across threads (the parallel drain) and across tests. Its
identity `(prompt_version, schema_version, validator_version)` is what every
archived attempt and every derived row is keyed by, so choosing a bundle IS
choosing a corpus partition. `get_bundle_for_tuple` is the inverse: replay
reads a tuple off an archived attempt and gets back the bundle that judged it,
which is what lets a mixed archive fold correctly.

Registering a bundle is the only way to make it selectable; `config.py` refuses
a name the registry does not carry, so a typo in the environment fails at
startup rather than at the first document.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jobhunter.l2.assemble import AssembleError
from jobhunter.l2.assemble import assemble as _assemble_v1
from jobhunter.l2.prompt import PROMPT_VERSION as _V1_PROMPT_VERSION
from jobhunter.l2.prompt import TEMPLATE as _V1_TEMPLATE
from jobhunter.l2.prompt import prompt_sha as _v1_prompt_sha
from jobhunter.l2.prompt import render as _v1_render
from jobhunter.l2.report import Finding, Report
from jobhunter.l2.transforms import VALIDATOR_VERSION as _V1_VALIDATOR_VERSION
from jobhunter.l2.v2 import serve as _v2_serve
from jobhunter.l2.v2.assemble import AssembleError as _V2AssembleError
from jobhunter.l2.v2.assemble import assemble as _assemble_v2
from jobhunter.l2.v2.emit_guard import engine_emit_schema as _v2_engine_emit_schema
from jobhunter.l2.v2.facts import VALIDATOR_VERSION as _V2_VALIDATOR_VERSION
from jobhunter.l2.v2.prompt import PROMPT_VERSION as _V2_PROMPT_VERSION
from jobhunter.l2.v2.prompt import TEMPLATE as _V2_TEMPLATE
from jobhunter.l2.v2.prompt import prompt_sha as _v2_prompt_sha
from jobhunter.l2.v2.prompt import render as _v2_render
from jobhunter.l2.v2.verify import verify as _verify_v2
from jobhunter.l2.verify import verify as _verify_v1
from jobhunter.store.extraction import split_mention

DEFAULT_BUNDLE = "v1"
#: names the CLI/env will accept; a name here that is not registered yet is a
#: teaching error, not a crash (see `config.Settings.load`).
BUNDLE_NAMES = ("v1", "v2")


@dataclass(frozen=True)
class Bundle:
    """One engine tuple: identity, prompt, and the four pure functions around it.

    The callables are plain instance attributes, so `bundle.render(...)` calls
    the wrapped function itself — no descriptor binding, no `self`.
    """

    name: str  # "v1" | "v2"
    prompt_version: str
    schema_version: str
    validator_version: str
    template: str
    prompt_sha: Callable[[], str]
    render: Callable[[str, list[str]], str]
    assemble: Callable[..., dict[str, Any]]  # raises AssembleError
    verify: Callable[[dict[str, Any], str], Report]
    profile_of: Callable[[dict[str, Any]], dict[str, Any]]
    mention_rows: Callable[[dict[str, Any]], list[tuple[str, str, str]]]
    # (mention, area_kind, importance)
    # engine-facing emit schema, when tighter than the stored contract (the
    # v2 kind-importance union); None means emit_schema(schema_version).
    engine_emit_schema: Callable[[], dict[str, Any]] | None = None
    # how a verify Finding renders into a retry error string; None means the
    # bare v1 form "check:code at path" (frozen v1 attempt bytes depend on it).
    render_finding: Callable[[Finding], str] | None = None
    # the agreement gate's F1 calibration for this bundle's claim granularity
    agreement_f1_min: float = 0.80


def _v1_profile_of(record: dict[str, Any]) -> dict[str, Any]:
    """The stored profile blob for a v1 record (was `runner._profile_of`)."""
    return {"facts": record["facts"], "demand_profile": record["demand_profile"]}


def _v1_mention_rows(record: dict[str, Any]) -> list[tuple[str, str, str]]:
    """`profile_mentions` rows for a v1 record — the area walk lifted verbatim
    out of `store.extraction.upsert_state`.

    Reads only `demand_profile`, so the full record and the stored profile blob
    (a superset of it) derive the same rows. One row per skill per area:
    `split_mention` normalizes each emitted mention and the first spelling wins
    within an area, casefolded.
    """
    rows: list[tuple[str, str, str]] = []
    for area in (record.get("demand_profile") or {}).get("areas") or []:
        seen: dict[str, str] = {}  # casefold -> first spelling, one row per skill
        for raw in area.get("mentions") or []:
            for mention in split_mention(raw):
                seen.setdefault(mention.casefold(), mention)
        rows += [(m, area["kind"], area["importance"]) for m in seen.values()]
    return rows


_V1 = Bundle(
    name="v1",
    prompt_version=_V1_PROMPT_VERSION,
    schema_version="1",
    validator_version=_V1_VALIDATOR_VERSION,
    template=_V1_TEMPLATE,
    prompt_sha=_v1_prompt_sha,
    render=_v1_render,
    assemble=_assemble_v1,
    verify=_verify_v1,
    profile_of=_v1_profile_of,
    mention_rows=_v1_mention_rows,
)

def _v2_render_finding(f: Finding) -> str:
    """Retry guidance the model can act on: the code plus its detail — 
    'importance_unexpected (kind=employer_context, importance=required)' says
    what to change; the bare code said nothing (first live v6 run, 240/240
    attribution failures dominated by exactly this)."""
    detail = ", ".join(f"{k}={v}" for k, v in sorted(f.detail.items()))
    tail = f" ({detail})" if detail else ""
    return f"{f.check}:{f.code} at {f.path}{tail}"


def _v2_assemble(emit: dict[str, Any], markdown: str, **kwargs: Any) -> dict[str, Any]:
    """`l2.v2.assemble` behind the runner's failure vocabulary.

    The keyword shape already matches (`document_hash`, `observed_model`, `at`,
    `normalizer_version`); what does not match is the exception. Each contract
    raises its own `AssembleError`, and the runner catches one class to decide
    `attribution_failed` and to feed `.errors` into the next prompt. Re-raising
    v2's as v1's here keeps that decision in one place instead of teaching the
    loop a second exception per bundle — the whole point of the abstraction.
    """
    try:
        return _assemble_v2(emit, markdown, **kwargs)
    except _V2AssembleError as exc:
        raise AssembleError(exc.errors) from exc


_V2 = Bundle(
    name="v2",
    prompt_version=_V2_PROMPT_VERSION,
    schema_version="2",
    validator_version=_V2_VALIDATOR_VERSION,
    template=_V2_TEMPLATE,
    prompt_sha=_v2_prompt_sha,
    render=_v2_render,
    assemble=_v2_assemble,
    verify=_verify_v2,
    profile_of=_v2_serve.profile_of,
    mention_rows=_v2_serve.mention_rows,
    engine_emit_schema=_v2_engine_emit_schema,
    render_finding=_v2_render_finding,
    # stays at the field default 0.80: the 2026-09-11 analysis showed the
    # borderline-F1 review cases include real importance/polarity conflicts,
    # and recalibration without adjudicated examples only relabels the queue
)

_REGISTRY: dict[str, Bundle] = {_V1.name: _V1, _V2.name: _V2}


def registered() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def get_bundle(name: str) -> Bundle:
    """The bundle registered under `name`, or a KeyError that says what exists."""
    bundle = _REGISTRY.get(name)
    if bundle is None:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(
            f"no extraction bundle {name!r} is registered (have: {known}); "
            "a bundle must be registered in l2/bundles.py before a run can select it"
        )
    return bundle


def get_bundle_for_tuple(prompt_version: str, schema_version: str) -> Bundle:
    """The bundle that owns an archived attempt's `(prompt_version, schema_version)`.

    Replay's inverse of `get_bundle`. A tuple no bundle claims — a historical
    prompt version, or one from a bundle that has since been retired — raises:
    folding it under today's shapes would relabel history, so the caller decides
    what to do about it.
    """
    for bundle in _REGISTRY.values():
        if (bundle.prompt_version, bundle.schema_version) == (prompt_version, schema_version):
            return bundle
    known = ", ".join(f"{b.prompt_version}/{b.schema_version}" for b in _REGISTRY.values())
    raise KeyError(
        f"no extraction bundle claims ({prompt_version!r}, {schema_version!r}); "
        f"registered tuples: {known}"
    )
