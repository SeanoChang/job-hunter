"""The extraction drain loop (harness spec §4.4/§4.6). Serial in M2.

Archive-before-DB per attempt; a crash between the two is healed by the next
run's catch-up scan. The ladder (`l2_model_candidates`) escalates on content
failure and falls through on model-not-found; quarantine only after the ladder
is exhausted. `observed_model` gates everything: out-of-glob responses are
`model_rejected`, and five consecutive rejections abort the run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatch
from typing import Any

from jobhunter import __version__
from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.config import Settings
from jobhunter.l2 import verify
from jobhunter.l2.assemble import AssembleError, assemble
from jobhunter.l2.attempts import Attempt, derived_error_detail, from_bytes, to_bytes
from jobhunter.l2.engines import (
    Engine,
    EngineModelNotFound,
    EngineThrottled,
    EngineTransportError,
)
from jobhunter.l2.prompt import PROMPT_VERSION, TEMPLATE, prompt_sha, render
from jobhunter.l2.schemas import emit_schema, validate_emit
from jobhunter.l2.state import DerivedState, derive_state
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.markdown import NORMALIZER_VERSION
from jobhunter.store import db, extraction
from jobhunter.store.extraction import Conn
from jobhunter.timeutil import iso, utcnow

SCHEMA_VERSION = "1"
MAX_DOC_CHARS = 60_000
CONTENT_ATTEMPTS = 3
TRANSPORT_RETRIES = 3
BREAKER_LIMIT = 5


@dataclass
class ExtractSummary:
    run_id: str
    lock_held: bool = False
    docs_attempted: int = 0
    validated: int = 0
    quarantined: int = 0
    pending: int = 0
    throttled: bool = False
    breaker_abort: bool = False
    replayed: int = 0
    spend_usd: float = 0.0
    queued: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "lock_held": self.lock_held,
            "docs_attempted": self.docs_attempted, "validated": self.validated,
            "quarantined": self.quarantined, "pending": self.pending,
            "throttled": self.throttled, "breaker_abort": self.breaker_abort,
            "replayed": self.replayed, "spend_usd": round(self.spend_usd, 5),
            "queued": self.queued,
        }


def _ensure_write_once(store: ArchiveStore) -> None:
    pk = keys.x_prompt_key(PROMPT_VERSION)
    if not store.exists(pk):
        store.put(pk, TEMPLATE.encode("utf-8"))
    sk = keys.x_schema_key(SCHEMA_VERSION)
    if not store.exists(sk):
        store.put(sk, json.dumps(emit_schema(SCHEMA_VERSION), sort_keys=True).encode("utf-8"))


def _settle(conn: Conn, dh: str, globs: tuple[str, ...], profile: dict[str, Any] | None,
            updated_at: str) -> DerivedState:
    attempts = extraction.attempts_for(conn, dh)
    reviews = extraction.reviews_for(conn, dh)
    state = derive_state(attempts, reviews, globs)
    chosen = {a.attempt_key: a for a in attempts}.get(state.chosen_attempt or "")
    model_col = (
        (chosen.observed_model if chosen else None)
        or (attempts[-1].requested_model if attempts else None)
    )
    if model_col is None:
        return state  # nothing decisive ever happened; no row to write
    extraction.upsert_state(
        conn, document_hash=dh, model=model_col, prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION, validator_version=VALIDATOR_VERSION,
        state=state,
        # the profile survives review demotions: reviews judge, they never regenerate
        profile=profile if state.status in ("validated", "needs_review") else None,
        reviewed_by=reviews[-1].actor if reviews else None,
        updated_at=updated_at,
    )
    return state


def _profile_of(record: dict[str, Any]) -> dict[str, Any]:
    return {"facts": record["facts"], "demand_profile": record["demand_profile"]}


def _catch_up(conn: Conn, store: ArchiveStore, globs: tuple[str, ...], updated_at: str) -> int:
    mark = extraction.watermark(conn)
    start_after = None
    if mark is not None:
        stamp = iso(mark)
        start_after = (
            f"{keys.X_ATTEMPTS_PREFIX}{stamp[0:4]}/{stamp[5:7]}/"
            f"{stamp[8:10]}T{stamp[11:19].replace(':', '')}Z"
        )
    replayed = 0
    touched: dict[str, dict[str, Any] | None] = {}
    for key in store.list(keys.X_ATTEMPTS_PREFIX, start_after=start_after):
        parsed = keys.parse_x_attempt_key(key)
        if parsed is None:
            continue
        if mark is not None and parsed[0] <= mark:
            continue
        attempt = from_bytes(store.get(key))
        extraction.record_attempt(conn, attempt, derived_error_detail(attempt))
        replayed += 1
        if attempt.outcome == "ok" and attempt.record is not None:
            touched[attempt.document_hash] = _profile_of(attempt.record)
        else:
            touched.setdefault(attempt.document_hash, None)
    for dh, profile in touched.items():
        _settle(conn, dh, globs, profile, updated_at)
    return replayed


def run(
    settings: Settings,
    conn: Conn,
    store: ArchiveStore,
    *,
    engine: Engine,
    max_docs: int,
    max_usd: float,
    only_doc: str | None = None,
    dry_run: bool = False,
    now: Callable[[], datetime] = utcnow,
) -> ExtractSummary:
    started = now()
    summary = ExtractSummary(run_id=f"x-{iso(started).replace(':', '').replace('-', '')}")
    if not db.try_lock(conn, db.EXTRACT_LOCK_KEY):
        summary.lock_held = True
        return summary
    try:
        _ensure_write_once(store)
        summary.replayed = _catch_up(conn, store, settings.l2_models, iso(now()))
        conn.commit()
        model_regex = extraction.globs_to_regex(settings.l2_model_candidates or ("*",))
        docs = [only_doc] if only_doc else extraction.queue(
            conn, prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
            validator_version=VALIDATOR_VERSION, model_regex=model_regex,
            normalizer_version=NORMALIZER_VERSION, limit=max_docs,
        )
        summary.queued = docs
        if dry_run:
            return summary
        breaker = 0
        for dh in docs:
            if summary.docs_attempted >= max_docs or summary.spend_usd >= max_usd:
                break
            result = _extract_doc(settings, conn, store, engine, dh, summary, breaker, now)
            if result is None:
                continue  # document vanished (normalizer bump mid-flight)
            disposition, breaker = result
            conn.commit()
            if disposition == "validated":
                summary.validated += 1
            elif disposition == "quarantined":
                summary.quarantined += 1
            elif disposition == "pending":
                summary.pending += 1
            elif disposition == "throttled":
                summary.throttled = True
                break
            elif disposition == "breaker":
                summary.breaker_abort = True
                break
    finally:
        conn.commit()
        db.unlock(conn, db.EXTRACT_LOCK_KEY)
        conn.commit()
    return summary


def _extract_doc(
    settings: Settings,
    conn: Conn,
    store: ArchiveStore,
    engine: Engine,
    dh: str,
    summary: ExtractSummary,
    breaker: int,
    now: Callable[[], datetime],
) -> tuple[str, int] | None:
    markdown = extraction.markdown_for(conn, dh, NORMALIZER_VERSION)
    if markdown is None:
        return None
    summary.docs_attempted += 1
    seq = 0
    schema = emit_schema(SCHEMA_VERSION)

    def archive_attempt(
        *, requested_model: str, observed_model: str | None, outcome: str,
        raw_response: str | None, fed: list[str], produced: list[str],
        ladder_exhausted: bool, findings: list[dict[str, Any]] | None = None,
        tokens: tuple[int | None, int | None] = (None, None),
        cost: float | None = None, started_at: datetime | None = None,
        record: dict[str, Any] | None = None,
    ) -> Attempt:
        # `fed` reproduces the rendered prompt (spec §4.2: prior_errors is the
        # non-reconstructible part of the request); `produced` is what this
        # attempt's validation yielded, stored in the trace + DB error_detail.
        nonlocal seq
        seq += 1
        t0 = started_at or now()
        validation = list(findings or []) + [{"error": e} for e in produced]
        attempt = Attempt(
            attempt_key=keys.x_attempt_key(t0, dh, 1, seq),
            run_id=summary.run_id, cli_version=__version__, document_hash=dh,
            normalizer_version=NORMALIZER_VERSION, sample_slot=1, attempt_no=seq,
            requested_engine=engine.name, requested_model=requested_model,
            observed_model=observed_model, prompt_version=PROMPT_VERSION,
            prompt_sha256=prompt_sha(), schema_version=SCHEMA_VERSION,
            validator_version=VALIDATOR_VERSION, prior_errors=list(fed),
            raw_response=raw_response, validation=validation, outcome=outcome,
            ladder_exhausted=ladder_exhausted, input_tokens=tokens[0],
            output_tokens=tokens[1], cost_usd=cost, started_at=iso(t0),
            finished_at=iso(now()), record=record,
        )
        store.put(attempt.attempt_key, to_bytes(attempt))
        extraction.record_attempt(conn, attempt, derived_error_detail(attempt))
        return attempt

    if len(markdown) > MAX_DOC_CHARS:
        archive_attempt(
            requested_model=(settings.l2_model_candidates or ("?",))[0], observed_model=None,
            outcome="over_budget", raw_response=None, fed=[],
            produced=[f"document {len(markdown)} chars > {MAX_DOC_CHARS}"],
            ladder_exhausted=True,
        )
        _settle(conn, dh, settings.l2_models, None, iso(now()))
        return "quarantined", breaker

    candidates = settings.l2_model_candidates or ("?",)
    for rung_i, model in enumerate(candidates):
        last_rung = rung_i == len(candidates) - 1
        prior_errors: list[str] = []
        transports = 0
        content_no = 0
        while content_no < CONTENT_ATTEMPTS:
            t0 = now()
            prompt = render(markdown, prior_errors)
            try:
                result = engine.complete(prompt, schema, model)
            except EngineThrottled as exc:
                archive_attempt(requested_model=model, observed_model=None,
                                outcome="throttled", raw_response=None,
                                fed=prior_errors, produced=[str(exc)],
                                ladder_exhausted=False, started_at=t0)
                return "throttled", breaker
            except EngineModelNotFound:
                archive_attempt(requested_model=model, observed_model=None,
                                outcome="model_rejected", raw_response=None,
                                fed=prior_errors, produced=["model not found"],
                                ladder_exhausted=False, started_at=t0)
                breaker += 1
                if breaker >= BREAKER_LIMIT:
                    return "breaker", breaker
                break  # next rung
            except EngineTransportError as exc:
                transports += 1
                archive_attempt(requested_model=model, observed_model=None,
                                outcome="transport", raw_response=None,
                                fed=prior_errors, produced=[str(exc)],
                                ladder_exhausted=False, started_at=t0)
                if transports >= TRANSPORT_RETRIES:
                    return "pending", breaker  # transport says nothing about the doc
                continue

            summary.spend_usd += result.cost_usd or 0.0
            observed = result.observed_model
            if not observed or not any(fnmatch(observed, g) for g in settings.l2_models):
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="model_rejected", raw_response=result.raw_text,
                                fed=prior_errors,
                                produced=[f"observed model {observed!r} outside globs"],
                                ladder_exhausted=False, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                breaker += 1
                if breaker >= BREAKER_LIMIT:
                    return "breaker", breaker
                break  # next rung
            breaker = 0
            content_no += 1
            exhausted = last_rung and content_no == CONTENT_ATTEMPTS

            try:
                emit = json.loads(result.raw_text)
                if not isinstance(emit, dict):
                    raise ValueError("top level is not an object")
            except ValueError as exc:
                errors = [f"response is not valid JSON: {exc}"]
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="schema_invalid", raw_response=result.raw_text,
                                fed=prior_errors, produced=errors,
                                ladder_exhausted=exhausted, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                prior_errors = errors
                continue
            if schema_errors := validate_emit(emit, SCHEMA_VERSION):
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="schema_invalid", raw_response=result.raw_text,
                                fed=prior_errors, produced=schema_errors,
                                ladder_exhausted=exhausted, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                prior_errors = schema_errors
                continue
            try:
                record = assemble(emit, markdown, document_hash=dh,
                                  normalizer_version=NORMALIZER_VERSION,
                                  observed_model=observed, at=iso(t0))
            except AssembleError as exc:
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="attribution_failed", raw_response=result.raw_text,
                                fed=prior_errors, produced=exc.errors,
                                ladder_exhausted=exhausted, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                prior_errors = exc.errors
                continue
            report = verify(record, markdown)
            findings: list[dict[str, Any]] = [
                {"check": f.check, "path": f.path, "code": f.code,
                 "severity": f.severity, "detail": f.detail}
                for f in report.findings
            ]
            if report.status == "fail":
                errors = [
                    f"{f.check}:{f.code} at {f.path}"
                    for f in report.findings if f.severity == "error"
                ]
                archive_attempt(requested_model=model, observed_model=observed,
                                outcome="attribution_failed", raw_response=result.raw_text,
                                fed=prior_errors, produced=errors, findings=findings,
                                ladder_exhausted=exhausted, started_at=t0,
                                tokens=(result.input_tokens, result.output_tokens),
                                cost=result.cost_usd)
                prior_errors = errors
                continue
            archive_attempt(requested_model=model, observed_model=observed, outcome="ok",
                            raw_response=result.raw_text, fed=prior_errors, produced=[],
                            findings=findings, ladder_exhausted=False, started_at=t0,
                            tokens=(result.input_tokens, result.output_tokens),
                            cost=result.cost_usd, record=record)
            _settle(conn, dh, settings.l2_models, _profile_of(record), iso(now()))
            return "validated", breaker

    _settle(conn, dh, settings.l2_models, None, iso(now()))
    return "quarantined", breaker
