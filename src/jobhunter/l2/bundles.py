"""The extraction engine tuple as one selectable object.

A run is defined by six things that only make sense together: the prompt (its
version, frozen bytes and renderer), the emit schema version, the validator
version, emit->record assembly, the record verifier, and the two projections
that reach storage — the profile blob `extractions.profile` holds and the rows
behind the `profile_mentions` aggregate. v1 hardwired all six into `runner.py`;
the v2 contract changes every one of them while the loop around them (ladder,
breaker, caps, catch-up, k-sampling, settle) stays exactly as it is. So the six
travel together as a `Bundle` the runner is handed, and the loop stops naming
any of them. A bundle may also carry a semantic audit phase (spec §4): its
version, prompt renderer, emit schema and judge, all four or none — v1 has
none, and `audit_version is None` is what tells the runner and settlement that
this tuple has no audit to run or read.

A bundle is data, never state: it is passed as a parameter, because the runner
is re-entrant across threads (the parallel drain) and across tests. Its
identity `(prompt_version, schema_version, validator_version)` is what every
archived attempt and every derived row is keyed by, so choosing a bundle IS
choosing a corpus partition. `get_bundle_for_tuple` is the inverse: replay
reads a tuple off an archived attempt and gets back the bundle that judged it,
which is what lets a mixed archive fold correctly.

Registering a bundle is the only way to make it selectable; `config.py` refuses
a name the registry does not carry, so a typo in the environment fails at
startup rather than at the first document. A tuple a bump retires keeps a
FROZEN registration instead (`_FROZEN`): resolvable by tuple for replay, not
by name for a run, because the documents already extracted under it have to
keep folding under the shapes that judged them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from jobhunter.l2.assemble import AssembleError
from jobhunter.l2.assemble import assemble as _assemble_v1
from jobhunter.l2.prompt import PROMPT_VERSION as _V1_PROMPT_VERSION
from jobhunter.l2.prompt import TEMPLATE as _V1_TEMPLATE
from jobhunter.l2.prompt import prompt_sha as _v1_prompt_sha
from jobhunter.l2.prompt import render as _v1_render
from jobhunter.l2.report import Finding, Report
from jobhunter.l2.transforms import VALIDATOR_VERSION as _V1_VALIDATOR_VERSION
from jobhunter.l2.v2 import prompt_v12 as _v12
from jobhunter.l2.v2 import prompt_v13 as _v13
from jobhunter.l2.v2 import prompt_v14 as _v14
from jobhunter.l2.v2 import prompt_v15 as _v15
from jobhunter.l2.v2 import serve as _v2_serve
from jobhunter.l2.v2.assemble import AssembleError as _V2AssembleError
from jobhunter.l2.v2.assemble import assemble as _assemble_v2
from jobhunter.l2.v2.audit import AUDIT_VERSION as _V2_AUDIT_VERSION
from jobhunter.l2.v2.audit import AUDIT_VERSION_V5 as _V3_AUDIT_VERSION
from jobhunter.l2.v2.audit import emit_schema as _v2_audit_emit_schema
from jobhunter.l2.v2.audit import emit_schema_v5 as _v3_audit_emit_schema
from jobhunter.l2.v2.audit import judge as _v2_audit_judge
from jobhunter.l2.v2.audit import render as _v2_audit_render
from jobhunter.l2.v2.audit import render_v5 as _v3_audit_render
from jobhunter.l2.v2.emit_guard import engine_emit_schema as _v2_engine_emit_schema
from jobhunter.l2.v2.facts import validator_version_for
from jobhunter.l2.v2.prompt import PROMPT_VERSION as _V2_PROMPT_VERSION
from jobhunter.l2.v2.prompt import PROMPT_VERSION_V10 as _V2_PROMPT_VERSION_V10
from jobhunter.l2.v2.prompt import TEMPLATE as _V2_TEMPLATE
from jobhunter.l2.v2.prompt import TEMPLATE_V10 as _V2_TEMPLATE_V10
from jobhunter.l2.v2.prompt import prompt_sha as _v2_prompt_sha
from jobhunter.l2.v2.prompt import prompt_sha_v10 as _v2_prompt_sha_v10
from jobhunter.l2.v2.prompt import render as _v2_render
from jobhunter.l2.v2.prompt import render_v10 as _v2_render_v10
from jobhunter.l2.v2.verify import verify as _verify_v2
from jobhunter.l2.verify import verify as _verify_v1
from jobhunter.store.extraction import split_mention

DEFAULT_BUNDLE = "v1"
#: names the CLI/env will accept; a name here that is not registered yet is a
#: teaching error, not a crash (see `config.Settings.load`).
BUNDLE_NAMES = ("v1", "v2", "v3")


@dataclass(frozen=True)
class Bundle:
    """One engine tuple: identity, prompt, and the pure functions around it.

    The callables are plain instance attributes, so `bundle.render(...)` calls
    the wrapped function itself — no descriptor binding, no `self`.
    """

    name: str  # "v1" | "v2" | "v3"
    prompt_version: str
    schema_version: str
    validator_version: str
    template: str
    prompt_sha: Callable[[], str]
    # (markdown, prior_errors, prior_emit) -> the prompt. The third argument is
    # the failed attempt's raw response, so a retry can be an edit of it rather
    # than a fresh generation (v10 retry contract); a bundle whose prompt has no
    # retry-candidate block takes it and ignores it. It has no default HERE on
    # purpose: every call site then has to say what the previous attempt left
    # behind, and "nothing" is a decision that reads in the code.
    render: Callable[[str, list[str], str | None], str]
    assemble: Callable[..., dict[str, Any]]  # raises AssembleError
    verify: Callable[[dict[str, Any], str], Report]
    profile_of: Callable[[dict[str, Any]], dict[str, Any]]
    mention_rows: Callable[[dict[str, Any]], list[tuple[str, str, str]]]
    # (mention, area_kind, importance)
    # engine-facing emit schema, when tighter than the stored contract (under
    # schema 2, the kind-importance union); None means
    # emit_schema(schema_version).
    engine_emit_schema: Callable[[], dict[str, Any]] | None = None
    # how a verify Finding renders into a retry error string; None means the
    # bare v1 form "check:code at path" (frozen v1 attempt bytes depend on it).
    render_finding: Callable[[Finding], str] | None = None
    # the agreement gate's F1 calibration for this bundle's claim granularity
    agreement_f1_min: float = 0.80
    # prior validator versions whose archived attempts fold under THIS tuple:
    # versions that changed settlement policy only, leaving assembly and
    # binding byte-identical, so a replayed corpus's attempt rows (kept at
    # their archived identity) still feed a live fold. Without this, every
    # live settle of a replayed document reads zero attempts and no-ops —
    # the 2026-09-14 stranded-repair defect (2,001 docs).
    compat_validators: tuple[str, ...] = ()
    # RETIRED `(prompt_version, schema_version)` tuples whose archived attempts
    # this one ADOPTS, and the derivation that brings their records forward.
    #
    # `compat_validators` one axis wider, and for the same reason: a fold reads
    # `extraction_attempts` scoped to its own tuple, so a partition with no
    # attempt rows is a partition no fold can move. A SCHEMA bump leaves exactly
    # that behind — the migrated rows are derived offline by `l2/rebuild` and
    # filed under this tuple, while their attempts keep the archived identity
    # they were written with and CANNOT be re-filed under a second one
    # (`extraction_attempts.attempt_key` is the archive key and the primary key
    # at once). Without this the migrated corpus would be a dead projection: no
    # live settle, no re-audit, no human review could ever reach it.
    #
    # A FALLBACK, never a union — a document actually extracted under this tuple
    # is that extraction, and the migration it superseded is history. `adopt` is
    # what the compat case does not need: the archived record is of the retired
    # SHAPE, so it is derived forward on read, by the same function the replay
    # derives it with, and a record it refuses contributes none.
    migrated_from: tuple[tuple[str, str], ...] = ()
    # (archived record, its document's markdown) -> this tuple's record shape.
    # Raises the migration's own refusal for a record it will not derive.
    adopt: Callable[[dict[str, Any], str], dict[str, Any]] | None = None
    # --- the semantic audit phase (spec §4 Auditor) -------------------------
    # `audit_version is None` means this tuple has NO audit phase: the runner
    # skips it and settlement folds without an audit probe, which is v1's
    # behaviour. A bundle that sets the version sets all four, because the
    # phase is prompt + schema + judge together, exactly like extraction.
    audit_version: str | None = None
    # (markdown, candidate_hash, record) -> the audit prompt
    audit_render: Callable[[str, str, dict[str, Any]], str] | None = None
    audit_emit_schema: Callable[[], dict[str, Any]] | None = None
    # (emit, record, markdown, candidate_hash) -> AuditOutcome; raises on any
    # validity defect, which the caller archives as an audit error
    audit_judge: Callable[[dict[str, Any], dict[str, Any], str, str], Any] | None = None


def _v1_render_prompt(
    markdown: str, prior_errors: list[str], prior_emit: str | None = None
) -> str:
    """v1's frozen two-argument `render`, behind the widened bundle signature.

    v1's prompt bytes are frozen (`demand-profile/v5` is a shipped corpus
    partition), so it has no retry-candidate block to put `prior_emit` in and
    the argument is accepted and dropped. Adapting here rather than touching
    `l2/prompt.py` is what keeps that guarantee literal.
    """
    return _v1_render(markdown, prior_errors)


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
    render=_v1_render_prompt,
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


def _v2_assemble_at(schema_version: str) -> Callable[..., dict[str, Any]]:
    """`l2.v2.assemble` at ONE schema version, behind the runner's failure
    vocabulary.

    The keyword shape already matches (`document_hash`, `observed_model`, `at`,
    `normalizer_version`); what does not match is the exception. Each contract
    raises its own `AssembleError`, and the runner catches one class to decide
    `attribution_failed` and to feed `.errors` into the next prompt. Re-raising
    v2's as v1's here keeps that decision in one place instead of teaching the
    loop a second exception per bundle — the whole point of the abstraction.

    The schema version is bound here rather than defaulted in `assemble`,
    because the record shape is the BUNDLE's choice: a tuple that archives
    attempts keyed "3" and assembles schema-2 records would key a corpus
    partition by a shape it does not hold.
    """

    def assemble(emit: dict[str, Any], markdown: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("schema_version", schema_version)
        try:
            return _assemble_v2(emit, markdown, **kwargs)
        except _V2AssembleError as exc:
            raise AssembleError(exc.errors) from exc

    return assemble


def _v2_verify_at(schema_version: str) -> Callable[[dict[str, Any], str], Report]:
    """`l2.v2.verify` reading records at this bundle's schema version."""

    def verify(record: dict[str, Any], markdown: str) -> Report:
        return _verify_v2(record, markdown, schema_version=schema_version)

    return verify


def _v2_adopt(record: dict[str, Any], markdown: str) -> dict[str, Any]:
    """A schema-2 v2 record, read as the schema-3 record the migration derives.

    The live path's half of the v20 migration, and deliberately the SAME
    function `l2/rebuild.derive_schema3` calls: a migrated row is re-folded by
    the drain (re-audit, repair, a human ruling) and by the replay, and the two
    must reach byte-identical candidates or the row's hash — what every audit
    and repair artifact is keyed by — would depend on which one folded it last.
    """
    from jobhunter.l2.v2.migrate import record3_of
    from jobhunter.l2.v2.source import annotate

    return record3_of(record, annotate(markdown))


def _v2_bundle(
    *,
    prompt_version: str,
    template: str,
    prompt_sha: Callable[[], str],
    render: Callable[[str, list[str], str | None], str],
    schema_version: str,
    migrated_from: tuple[tuple[str, str], ...] = (),
    name: str = "v2",
    compat_validators: tuple[str, ...] = ("17", "18", "19"),
    audit_version: str = _V2_AUDIT_VERSION,
    audit_render: Callable[[str, str, dict[str, Any]], str] = _v2_audit_render,
    audit_emit_schema: Callable[[], dict[str, Any]] = _v2_audit_emit_schema,
) -> Bundle:
    """One v2-family registration: same six functions, one prompt and one
    record shape. Only the prompt and the schema differ between the active
    tuple and the frozen one, so they are built from the same call — a
    hand-copied second `Bundle(...)` is how a replay tuple silently acquires a
    different judge from the one that wrote it.

    Bundle v3 (parsing contract v4) is the same family one contract later: its
    validator is whatever `facts.validator_version_for` seals its schema with
    ("22" for schema 4, "20" for 2 and 3 — so v2's registrations are
    unchanged), and it names its own audit contract and compat set. The keyword
    defaults are bundle v2's, so v2's two calls below read exactly as before.
    """
    return Bundle(
        name=name,
        prompt_version=prompt_version,
        schema_version=schema_version,
        validator_version=validator_version_for(schema_version),
        template=template,
        prompt_sha=prompt_sha,
        render=render,
        assemble=_v2_assemble_at(schema_version),
        verify=_v2_verify_at(schema_version),
        profile_of=_v2_serve.profile_of,
        mention_rows=_v2_serve.mention_rows,
        engine_emit_schema=partial(_v2_engine_emit_schema, schema_version),
        render_finding=_v2_render_finding,
        audit_version=audit_version,
        # 17 -> 18 changed settlement (dispute-set adjudication) only; assembly,
        # binding and the emit contract are byte-identical, so 17 attempts fold.
        # 18 -> 19 DOES change derivation and the check table. Only rebuild.py
        # re-judges (it re-assembles and re-verifies the archived raw emit, so a
        # unit-anchor emit lands at whatever 19 makes of it); the LIVE fold
        # serves attempt rows and records exactly as archived — an already-
        # extracted document keeps its 18-derived values until a replay, which is
        # why Task 6 measures by replay, never by the live path. Dropping 18 here
        # would strand those documents mid-ladder instead — the 2026-09-14
        # stranded-repair defect this field exists for.
        # 19 -> 20 is the same settlement shape, and the whole live corpus sits
        # at 19: leaving it out is not a smaller change than adding it — it
        # silently no-ops every live settle of every already-extracted document.
        compat_validators=compat_validators,
        migrated_from=migrated_from,
        adopt=_v2_adopt if migrated_from else None,
        audit_render=audit_render,
        audit_emit_schema=audit_emit_schema,
        audit_judge=_v2_audit_judge,
        # stays at the field default 0.80: the 2026-09-11 analysis showed the
        # borderline-F1 review cases include real polarity conflicts, and
        # recalibration without adjudicated examples only relabels the queue
    )


#: the tuple a v2 run extracts under (parsing contract v3): statements carry a
#: code-derived heading and a quoted modal phrase instead of verdicts.
_V2 = _v2_bundle(
    prompt_version=_V2_PROMPT_VERSION,
    template=_V2_TEMPLATE,
    prompt_sha=_v2_prompt_sha,
    render=_v2_render,
    schema_version="3",
    # the v20 migration (`l2/rebuild`): the archived schema-2 corpus's rows are
    # derived into this partition, so this tuple's folds must be able to read
    # the attempts behind them — which stay, forever, at the tuple they were
    # archived under.
    migrated_from=((_V2_PROMPT_VERSION_V10, "2"),),
)

#: Frozen, replay-only: the tuple the archived v2 corpus was extracted and
#: judged under. It is not in `_REGISTRY`, so no run can select it by name;
#: `get_bundle_for_tuple` resolves it, which is all a fold over archived
#: attempts needs. Retiring a prompt does not retire the attempts written under
#: it, and a tuple that stops resolving folds under the wrong shapes or under
#: none at all (the 2026-09-14 stranding defect).
_V2_SCHEMA2 = _v2_bundle(
    prompt_version=_V2_PROMPT_VERSION_V10,
    template=_V2_TEMPLATE_V10,
    prompt_sha=_v2_prompt_sha_v10,
    render=_v2_render_v10,
    schema_version="2",
)

#: parsing contract v4: the tuple a v3 run extracts under. Schema 4 adds the
#: authorization presence families (and the code-derived `authorization`
#: block), typed mentions and `relations.tracks`, judged by the schema-4
#: validator (`facts.SCHEMA_4_VALIDATOR_VERSION`: 21, then 22 from 2026-10-08,
#: which drops a dangling link beside a live one), and audited under
#: semantic-audit/v5 (its repair round, semantic-repair/v3, is wired by schema
#: version in `runner._REPAIR_CONTRACTS`). Nothing migrates into it — schema 3
#: cannot derive what the model must newly anchor (contract v4 §6) — so it
#: adopts no retired tuple and carries no compat validators: each prompt bump
#: starts a fresh partition that the entry-level drain re-extracts.
_V3 = _v2_bundle(
    prompt_version=_v15.PROMPT_VERSION,
    template=_v15.TEMPLATE,
    prompt_sha=_v15.prompt_sha,
    render=_v15.render,
    schema_version="4",
    name="v3",
    compat_validators=(),
    audit_version=_V3_AUDIT_VERSION,
    audit_render=_v3_audit_render,
    audit_emit_schema=_v3_audit_emit_schema,
)

#: v3 under demand-profile/v14 (2026-10-08): replaced by v15 the same day after
#: its test run read conditional export-control boilerplate as a citizenship
#: requirement. Frozen, replayable, not selectable.
_V3_V14 = _v2_bundle(
    prompt_version=_v14.PROMPT_VERSION,
    template=_v14.TEMPLATE,
    prompt_sha=_v14.prompt_sha,
    render=_v14.render,
    schema_version="4",
    name="v3",
    compat_validators=(),
    audit_version=_V3_AUDIT_VERSION,
    audit_render=_v3_audit_render,
    audit_emit_schema=_v3_audit_emit_schema,
)

#: v3 under demand-profile/v13 (2026-10-07): replaced by v14 on 2026-10-08
#: after its entry-level run typed the employer's own public platforms (AWS at
#: Amazon) as organizations. Frozen, replayable, not selectable.
_V3_V13 = _v2_bundle(
    prompt_version=_v13.PROMPT_VERSION,
    template=_v13.TEMPLATE,
    prompt_sha=_v13.prompt_sha,
    render=_v13.render,
    schema_version="4",
    name="v3",
    compat_validators=(),
    audit_version=_V3_AUDIT_VERSION,
    audit_render=_v3_audit_render,
    audit_emit_schema=_v3_audit_emit_schema,
)

#: v3 as it first shipped (demand-profile/v12): replaced by v13 on 2026-10-07
#: after the live run typed generic nouns as skills. Frozen like `_V2_SCHEMA2`:
#: its archived attempts replay under their own prompt; nothing may select it.
_V3_V12 = _v2_bundle(
    prompt_version=_v12.PROMPT_VERSION,
    template=_v12.TEMPLATE,
    prompt_sha=_v12.prompt_sha,
    render=_v12.render,
    schema_version="4",
    name="v3",
    compat_validators=(),
    audit_version=_V3_AUDIT_VERSION,
    audit_render=_v3_audit_render,
    audit_emit_schema=_v3_audit_emit_schema,
)

_REGISTRY: dict[str, Bundle] = {_V1.name: _V1, _V2.name: _V2, _V3.name: _V3}
#: registrations replay may resolve but nothing may select (see `_V2_SCHEMA2`)
_FROZEN: tuple[Bundle, ...] = (_V2_SCHEMA2, _V3_V12, _V3_V13, _V3_V14)


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

    Replay's inverse of `get_bundle`, over the registered bundles and the
    frozen ones. An exact tuple wins. Failing that, a FROZEN registration of
    the same schema wins: a retired prompt (v6..v9, say) still produced a
    record whose shape is its SCHEMA's, and the frozen registration is the
    tuple that owns that shape — the alternative is folding a schema-2 record
    under v1's projections, which raised KeyError('demand_profile') on the
    first mixed-archive catch-up. The fallback deliberately reaches frozen
    registrations only: an ACTIVE bundle claiming every unknown tuple at its
    schema would relabel history under a prompt it never saw.

    A tuple nothing claims raises, so the caller decides what to do about it.
    """
    for bundle in (*_REGISTRY.values(), *_FROZEN):
        if (bundle.prompt_version, bundle.schema_version) == (prompt_version, schema_version):
            return bundle
    for bundle in _FROZEN:
        if bundle.schema_version == schema_version:
            return bundle
    known = ", ".join(
        f"{b.prompt_version}/{b.schema_version}" for b in (*_REGISTRY.values(), *_FROZEN)
    )
    raise KeyError(
        f"no extraction bundle claims ({prompt_version!r}, {schema_version!r}); "
        f"registered tuples: {known}"
    )
