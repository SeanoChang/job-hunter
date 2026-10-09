"""`scripts/local_codex_drain.py` under a selectable bundle, against a fake engine.

The outbox path must produce attempts the runner's catch-up scan cannot tell
from its own. Two things pin that here: the attempts a drain writes carry the
SELECTED bundle's tuple (prompt, schema, validator) and its renderer's bytes,
and — the parity test — the same scripted responses driven through
`runner.run` and through the script yield the same attempt sequence: same
prompts sent, same outcomes, same fed errors, same records. No model and no
network is ever touched; the parity test needs the test Postgres for the
runner half only.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.hashing import sha256_hex
from jobhunter.l2.attempts import Attempt, from_bytes
from jobhunter.l2.bundles import get_bundle
from jobhunter.l2.engines import EngineResult, EngineTransportError
from jobhunter.l2.runner import run
from tests.l2 import test_runner as v1
from tests.l2 import test_runner_v2 as v2
from tests.l2.test_runner import _seed_doc, _settings, store  # noqa: F401

Conn = psycopg.Connection[dict[str, Any]]
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CODEX_MODEL = "gpt-6-luna"


def _script(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def drain() -> Any:
    return _script("local_codex_drain")


class Recording:
    """Answers extraction calls from a script and records what it was sent.

    An audit or repair prompt (the v2 phases the runner runs after the
    samples) never reaches the script: it is answered with a transport error,
    which the runner archives nothing for, so the two archives under
    comparison hold extraction attempts only.
    """

    name = "codex-cli"

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        if "CANDIDATE HASH:" in prompt:
            raise EngineTransportError("no auditor in this test")
        self.calls.append((prompt, schema, model))
        item = self.answers.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, str)
        return EngineResult(item, CODEX_MODEL, 40, 9, None)


def _case(bundle: str) -> tuple[str, str]:
    """(markdown, a valid raw emit) for one bundle's contract."""
    if bundle == "v1":
        return v1.DOC_MD, json.dumps(v1.EMIT)
    return v2.source("C01"), json.dumps(v2.emit_of("C01"))


def _unbound(bundle: str) -> str:
    """A candidate that parses and passes the schema but quotes text the source
    does not hold: an `attribution_failed` the retry is shown and must edit."""
    if bundle == "v1":
        emit = copy.deepcopy(v1.EMIT)
        emit["demand_profile"]["areas"][0]["claims"][0]["quote"]["text"] = "not in the source"
    else:
        emit = copy.deepcopy(v2.emit_of("C01"))
        emit["statements"][0]["modality_evidence"][0]["text"] = "not in the source"
    return json.dumps(emit)


def _queue(path: Path, markdown: str, next_attempt_no: int = 1) -> tuple[Path, str]:
    dh = sha256_hex(markdown.encode("utf-8"))
    row = {"document_hash": dh, "markdown": markdown, "next_attempt_no": next_attempt_no}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path, dh


def _outbox_attempts(outbox: Path) -> list[Attempt]:
    return [from_bytes(p.read_bytes())
            for p in sorted((outbox / keys.X_ATTEMPTS_PREFIX).rglob("*.json.gz"))]


def _tuple(bundle_name: str) -> tuple[str, str, str]:
    b = get_bundle(bundle_name)
    return b.prompt_version, b.schema_version, b.validator_version


def test_bundle_v2_stamps_attempts_with_the_v2_tuple(drain: Any, tmp_path: Path) -> None:
    markdown, good = _case("v2")
    queue, dh = _queue(tmp_path / "queue.jsonl", markdown)
    outbox = tmp_path / "outbox"
    engine = Recording([good])
    assert drain.main([str(queue), str(outbox), "--bundle", "v2"], engine=engine) == 0

    bundle = get_bundle("v2")
    [attempt] = _outbox_attempts(outbox)
    assert attempt.outcome == "ok" and attempt.document_hash == dh
    assert (attempt.prompt_version, attempt.schema_version,
            attempt.validator_version) == _tuple("v2")
    assert attempt.prompt_sha256 == bundle.prompt_sha()
    assert attempt.record is not None
    assert attempt.record["extraction"]["schema_version"] == bundle.schema_version
    # the v2 renderer and the engine-facing schema are what the engine saw
    [(prompt, schema, model)] = engine.calls
    assert prompt == bundle.render(markdown, [], None)
    assert bundle.engine_emit_schema is not None
    assert schema == bundle.engine_emit_schema()
    assert model == CODEX_MODEL


def test_the_default_bundle_follows_the_setting(
    drain: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOB_HUNTER_L2_BUNDLE", "v2")
    markdown, good = _case("v2")
    queue, _ = _queue(tmp_path / "queue.jsonl", markdown)
    outbox = tmp_path / "outbox"
    assert drain.main([str(queue), str(outbox)], engine=Recording([good])) == 0
    [attempt] = _outbox_attempts(outbox)
    assert (attempt.prompt_version, attempt.schema_version,
            attempt.validator_version) == _tuple("v2")


def test_with_no_setting_the_drain_runs_v1(
    drain: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JOB_HUNTER_L2_BUNDLE", raising=False)  # conftest isolates the rest
    markdown, good = _case("v1")
    queue, _ = _queue(tmp_path / "queue.jsonl", markdown)
    outbox = tmp_path / "outbox"
    assert drain.main([str(queue), str(outbox)], engine=Recording([good])) == 0
    [attempt] = _outbox_attempts(outbox)
    assert (attempt.prompt_version, attempt.schema_version,
            attempt.validator_version) == _tuple("v1")


def test_an_unregistered_bundle_is_refused(
    drain: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queue, _ = _queue(tmp_path / "queue.jsonl", "x")
    with pytest.raises(SystemExit) as exc:
        drain.main([str(queue), str(tmp_path / "outbox"), "--bundle", "v9"],
                   engine=Recording([]))
    assert exc.value.code == 2
    assert "v9" in capsys.readouterr().err


def test_an_outbox_holding_another_tuple_does_not_skip_the_document(
    drain: Any, tmp_path: Path
) -> None:
    """A bundle flip re-queues every document under the new tuple, so a v1
    attempt already in the outbox is no reason to skip it under v2."""
    markdown, good_v2 = _case("v2")
    queue, _ = _queue(tmp_path / "queue.jsonl", markdown)
    outbox = tmp_path / "outbox"
    # a v1 drain of the same document: content-invalid, archived all the same
    assert drain.main([str(queue), str(outbox), "--bundle", "v1"],
                      engine=Recording(["not json"] * 3)) == 0
    v2_engine = Recording([good_v2])
    assert drain.main([str(queue), str(outbox), "--bundle", "v2"], engine=v2_engine) == 0
    assert len(v2_engine.calls) == 1
    # ... and a second v2 pass over the same outbox skips it
    again = Recording([])
    assert drain.main([str(queue), str(outbox), "--bundle", "v2"], engine=again) == 0
    assert again.calls == []


def test_the_summary_says_the_audit_phase_is_deferred(
    drain: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    markdown, good = _case("v2")
    queue, _ = _queue(tmp_path / "queue.jsonl", markdown)
    drain.main([str(queue), str(tmp_path / "outbox"), "--bundle", "v2"],
               engine=Recording([good]))
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["bundle"] == "v2"
    assert summary["audit"] == "deferred"


# --- parity with runner._extract_doc ----------------------------------------


def _comparable(a: Attempt) -> dict[str, Any]:
    """An attempt minus what legitimately differs between the two writers:
    clock, key, run, engine label and spend accounting."""
    record = json.loads(json.dumps(a.record)) if a.record is not None else None
    if record is not None:
        record.get("extraction", {}).pop("at", None)
        record.get("extraction", {}).pop("extracted_at", None)
        # hashed over the record WITH its clock, so two writers a second
        # apart differ here and nowhere else (CI run 37960047437)
        record.get("extraction", {}).pop("candidate_hash", None)
    return {
        "sample_slot": a.sample_slot, "outcome": a.outcome,
        "tuple": (a.prompt_version, a.schema_version, a.validator_version),
        "prompt_sha256": a.prompt_sha256, "prior_errors": a.prior_errors,
        "raw_response": a.raw_response, "validation": a.validation,
        "ladder_exhausted": a.ladder_exhausted, "requested_model": a.requested_model,
        "observed_model": a.observed_model, "record": record,
    }


@pytest.mark.parametrize("bundle_name", ["v1", "v2"])
def test_the_script_writes_what_the_runner_writes(
    drain: Any, tmp_path: Path, pg: Conn, store: ArchiveStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch, bundle_name: str,
) -> None:
    """Same responses, same attempts: an unbound candidate, a parse failure
    (so the next retry is shown that candidate and both error halves), a fixed
    retry, then both sample slots (audit slot forced) — through the runner,
    then through the script."""
    markdown, good = _case(bundle_name)
    answers: list[Any] = [_unbound(bundle_name), "not json", good, good, good]
    dh = sha256_hex(markdown.encode("utf-8"))

    _seed_doc(pg, dh=dh, markdown=markdown, uid="gh:x:parity")
    settings = _settings(
        JOB_HUNTER_L2_BUNDLE=bundle_name, JOB_HUNTER_L2_AUDIT_MOD="1",
        JOB_HUNTER_L2_MODELS=CODEX_MODEL + "*", JOB_HUNTER_L2_MODEL_CANDIDATES=CODEX_MODEL,
    )
    runner_engine = Recording(answers)
    run(settings, pg, store, engine=runner_engine, max_docs=1, max_usd=5.0)
    runner_attempts = [from_bytes(store.get(k))
                       for k in sorted(store.list(keys.X_ATTEMPTS_PREFIX))]

    monkeypatch.setattr(drain, "AUDIT_MOD", 1)
    queue, _ = _queue(tmp_path / "queue.jsonl", markdown)
    outbox = tmp_path / "outbox"
    script_engine = Recording(answers)
    drain.main([str(queue), str(outbox), "--bundle", bundle_name], engine=script_engine)
    script_attempts = _outbox_attempts(outbox)

    assert [c[:2] for c in script_engine.calls] == [c[:2] for c in runner_engine.calls]
    by_order = sorted(runner_attempts, key=lambda a: a.attempt_no)
    assert [_comparable(a) for a in sorted(script_attempts, key=lambda a: a.attempt_no)] == [
        _comparable(a) for a in by_order
    ]
    assert [a.outcome for a in by_order] == [
        "attribution_failed", "schema_invalid", "ok", "ok", "ok",
    ]
