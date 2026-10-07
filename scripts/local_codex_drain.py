"""Drain the extraction queue locally through codex-cli, writing archive
attempt objects into an outbox directory — no database, no archive access.

The upload half of the pipeline (see .github/workflows/outbox-ingest.yml)
copies the outbox into R2 under the same keys; the runner's own catch-up scan
then records and settles them, so a locally produced attempt is
indistinguishable from one the CI drain made. This script mirrors
`runner._extract_doc` exactly — same outcome vocabulary, same content-retry
and ladder rules, same retry-carries-the-candidate edit contract and deletion
check, same k-sampling — with the DB reads replaced by the queue dump
(extract-queue-dump.yml artifact: one JSON object per line with
document_hash, markdown, next_attempt_no). `tests/test_local_codex_drain.py`
pins that parity by driving both with the same responses.

The engine tuple comes from a `Bundle` (`--bundle`, default the
`JOB_HUNTER_L2_BUNDLE` setting, else v1), exactly as the runner takes it: the
prompt, its version and sha, the emit schema, assembly, verification and the
retry-error wording are all the bundle's. The queue must be dumped under the
same bundle (`extract-queue-dump.yml -f bundle=...`).

A bundle with a semantic audit phase (v2) gets its EXTRACTION phases here and
its audit later. The runner audits between the samples and the single
settlement, but that step folds the document's attempt history out of the
database to pick the candidate, and its artifacts live outside the attempt
namespace the outbox ingest accepts — neither exists on this side of the
pipeline. So the audit is deferred, never faked: the ingest's catch-up settles
the document with its audit `not_checked` (the runner's own "auditor
unreachable" outcome, `AuditPhase.unreachable`), the fold flags it
`audit_retry`, and the next `extract run` with a document budget audits it in
`_reaudit_pass` — which takes `validated` rows only, so a `needs_review`
cohort stays unaudited until a human `retry`.

Usage:
    uv run python scripts/local_codex_drain.py queue.jsonl outbox/ --max-docs 25 [--bundle v2]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from jobhunter import __version__
from jobhunter.archive import keys
from jobhunter.config import env_snapshot
from jobhunter.l2.assemble import AssembleError
from jobhunter.l2.attempts import Attempt, from_bytes, to_bytes
from jobhunter.l2.bundles import DEFAULT_BUNDLE, Bundle, get_bundle
from jobhunter.l2.engines import (
    CodexCli,
    Engine,
    EngineFatalError,
    EngineModelNotFound,
    EngineThrottled,
    EngineTransportError,
)
from jobhunter.l2.report import Report
from jobhunter.l2.runner import (
    CONTENT_ATTEMPTS,
    MAX_DOC_CHARS,
    SAMPLE_CONTENT_ATTEMPTS,
    TRANSPORT_RETRIES,
    unexplained_deletions,
)
from jobhunter.l2.schemas import emit_schema, normalize_emit, validate_emit
from jobhunter.l2.state import model_matches
from jobhunter.markdown import NORMALIZER_VERSION
from jobhunter.timeutil import iso, utcnow_precise

MODEL = "gpt-6-luna"
GLOBS = (MODEL + "*",)
AUDIT_MOD = 20  # spec §4.5: 5% deterministic audit by hash slot


class _Doc:
    def __init__(self, row: dict[str, Any]) -> None:
        self.hash = str(row["document_hash"])
        self.markdown = str(row["markdown"])
        self.next_attempt_no = int(row["next_attempt_no"])


def default_bundle() -> str:
    """The bundle a run selects when `--bundle` is not given: the
    `JOB_HUNTER_L2_BUNDLE` setting through config's layered env, else v1.

    Read without `Settings.load`, which demands an archive URL this machine
    deliberately does not have; `get_bundle` validates the name instead.
    """
    return (env_snapshot().get("JOB_HUNTER_L2_BUNDLE") or "").strip() or DEFAULT_BUNDLE


def _drained(outbox: Path, dh: str, bundle: Bundle) -> bool:
    """Has this document already been drained under THIS bundle's tuple?

    The key carries no tuple, so a matching key is opened to read it: after a
    bundle flip the queue re-offers every document, and an outbox still holding
    the previous tuple's attempts must not make the new tuple skip them.
    """
    marker = f"-{dh[:12]}-"
    attempts = outbox / keys.X_ATTEMPTS_PREFIX
    if not attempts.is_dir():
        return False
    want = (bundle.prompt_version, bundle.schema_version, bundle.validator_version)
    for path in attempts.rglob("*.json.gz"):
        if marker not in path.name:
            continue
        a = from_bytes(path.read_bytes())
        if a.document_hash == dh and (
            a.prompt_version, a.schema_version, a.validator_version
        ) == want:
            return True
    return False


def _findings(report: Report) -> list[dict[str, Any]]:
    return [
        {"check": f.check, "path": f.path, "code": f.code,
         "severity": f.severity, "detail": f.detail}
        for f in report.findings
    ]


def _errors(report: Report, bundle: Bundle) -> list[str]:
    rf = bundle.render_finding
    return [rf(f) if rf is not None else f"{f.check}:{f.code} at {f.path}"
            for f in report.findings if f.severity == "error"]


def _drain_doc(
    doc: _Doc, outbox: Path, engine: Engine, run_id: str, bundle: Bundle
) -> str:
    """One document through the runner's exact loop; returns its disposition."""
    dh = doc.hash
    seq = doc.next_attempt_no - 1
    schema = (bundle.engine_emit_schema() if bundle.engine_emit_schema is not None
              else emit_schema(bundle.schema_version))

    def put_attempt(
        *,
        requested_model: str,
        observed_model: str | None,
        outcome: str,
        raw_response: str | None,
        fed: list[str],
        produced: list[str],
        ladder_exhausted: bool,
        findings: list[dict[str, Any]] | None = None,
        tokens: tuple[int | None, int | None] = (None, None),
        started_at: datetime | None = None,
        record: dict[str, Any] | None = None,
        sample_slot: int = 1,
    ) -> None:
        nonlocal seq
        seq += 1
        t0 = started_at or utcnow_precise()
        validation = list(findings or []) + [{"error": e} for e in produced]
        attempt = Attempt(
            attempt_key=keys.x_attempt_key(t0, dh, sample_slot, seq),
            run_id=run_id,
            cli_version=__version__,
            document_hash=dh,
            normalizer_version=NORMALIZER_VERSION,
            sample_slot=sample_slot,
            attempt_no=seq,
            requested_engine=engine.name,
            requested_model=requested_model,
            observed_model=observed_model,
            prompt_version=bundle.prompt_version,
            prompt_sha256=bundle.prompt_sha(),
            schema_version=bundle.schema_version,
            validator_version=bundle.validator_version,
            prior_errors=list(fed),
            raw_response=raw_response,
            validation=validation,
            outcome=outcome,
            ladder_exhausted=ladder_exhausted,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cost_usd=None,  # codex-cli reports no per-call cost
            started_at=iso(t0),
            finished_at=iso(utcnow_precise()),
            record=record,
        )
        path = outbox / attempt.attempt_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(to_bytes(attempt))

    markdown = doc.markdown
    if len(markdown) > MAX_DOC_CHARS:
        put_attempt(
            requested_model=MODEL, observed_model=None, outcome="over_budget",
            raw_response=None, fed=[],
            produced=[f"document {len(markdown)} chars > {MAX_DOC_CHARS}"],
            ladder_exhausted=True,
        )
        return "over_budget"

    prior_errors: list[str] = []
    # the last candidate that parsed: fed back verbatim so a retry edits it
    # (v10 retry contract), and the baseline of the deletion check
    prior_raw: str | None = None
    prior_emit: dict[str, Any] | None = None
    transports = 0
    content_no = 0
    while content_no < CONTENT_ATTEMPTS:
        t0 = utcnow_precise()
        prompt = bundle.render(markdown, prior_errors, prior_raw)
        try:
            result = engine.complete(prompt, schema, MODEL)
        except EngineThrottled as exc:
            put_attempt(requested_model=MODEL, observed_model=None, outcome="throttled",
                        raw_response=None, fed=prior_errors, produced=[str(exc)],
                        ladder_exhausted=False, started_at=t0)
            return "throttled"
        except EngineFatalError as exc:
            put_attempt(requested_model=MODEL, observed_model=None, outcome="engine_fatal",
                        raw_response=None, fed=prior_errors, produced=[str(exc)],
                        ladder_exhausted=False, started_at=t0)
            return "fatal"
        except EngineModelNotFound:
            put_attempt(requested_model=MODEL, observed_model=None, outcome="model_rejected",
                        raw_response=None, fed=prior_errors, produced=["model not found"],
                        ladder_exhausted=False, started_at=t0)
            return "fatal"
        except EngineTransportError as exc:
            transports += 1
            put_attempt(requested_model=MODEL, observed_model=None, outcome="transport",
                        raw_response=None, fed=prior_errors, produced=[str(exc)],
                        ladder_exhausted=False, started_at=t0)
            if transports >= TRANSPORT_RETRIES:
                return "pending"
            continue

        observed = result.observed_model
        tokens = (result.input_tokens, result.output_tokens)
        if not model_matches(observed, GLOBS):
            put_attempt(requested_model=MODEL, observed_model=observed,
                        outcome="model_rejected", raw_response=result.raw_text,
                        fed=prior_errors,
                        produced=[f"observed model {observed!r} outside globs"],
                        ladder_exhausted=False, started_at=t0, tokens=tokens)
            return "model_rejected"
        assert observed is not None
        content_no += 1
        exhausted = content_no == CONTENT_ATTEMPTS  # single-rung ladder locally

        try:
            emit = json.loads(result.raw_text)
            if not isinstance(emit, dict):
                raise ValueError("top level is not an object")
            emit = normalize_emit(emit, bundle.schema_version)
        except ValueError as exc:
            errors = [f"response is not valid JSON: {exc}"]
            put_attempt(requested_model=MODEL, observed_model=observed,
                        outcome="schema_invalid", raw_response=result.raw_text,
                        fed=prior_errors, produced=errors, ladder_exhausted=exhausted,
                        started_at=t0, tokens=tokens)
            # both halves: the parse failure and what the shown candidate failed on
            prior_errors = errors + [
                e for e in prior_errors if not e.startswith("response is not valid JSON")
            ]
            continue
        asked = [e for e in prior_errors if not e.startswith("retry:unexplained_deletion")]
        dropped = (
            unexplained_deletions(prior_emit, emit, asked) if prior_emit is not None else []
        )
        if not dropped:
            prior_raw, prior_emit = result.raw_text, emit
        if schema_errors := validate_emit(emit, bundle.schema_version):
            errors = schema_errors + dropped
            put_attempt(requested_model=MODEL, observed_model=observed,
                        outcome="schema_invalid", raw_response=result.raw_text,
                        fed=prior_errors, produced=errors, ladder_exhausted=exhausted,
                        started_at=t0, tokens=tokens)
            prior_errors = errors
            continue
        try:
            record = bundle.assemble(emit, markdown, document_hash=dh,
                                     normalizer_version=NORMALIZER_VERSION,
                                     observed_model=observed, at=iso(t0))
        except AssembleError as exc:
            errors = exc.errors + dropped
            put_attempt(requested_model=MODEL, observed_model=observed,
                        outcome="attribution_failed", raw_response=result.raw_text,
                        fed=prior_errors, produced=errors, ladder_exhausted=exhausted,
                        started_at=t0, tokens=tokens)
            prior_errors = errors
            continue
        report = bundle.verify(record, markdown)
        findings = _findings(report)
        if report.status == "fail" or dropped:
            errors = _errors(report, bundle) + dropped
            put_attempt(requested_model=MODEL, observed_model=observed,
                        outcome="attribution_failed", raw_response=result.raw_text,
                        fed=prior_errors, produced=errors, findings=findings,
                        ladder_exhausted=exhausted, started_at=t0, tokens=tokens)
            prior_errors = errors
            continue
        put_attempt(requested_model=MODEL, observed_model=observed, outcome="ok",
                    raw_response=result.raw_text, fed=prior_errors, produced=[],
                    findings=findings, ladder_exhausted=False, started_at=t0,
                    tokens=tokens, record=record)

        # k-sampling (spec §4.5, narrowed by parsing contract v3 §5): the 5%
        # deterministic audit slot by hash, and nothing else.
        sampled = AUDIT_MOD <= 1 or int(dh[:8], 16) % AUDIT_MOD == 0
        if sampled and _take_samples(
            markdown, dh, schema, engine, bundle, put_attempt
        ) == "throttled":
            return "throttled"
        return "ok"
    return "exhausted"


def _take_samples(
    markdown: str, dh: str, schema: dict[str, Any], engine: Engine, bundle: Bundle,
    put_attempt: Any,
) -> str | None:
    """Slots 2 and 3, as `runner._take_samples`: single-shot generations with
    one transport retry and SAMPLE_CONTENT_ATTEMPTS content attempts each."""
    slots: list[tuple[int, bool, int, list[str]]] = [(2, False, 1, []), (3, False, 1, [])]
    while slots:
        slot, retried, content_no, prior_errors = slots.pop(0)
        t0 = utcnow_precise()
        prompt = bundle.render(markdown, prior_errors, None)
        try:
            result = engine.complete(prompt, schema, MODEL)
        except EngineThrottled as exc:
            put_attempt(requested_model=MODEL, observed_model=None, outcome="throttled",
                        raw_response=None, fed=[], produced=[str(exc)],
                        ladder_exhausted=False, started_at=t0, sample_slot=slot)
            return "throttled"
        except EngineTransportError as exc:
            put_attempt(requested_model=MODEL, observed_model=None, outcome="transport",
                        raw_response=None, fed=[], produced=[str(exc)],
                        ladder_exhausted=False, started_at=t0, sample_slot=slot)
            if not retried:
                slots.insert(0, (slot, True, content_no, prior_errors))
            continue
        except (EngineModelNotFound, EngineFatalError) as exc:
            put_attempt(requested_model=MODEL, observed_model=None, outcome="engine_fatal",
                        raw_response=None, fed=[], produced=[str(exc)],
                        ladder_exhausted=False, started_at=t0, sample_slot=slot)
            continue
        observed = result.observed_model
        common: dict[str, Any] = {
            "requested_model": MODEL, "observed_model": observed,
            "raw_response": result.raw_text, "fed": list(prior_errors),
            "ladder_exhausted": False, "started_at": t0, "sample_slot": slot,
            "tokens": (result.input_tokens, result.output_tokens),
        }

        def _content_retry(errors: list[str], *, _slot: int = slot,
                           _retried: bool = retried, _n: int = content_no) -> None:
            if _n < SAMPLE_CONTENT_ATTEMPTS:
                slots.insert(0, (_slot, _retried, _n + 1, errors))

        if not model_matches(observed, GLOBS):
            put_attempt(outcome="model_rejected",
                        produced=[f"observed model {observed!r} outside globs"], **common)
            continue
        assert observed is not None
        try:
            emit = json.loads(result.raw_text)
            if not isinstance(emit, dict):
                raise ValueError("top level is not an object")
            emit = normalize_emit(emit, bundle.schema_version)
        except ValueError as exc:
            errors = [f"response is not valid JSON: {exc}"]
            put_attempt(outcome="schema_invalid", produced=errors, **common)
            _content_retry(errors)
            continue
        if schema_errors := validate_emit(emit, bundle.schema_version):
            put_attempt(outcome="schema_invalid", produced=schema_errors, **common)
            _content_retry(schema_errors)
            continue
        try:
            record = bundle.assemble(emit, markdown, document_hash=dh,
                                     normalizer_version=NORMALIZER_VERSION,
                                     observed_model=observed, at=iso(t0))
        except AssembleError as exc:
            put_attempt(outcome="attribution_failed", produced=exc.errors, **common)
            _content_retry(exc.errors)
            continue
        report = bundle.verify(record, markdown)
        findings = _findings(report)
        if report.status == "fail":
            errors = _errors(report, bundle)
            put_attempt(outcome="attribution_failed", produced=errors, findings=findings,
                        **common)
            _content_retry(errors)
            continue
        put_attempt(outcome="ok", produced=[], findings=findings, record=record, **common)
    return None


def main(argv: Sequence[str] | None = None, *, engine: Engine | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("queue", type=Path, help="queue.jsonl from extract-queue-dump")
    ap.add_argument("outbox", type=Path)
    ap.add_argument("--max-docs", type=int, default=25)
    ap.add_argument("--bundle", default=None,
                    help="engine tuple to extract under (default: JOB_HUNTER_L2_BUNDLE, else v1)")
    args = ap.parse_args(argv)
    name = args.bundle or default_bundle()
    try:
        bundle = get_bundle(name)
    except KeyError as exc:
        ap.error(str(exc.args[0]))

    started = utcnow_precise()
    run_id = f"xlocal-{iso(started).replace(':', '').replace('-', '')}"
    engine = engine if engine is not None else CodexCli(trust_requested_model=True)
    if bundle.audit_version is not None:
        print(f"local_codex_drain: bundle {name} has an audit phase ({bundle.audit_version}); "
              "it is deferred to the next `extract run` after ingest (_reaudit_pass)",
              file=sys.stderr, flush=True)
    counts: dict[str, int] = {}
    attempted = 0
    with args.queue.open(encoding="utf-8") as fh:
        for line in fh:
            if attempted >= args.max_docs:
                break
            doc = _Doc(json.loads(line))
            if _drained(args.outbox, doc.hash, bundle):
                continue
            attempted += 1
            disposition = _drain_doc(doc, args.outbox, engine, run_id, bundle)
            counts[disposition] = counts.get(disposition, 0) + 1
            print(json.dumps({"doc": doc.hash[:12], "disposition": disposition}), flush=True)
            if disposition == "throttled":
                break
    print(json.dumps({
        "run_id": run_id, "bundle": name, "attempted": attempted, "counts": counts,
        "audit": "deferred" if bundle.audit_version is not None else "none",
    }))
    return 3 if counts.get("throttled") else 0


if __name__ == "__main__":
    sys.exit(main())
