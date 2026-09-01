import json
import subprocess
from typing import Any

import httpx
import pytest

from jobhunter.l2.engines import (
    ClaudeCli,
    EngineModelNotFound,
    EngineThrottled,
    EngineTransportError,
    OpenAICompat,
)

SCHEMA = {"type": "object"}


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x.test")


def test_openai_compat_happy_path() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "model": "z-ai/glm-5.2:free",
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 9},
            },
        )

    eng = OpenAICompat("https://x.test/api/v1", "sk-test", client=_client(handler))
    result = eng.complete("hello", SCHEMA, "z-ai/glm-5.2:free")
    assert result.raw_text == '{"ok": true}'
    assert result.observed_model == "z-ai/glm-5.2:free"
    assert result.input_tokens == 40 and result.output_tokens == 9
    assert seen["auth"] == "Bearer sk-test"
    body = seen["body"]
    assert body["model"] == "z-ai/glm-5.2:free"
    assert body["max_tokens"] == 8192
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA


@pytest.mark.parametrize(
    "status,exc",
    [(429, EngineThrottled), (404, EngineModelNotFound), (500, EngineTransportError)],
)
def test_openai_compat_error_statuses(status: int, exc: type[Exception]) -> None:
    eng = OpenAICompat(
        "https://x.test/api/v1",
        None,
        client=_client(lambda _: httpx.Response(status, json={"error": "nope"})),
    )
    with pytest.raises(exc):
        eng.complete("p", SCHEMA, "m")


def test_openai_compat_missing_content_is_transport() -> None:
    eng = OpenAICompat(
        "https://x.test/api/v1",
        None,
        client=_client(lambda _: httpx.Response(200, json={"model": "m", "choices": []})),
    )
    with pytest.raises(EngineTransportError):
        eng.complete("p", SCHEMA, "m")


def _cli_result(stdout: dict[str, Any], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=json.dumps(stdout),
                                       stderr="")


def test_claude_cli_happy_path() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _cli_result(
            {
                "structured_output": {"ok": True},
                "modelUsage": {"claude-sonnet-5-20260514": {}},
                "total_cost_usd": 0.0,
            }
        )

    eng = ClaudeCli(run=fake_run, which=lambda _: "/usr/bin/claude")
    result = eng.complete("prompt", SCHEMA, "sonnet")
    assert json.loads(result.raw_text) == {"ok": True}
    assert result.observed_model == "claude-sonnet-5-20260514"
    assert "--json-schema" in calls[0]


def test_claude_cli_errors() -> None:
    eng_err = ClaudeCli(
        run=lambda *a, **k: _cli_result({"is_error": True, "result": "boom"}),
        which=lambda _: "/usr/bin/claude",
    )
    with pytest.raises(EngineTransportError):
        eng_err.complete("p", SCHEMA, "m")

    eng_rc = ClaudeCli(
        run=lambda *a, **k: _cli_result({}, returncode=1), which=lambda _: "/usr/bin/claude"
    )
    with pytest.raises(EngineTransportError):
        eng_rc.complete("p", SCHEMA, "m")

    eng_missing = ClaudeCli(
        run=lambda *a, **k: _cli_result({}), which=lambda _: None, sleep=lambda _: None
    )
    with pytest.raises(EngineTransportError):
        eng_missing.complete("p", SCHEMA, "m")


def test_claude_cli_no_model_usage_is_null_model() -> None:
    eng = ClaudeCli(
        run=lambda *a, **k: _cli_result({"structured_output": {}}),
        which=lambda _: "/usr/bin/claude",
    )
    result = eng.complete("p", SCHEMA, "sonnet")
    assert result.observed_model is None  # never the requested alias: null-over-guess


def test_claude_cli_multi_model_usage_picks_the_worker() -> None:
    eng = ClaudeCli(
        run=lambda *a, **k: _cli_result(
            {
                "structured_output": {},
                "modelUsage": {
                    "claude-haiku-4-5": {"inputTokens": 30, "outputTokens": 5},
                    "claude-sonnet-5-20260514": {"inputTokens": 4000, "outputTokens": 800},
                },
            }
        ),
        which=lambda _: "/usr/bin/claude",
    )
    result = eng.complete("p", SCHEMA, "sonnet")
    assert result.observed_model == "claude-sonnet-5-20260514"  # the entry that did the work


def test_openai_compat_permanent_4xx_taxonomy() -> None:
    from jobhunter.l2.engines import EngineFatalError

    for status in (401, 403, 422):
        eng = OpenAICompat(
            "https://x.test/api/v1", None,
            client=_client(lambda _, s=status: httpx.Response(s, json={})),
        )
        with pytest.raises(EngineFatalError):
            eng.complete("p", SCHEMA, "m")
    model_400 = OpenAICompat(
        "https://x.test/api/v1", None,
        client=_client(lambda _: httpx.Response(400, json={"error": "no such model: nope"})),
    )
    with pytest.raises(EngineModelNotFound):
        model_400.complete("p", SCHEMA, "nope")
    other_400 = OpenAICompat(
        "https://x.test/api/v1", None,
        client=_client(lambda _: httpx.Response(400, json={"error": "bad json_schema field"})),
    )
    with pytest.raises(EngineFatalError):
        other_400.complete("p", SCHEMA, "m")


def test_openai_compat_cost_from_usage_and_prices() -> None:
    reported = OpenAICompat(
        "https://x.test/api/v1", None,
        client=_client(lambda _: httpx.Response(200, json={
            "model": "m", "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 4000, "completion_tokens": 1000, "cost": 0.0123},
        })),
    )
    assert reported.complete("p", SCHEMA, "m").cost_usd == 0.0123

    priced = OpenAICompat(
        "https://x.test/api/v1", None,
        client=_client(lambda _: httpx.Response(200, json={
            "model": "m", "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        })),
        prices=(0.35, 0.75),
    )
    assert priced.complete("p", SCHEMA, "m").cost_usd == pytest.approx(1.10)


def test_claude_cli_strips_schema_meta_ref() -> None:
    """`claude -p --json-schema` cannot resolve the draft-2020-12 meta-ref and
    exits 1 — found by the first real extraction run."""
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen["schema"] = json.loads(cmd[cmd.index("--json-schema") + 1])
        return _cli_result({"structured_output": {"ok": True}, "modelUsage": {"m": {}}})

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
    }
    ClaudeCli(run=fake_run, which=lambda _: "/usr/bin/claude").complete("p", schema, "m")
    assert "$schema" not in seen["schema"]
    assert seen["schema"]["properties"] == {"ok": {"type": "boolean"}}
