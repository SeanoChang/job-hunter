"""Drain the extraction queue locally through codex-cli, writing archive
attempt objects into an outbox directory — no database, no archive access.

The upload half of the pipeline (see .github/workflows/outbox-ingest.yml)
copies the outbox into R2 under the same keys; the runner's own catch-up scan
then records and settles them, so a locally produced attempt is
indistinguishable from one the CI drain made. This script mirrors
`runner._extract_doc` exactly — same outcome vocabulary, same content-retry
and ladder rules, same k-sampling — with the DB reads replaced by the queue
dump (extract-queue-dump.yml artifact: one JSON object per line with
document_hash, markdown, next_attempt_no).

Usage:
    uv run python scripts/local_codex_drain.py queue.jsonl outbox/ --max-docs 25
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from jobhunter import __version__
from jobhunter.archive import keys
from jobhunter.l2.assemble import AssembleError, assemble
from jobhunter.l2.attempts import Attempt, to_bytes
from jobhunter.l2.engines import (
    CodexCli,
    Engine,
    EngineFatalError,
    EngineModelNotFound,
    EngineThrottled,
    EngineTransportError,
)
from jobhunter.l2.prompt import PROMPT_VERSION, prompt_sha, render
from jobhunter.l2.runner import CONTENT_ATTEMPTS, MAX_DOC_CHARS, SCHEMA_VERSION, TRANSPORT_RETRIES
from jobhunter.l2.schemas import emit_schema, normalize_emit, validate_emit
from jobhunter.l2.state import model_matches
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.l2.verify import verify
from jobhunter.markdown import NORMALIZER_VERSION
from jobhunter.timeutil import iso, utcnow_precise

MODEL = "gpt-5.6-luna"
GLOBS = (MODEL + "*",)
AUDIT_MOD = 20  # spec §4.5: 5% deterministic audit by hash slot


class _Doc:
    def __init__(self, row: dict[str, Any]) -> None:
        self.hash = str(row["document_hash"])
        self.markdown = str(row["markdown"])
        self.next_attempt_no = int(row["next_attempt_no"])


def _drained(outbox: Path, dh: str) -> bool:
    marker = f"-{dh[:12]}-"
    attempts = outbox / keys.X_ATTEMPTS_PREFIX
    return attempts.is_dir() and any(marker in p.name for p in attempts.rglob("*.json.gz"))


def _drain_doc(doc: _Doc, outbox: Path, engine: Engine, run_id: str) -> str:
    """One document through the runner's exact loop; returns its disposition."""
    dh = doc.hash
    seq = doc.next_attempt_no - 1
    schema = emit_schema(SCHEMA_VERSION)
    ok_count = 0

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
            prompt_version=PROMPT_VERSION,
            prompt_sha256=prompt_sha(),
            schema_version=SCHEMA_VERSION,
            validator_version=VALIDATOR_VERSION,
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
    transports = 0
    content_no = 0
    while content_no < CONTENT_ATTEMPTS:
        t0 = utcnow_precise()
        prompt = render(markdown, prior_errors)
        try:
            result = engine.complete(prompt, schema, MODEL)
        except EngineThrottled as exc:
            put_attempt(requested_model=MODEL, observed_model=None, outcome="throttled",
                        raw_response=None, fed=prior_errors, produced=[str(exc)],
                        ladder_exhausted=False, started_at=t0)
            return "throttled"
        except (EngineFatalError, EngineModelNotFound) as exc:
            outcome = "engine_fatal" if isinstance(exc, EngineFatalError) else "model_rejected"
            put_attempt(requested_model=MODEL, observed_model=None, outcome=outcome,
                        raw_response=None, fed=prior_errors, produced=[str(exc)],
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
        if not model_matches(observed, GLOBS):
            put_attempt(requested_model=MODEL, observed_model=observed,
                        outcome="model_rejected", raw_response=result.raw_text,
                        fed=prior_errors,
                        produced=[f"observed model {observed!r} outside globs"],
                        ladder_exhausted=False, started_at=t0,
                        tokens=(result.input_tokens, result.output_tokens))
            return "model_rejected"
        assert observed is not None
        content_no += 1
        exhausted = content_no == CONTENT_ATTEMPTS  # single-rung ladder locally

        try:
            emit = json.loads(result.raw_text)
            if not isinstance(emit, dict):
                raise ValueError("top level is not an object")
            emit = normalize_emit(emit, SCHEMA_VERSION)
        except ValueError as exc:
            errors = [f"response is not valid JSON: {exc}"]
            put_attempt(requested_model=MODEL, observed_model=observed,
                        outcome="schema_invalid", raw_response=result.raw_text,
                        fed=prior_errors, produced=errors, ladder_exhausted=exhausted,
                        started_at=t0, tokens=(result.input_tokens, result.output_tokens))
            prior_errors = errors
            continue
        if schema_errors := validate_emit(emit, SCHEMA_VERSION):
            put_attempt(requested_model=MODEL, observed_model=observed,
                        outcome="schema_invalid", raw_response=result.raw_text,
                        fed=prior_errors, produced=schema_errors, ladder_exhausted=exhausted,
                        started_at=t0, tokens=(result.input_tokens, result.output_tokens))
            prior_errors = schema_errors
            continue
        try:
            record = assemble(emit, markdown, document_hash=dh,
                              normalizer_version=NORMALIZER_VERSION,
                              observed_model=observed, at=iso(t0))
        except AssembleError as exc:
            put_attempt(requested_model=MODEL, observed_model=observed,
                        outcome="attribution_failed", raw_response=result.raw_text,
                        fed=prior_errors, produced=exc.errors, ladder_exhausted=exhausted,
                        started_at=t0, tokens=(result.input_tokens, result.output_tokens))
            prior_errors = exc.errors
            continue
        report = verify(record, markdown)
        findings: list[dict[str, Any]] = [
            {"check": f.check, "path": f.path, "code": f.code,
             "severity": f.severity, "detail": f.detail}
            for f in report.findings
        ]
        if report.status == "fail":
            errors = [f"{f.check}:{f.code} at {f.path}"
                      for f in report.findings if f.severity == "error"]
            put_attempt(requested_model=MODEL, observed_model=observed,
                        outcome="attribution_failed", raw_response=result.raw_text,
                        fed=prior_errors, produced=errors, findings=findings,
                        ladder_exhausted=exhausted, started_at=t0,
                        tokens=(result.input_tokens, result.output_tokens))
            prior_errors = errors
            continue
        put_attempt(requested_model=MODEL, observed_model=observed, outcome="ok",
                    raw_response=result.raw_text, fed=prior_errors, produced=[],
                    findings=findings, ladder_exhausted=False, started_at=t0,
                    tokens=(result.input_tokens, result.output_tokens), record=record)
        ok_count += 1

        # k-sampling (spec §4.5): 5% deterministic audit by hash slot, plus any
        # document whose slot-1 pass needed a reprompt.
        audit = AUDIT_MOD <= 1 or int(dh[:8], 16) % AUDIT_MOD == 0
        reprompted = bool(prior_errors) or content_no > 1
        if audit or reprompted:
            for slot in (2, 3):
                st0 = utcnow_precise()
                sprompt = render(markdown, [])
                try:
                    sres = engine.complete(sprompt, schema, MODEL)
                except EngineThrottled as exc:
                    put_attempt(requested_model=MODEL, observed_model=None,
                                outcome="throttled", raw_response=None, fed=[],
                                produced=[str(exc)], ladder_exhausted=False,
                                started_at=st0, sample_slot=slot)
                    return "throttled"
                except (EngineTransportError, EngineModelNotFound, EngineFatalError) as exc:
                    put_attempt(requested_model=MODEL, observed_model=None,
                                outcome="transport", raw_response=None, fed=[],
                                produced=[str(exc)], ladder_exhausted=False,
                                started_at=st0, sample_slot=slot)
                    continue
                sobs = sres.observed_model
                srecord: dict[str, Any] | None = None
                soutcome = "ok"
                sproduced: list[str] = []
                sfindings: list[dict[str, Any]] = []
                if not model_matches(sobs, GLOBS):
                    soutcome, sproduced = "model_rejected", [f"observed {sobs!r} outside globs"]
                else:
                    assert sobs is not None  # model_matches guarantees it
                    try:
                        semit = json.loads(sres.raw_text)
                        if not isinstance(semit, dict):
                            raise ValueError("top level is not an object")
                        semit = normalize_emit(semit, SCHEMA_VERSION)
                        if serrs := validate_emit(semit, SCHEMA_VERSION):
                            soutcome, sproduced = "schema_invalid", serrs
                        else:
                            srec = assemble(semit, markdown, document_hash=dh,
                                            normalizer_version=NORMALIZER_VERSION,
                                            observed_model=sobs, at=iso(st0))
                            sreport = verify(srec, markdown)
                            sfindings = [
                                {"check": f.check, "path": f.path, "code": f.code,
                                 "severity": f.severity, "detail": f.detail}
                                for f in sreport.findings
                            ]
                            if sreport.status == "fail":
                                soutcome = "attribution_failed"
                                sproduced = [f"{f.check}:{f.code} at {f.path}"
                                             for f in sreport.findings
                                             if f.severity == "error"]
                            else:
                                srecord = srec
                    except ValueError as exc:
                        soutcome = "schema_invalid"
                        sproduced = [f"response is not valid JSON: {exc}"]
                    except AssembleError as exc:
                        soutcome, sproduced = "attribution_failed", exc.errors
                put_attempt(requested_model=MODEL, observed_model=sobs, outcome=soutcome,
                            raw_response=sres.raw_text, fed=[], produced=sproduced,
                            findings=sfindings, ladder_exhausted=False, started_at=st0,
                            tokens=(sres.input_tokens, sres.output_tokens),
                            record=srecord, sample_slot=slot)
        return "ok"
    return "exhausted"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("queue", type=Path, help="queue.jsonl from extract-queue-dump")
    ap.add_argument("outbox", type=Path)
    ap.add_argument("--max-docs", type=int, default=25)
    args = ap.parse_args()

    started = utcnow_precise()
    run_id = f"xlocal-{iso(started).replace(':', '').replace('-', '')}"
    engine = CodexCli(trust_requested_model=True)
    counts: dict[str, int] = {}
    attempted = 0
    with args.queue.open(encoding="utf-8") as fh:
        for line in fh:
            if attempted >= args.max_docs:
                break
            doc = _Doc(json.loads(line))
            if _drained(args.outbox, doc.hash):
                continue
            attempted += 1
            disposition = _drain_doc(doc, args.outbox, engine, run_id)
            counts[disposition] = counts.get(disposition, 0) + 1
            print(json.dumps({"doc": doc.hash[:12], "disposition": disposition}), flush=True)
            if disposition == "throttled":
                break
    print(json.dumps({"run_id": run_id, "attempted": attempted, "counts": counts}))
    return 3 if counts.get("throttled") else 0


if __name__ == "__main__":
    sys.exit(main())
