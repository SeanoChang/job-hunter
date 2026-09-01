"""CodexCli engine: invocation lockdown, file plumbing, event parsing.

`codex exec` is an agentic loop by default (MCP servers, plugins/skills, shell
access). Extraction must be a pure function of the document, so most of what
this file asserts is what the engine REFUSES to inherit.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from jobhunter.l2.engines import (
    CodexCli,
    EngineFatalError,
    EngineTransportError,
    observed_from_events,
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"ok": {"type": "boolean"}},
}


class FakeRun:
    """Stands in for subprocess.run: records argv/kwargs and writes the
    --output-last-message file the way codex does."""

    def __init__(self, last_message: str | None, stdout: str = "", returncode: int = 0) -> None:
        self.last_message = last_message
        self.stdout = stdout
        self.returncode = returncode
        self.argv: list[str] = []
        self.kwargs: dict[str, Any] = {}

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        self.kwargs = kwargs
        if self.last_message is not None:
            out_path = Path(argv[argv.index("--output-last-message") + 1])
            out_path.write_text(self.last_message, encoding="utf-8")
        return subprocess.CompletedProcess(
            args=argv, returncode=self.returncode, stdout=self.stdout, stderr="boom"
        )


def _engine(run: FakeRun, **kw: Any) -> CodexCli:
    return CodexCli(run=run, which=lambda _: "/usr/bin/codex", sleep=lambda _: None, **kw)


def test_invocation_is_locked_down() -> None:
    run = FakeRun('{"ok": true}')
    _engine(run).complete("PROMPT", SCHEMA, "gpt-5.6-sol")
    argv = run.argv
    assert argv[0] == "/usr/bin/codex" and argv[1] == "exec"
    # no user config: kills MCP servers and plugins/skills in one flag
    assert "--ignore-user-config" in argv
    assert "--ephemeral" in argv          # no session files
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-m") + 1] == "gpt-5.6-sol"   # requested model is explicit
    assert argv[-1] == "PROMPT"           # prompt is the trailing positional
    # effort is passed by the harness, never inherited from ~/.codex/config.toml
    assert any(a.startswith("model_reasoning_effort=") for a in argv)
    # stdin must be closed: codex reads stdin when it is not a TTY and blocks
    assert run.kwargs["stdin"] is subprocess.DEVNULL
    # and it runs in a scratch cwd, so a read-only sandbox still sees no repo
    assert run.kwargs["cwd"] != str(Path.cwd())


def test_schema_is_written_for_the_cli() -> None:
    captured: dict[str, Any] = {}

    class SchemaCapturingRun(FakeRun):
        def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["schema"] = json.loads(
                Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8")
            )
            return super().__call__(argv, **kwargs)

    run = SchemaCapturingRun('{"ok": true}')
    _engine(run).complete("p", SCHEMA, "m")
    assert captured["schema"] == SCHEMA


def test_effort_override() -> None:
    run = FakeRun('{"ok": true}')
    _engine(run, reasoning_effort="medium").complete("p", SCHEMA, "m")
    assert 'model_reasoning_effort="medium"' in run.argv


def test_raw_text_comes_from_the_last_message_file() -> None:
    run = FakeRun('{"ok": true, "who": "codex"}')
    result = _engine(run).complete("p", SCHEMA, "m")
    assert json.loads(result.raw_text) == {"ok": True, "who": "codex"}


def test_missing_last_message_is_transport() -> None:
    run = FakeRun(None)  # codex exited 0 but wrote nothing
    with pytest.raises(EngineTransportError):
        _engine(run).complete("p", SCHEMA, "m")


def test_nonzero_exit_is_transport() -> None:
    run = FakeRun(None, returncode=1)
    with pytest.raises(EngineTransportError):
        _engine(run).complete("p", SCHEMA, "m")


def test_auth_failure_is_fatal() -> None:
    class AuthRun(FakeRun):
        def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="",
                stderr="ERROR: not logged in. Run `codex login`.",
            )

    with pytest.raises(EngineFatalError):
        _engine(AuthRun(None)).complete("p", SCHEMA, "m")


def test_binary_missing_is_transport() -> None:
    engine = CodexCli(run=FakeRun('{"ok":1}'), which=lambda _: None, sleep=lambda _: None)
    with pytest.raises(EngineTransportError):
        engine.complete("p", SCHEMA, "m")


def test_timeout_is_transport() -> None:
    def timing_out(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)

    engine = CodexCli(run=timing_out, which=lambda _: "/usr/bin/codex", sleep=lambda _: None)
    with pytest.raises(EngineTransportError):
        engine.complete("p", SCHEMA, "m")


# --- event parsing --------------------------------------------------------
# observed_model is read from the --json event stream, never from the -m we
# requested (harness spec §4.1). The parser is deliberately shape-tolerant:
# codex's JSONL event schema is not contractual across versions.

def test_observed_from_events_typical_shape() -> None:
    events = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "abc"}),
        json.dumps({"type": "turn.started", "model": "gpt-5.6-sol"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 4000, "output_tokens": 900}}),
    ])
    model, tin, tout = observed_from_events(events)
    assert model == "gpt-5.6-sol" and tin == 4000 and tout == 900


def test_observed_from_events_nested_and_alternate_keys() -> None:
    events = "\n".join([
        json.dumps({"type": "session.created", "session": {"model": "gpt-5.6-terra"}}),
        json.dumps({"usage": {"prompt_tokens": 12, "completion_tokens": 3}}),
    ])
    model, tin, tout = observed_from_events(events)
    assert model == "gpt-5.6-terra" and tin == 12 and tout == 3


def test_observed_from_events_absent_is_none() -> None:
    events = "\n".join([json.dumps({"type": "turn.started"}), "not json at all"])
    assert observed_from_events(events) == (None, None, None)


def test_unresolvable_model_fails_safe() -> None:
    """No model id in the stream -> observed_model None -> the runner records
    model_rejected. Never fall back to the requested id: that would forge
    provenance for a response we cannot attribute."""
    run = FakeRun('{"ok": true}', stdout='{"type":"turn.started"}')
    result = _engine(run).complete("p", SCHEMA, "gpt-5.6-sol")
    assert result.observed_model is None


# --- provenance: codex reports no model id --------------------------------
# Verified against codex-cli 0.149.1: the --json vocabulary is
# thread.started / turn.started / item.completed / turn.completed, and none
# of those events carries a model field. The observed-model rule (spec §4.1)
# therefore cannot be satisfied, so trusting the requested id is opt-in.

REAL_EVENTS = "\n".join([
    '{"type":"thread.started","thread_id":"01a043a8-1095-7883-95e7-7a5b59ea19ab"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
    '"text":"{\\"ok\\":true}"}}',
    '{"type":"turn.completed","usage":{"input_tokens":15418,"cached_input_tokens":0,'
    '"cache_write_input_tokens":0,"output_tokens":20,"reasoning_output_tokens":0}}',
])


def test_real_codex_events_yield_tokens_but_no_model() -> None:
    model, tin, tout = observed_from_events(REAL_EVENTS)
    assert model is None          # codex simply does not report it
    assert tin == 15418 and tout == 20


def test_default_fails_safe_on_real_events() -> None:
    run = FakeRun('{"ok": true}', stdout=REAL_EVENTS)
    result = _engine(run).complete("p", SCHEMA, "gpt-5.6-sol")
    assert result.observed_model is None   # -> runner records model_rejected
    assert result.input_tokens == 15418


def test_trust_requested_model_is_opt_in() -> None:
    run = FakeRun('{"ok": true}', stdout=REAL_EVENTS)
    engine = _engine(run, trust_requested_model=True)
    result = engine.complete("p", SCHEMA, "gpt-5.6-sol")
    assert result.observed_model == "gpt-5.6-sol"  # asserted, not observed


def test_trust_flag_never_overrides_a_reported_model() -> None:
    """If a future codex does report a model, the reported one wins — the flag
    fills a gap, it never overwrites real provenance."""
    events = REAL_EVENTS + '\n{"type":"turn.started","model":"gpt-5.7-actual"}'
    run = FakeRun('{"ok": true}', stdout=events)
    result = _engine(run, trust_requested_model=True).complete("p", SCHEMA, "gpt-5.6-sol")
    assert result.observed_model == "gpt-5.7-actual"


# --- engine-ready schema --------------------------------------------------
# Found by the first real run: `claude -p --json-schema` rejects our emit
# schema with 'no schema with key or ref "https://json-schema.org/draft/
# 2020-12/schema"'. The $schema meta-ref is for OUR validators (jsonschema
# resolves it); engines only need the structural schema, and some of them
# refuse to fetch or resolve remote refs.

REAL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "job-hunter L2 emit schema",
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
}


def test_codex_strips_schema_meta_ref() -> None:
    seen: dict[str, Any] = {}

    class Capture(FakeRun):
        def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen["schema"] = json.loads(
                Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8")
            )
            return super().__call__(argv, **kwargs)

    _engine(Capture('{"ok": true}')).complete("p", REAL_SCHEMA, "m")
    assert "$schema" not in seen["schema"]
    assert seen["schema"]["properties"] == {"ok": {"type": "boolean"}}  # structure intact
