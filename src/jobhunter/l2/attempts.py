"""One immutable archive object per LLM call (harness spec §4.2). The rendered
prompt is NOT stored — it is template(prompt_version) + documents.markdown +
prior_errors, all reconstructable — and `ladder_exhausted` is stored so replay
can re-derive quarantine without knowing the run's ladder config."""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict, dataclass, fields
from typing import Any

OUTCOMES = (
    "ok",
    "transport",
    "throttled",
    "model_rejected",
    "schema_invalid",
    "attribution_failed",
    "over_budget",
    "engine_fatal",  # the provider refused the request (credentials, payment, bad request)
)


@dataclass(frozen=True)
class Attempt:
    attempt_key: str
    run_id: str
    cli_version: str
    document_hash: str
    normalizer_version: str
    sample_slot: int
    attempt_no: int
    requested_engine: str
    requested_model: str
    observed_model: str | None
    prompt_version: str
    prompt_sha256: str
    schema_version: str
    validator_version: str
    prior_errors: list[str]
    raw_response: str | None
    validation: list[dict[str, Any]]
    outcome: str
    ladder_exhausted: bool
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    started_at: str
    finished_at: str
    record: dict[str, Any] | None = None  # the assembled record, on passing attempts

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {self.outcome!r}")


def derived_error_detail(a: Attempt) -> dict[str, Any] | None:
    """The DB error_detail column, derived purely from the archived object so
    live inserts and replay produce identical rows."""
    errors = [str(v["error"]) for v in a.validation if isinstance(v, dict) and "error" in v]
    return {"errors": errors[:10]} if errors else None


def to_bytes(a: Attempt) -> bytes:
    payload = json.dumps(asdict(a), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return gzip.compress(payload, mtime=0)  # deterministic bytes for identical attempts


def from_bytes(raw: bytes) -> Attempt:
    data = json.loads(gzip.decompress(raw))
    names = {f.name for f in fields(Attempt)}
    return Attempt(**{k: v for k, v in data.items() if k in names})
