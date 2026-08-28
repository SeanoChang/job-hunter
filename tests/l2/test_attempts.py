import dataclasses
import gzip
import json

import pytest

from jobhunter.l2.attempts import Attempt, from_bytes, to_bytes
from jobhunter.l2.prompt import PROMPT_VERSION
from jobhunter.l2.transforms import VALIDATOR_VERSION


def _attempt(**overrides: object) -> Attempt:
    base: dict[str, object] = {
        "attempt_key": "extractions/attempts/2026/08/27T061204Z-abcdefabcdef-s1a1.json.gz",
        "run_id": "r1",
        "cli_version": "0.1.0",
        "document_hash": "ab" * 32,
        "normalizer_version": "md/1",
        "sample_slot": 1,
        "attempt_no": 1,
        "requested_engine": "openai-compat",
        "requested_model": "z-ai/glm-5.2:free",
        "observed_model": "z-ai/glm-5.2:free",
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": "0" * 64,
        "schema_version": "1",
        "validator_version": VALIDATOR_VERSION,
        "prior_errors": [],
        "raw_response": "{}",
        "validation": [],
        "outcome": "ok",
        "ladder_exhausted": False,
        "input_tokens": 40,
        "output_tokens": 9,
        "cost_usd": 0.0,
        "started_at": "2026-08-27T06:12:04Z",
        "finished_at": "2026-08-27T06:12:09Z",
    }
    base.update(overrides)
    return Attempt(**base)  # type: ignore[arg-type]


def test_roundtrip_and_gzip() -> None:
    a = _attempt(prior_errors=["quote not found: 'x'"], validation=[{"check": "schema"}])
    raw = to_bytes(a)
    assert raw[:2] == b"\x1f\x8b"  # gzip magic
    assert from_bytes(raw) == a
    payload = json.loads(gzip.decompress(raw))
    assert payload["outcome"] == "ok" and payload["prior_errors"] == ["quote not found: 'x'"]


def test_unknown_outcome_rejected() -> None:
    with pytest.raises(ValueError):
        _attempt(outcome="exploded")


def test_frozen() -> None:
    a = _attempt()
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.outcome = "transport"  # type: ignore[misc]
