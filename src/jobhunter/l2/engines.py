"""Engine backends behind one small protocol. The runner is engine-agnostic;
the integration suite drives it with a scripted fake. `observed_model` always
comes from the response, never from the request (harness spec §4.1) — an
engine that cannot say what served the call reports None, and the runner
treats that as `model_rejected`."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class EngineResult:
    raw_text: str
    observed_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


class EngineTransportError(Exception):
    pass


class EngineThrottled(EngineTransportError):
    """Explicit rate-limit response — the runner stops the batch, not the doc."""


class EngineModelNotFound(Exception):
    """The requested model id does not exist — the ladder falls through."""


class Engine(Protocol):
    name: str

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult: ...


class OpenAICompat:
    """Any OpenAI-compatible endpoint (OpenRouter, Cloudflare, vLLM, ollama).

    `provider.require_parameters` is NOT sent by default: the ladder may mix
    schema-capable and JSON-mode-only rungs, and requiring the parameter would
    leave the non-schema rung with no eligible endpoint. Endpoints that ignore
    response_format are caught by the validator chain.
    """

    name = "openai-compat"

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        client: httpx.Client | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.Client(timeout=timeout, headers=headers)
        if client is not None and api_key:
            client.headers.update(headers)
        self._base_url = base_url.rstrip("/")
        self._extra_body = extra_body or {}

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8192,  # gpt-oss-class models default to 256 (spec §8)
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "demand_profile", "strict": True, "schema": schema},
            },
            **self._extra_body,
        }
        try:
            resp = self._client.post(f"{self._base_url}/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise EngineTransportError(str(exc)) from exc
        if resp.status_code == 429:
            raise EngineThrottled(f"429 from {self._base_url}")
        if resp.status_code == 404:
            raise EngineModelNotFound(model)
        if resp.status_code >= 400:
            raise EngineTransportError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, LookupError, TypeError) as exc:
            raise EngineTransportError(f"malformed completion body: {exc!r}") from exc
        if not isinstance(content, str) or not content:
            raise EngineTransportError("empty completion content")
        usage = data.get("usage") or {}
        return EngineResult(
            raw_text=content,
            observed_model=data.get("model") or None,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cost_usd=None,
        )


class ClaudeCli:
    """The owner's own agent via `claude -p` — supervised backfill sessions.

    Mirrors the wiring proven in prototypes/parsing/retree.py: structured
    output via --json-schema, observed model from modelUsage, and a retry for
    the binary-vanishes-during-self-update window.
    """

    name = "claude-cli"

    def __init__(
        self,
        timeout: float = 300.0,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._timeout = timeout
        self._run = run
        self._which = which
        self._sleep = sleep

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        args = [
            "-p", prompt, "--model", model, "--output-format", "json",
            "--no-session-persistence", "--tools", "", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--json-schema", json.dumps(schema),
        ]
        for attempt in range(3):  # the CLI self-updates in place; the binary can vanish briefly
            exe = self._which("claude")
            if exe:
                try:
                    proc = self._run(
                        [exe, *args], capture_output=True, text=True, timeout=self._timeout
                    )
                    break
                except FileNotFoundError:
                    pass
                except subprocess.TimeoutExpired as exc:
                    raise EngineTransportError(f"claude -p timed out: {exc}") from exc
            self._sleep(2.0 * (attempt + 1))
        else:
            raise EngineTransportError("claude CLI not found on PATH")
        if proc.returncode != 0:
            raise EngineTransportError(f"claude exited {proc.returncode}: {proc.stderr[:300]}")
        try:
            data = json.loads(proc.stdout)
        except ValueError as exc:
            raise EngineTransportError(f"claude output not JSON: {exc}") from exc
        if data.get("is_error") or "structured_output" not in data:
            raise EngineTransportError(f"no structured output: {str(data.get('result'))[:200]}")
        model_usage = data.get("modelUsage") or {}

        def _tokens(entry: object) -> int:
            if not isinstance(entry, dict):
                return 0
            return int(entry.get("inputTokens") or 0) + int(entry.get("outputTokens") or 0)

        # a -p session can record side-model usage; attribute to the entry that
        # did the work, with a sorted tiebreak so the pick is deterministic
        observed = (
            max(sorted(model_usage), key=lambda k: _tokens(model_usage[k]))
            if model_usage else None
        )
        return EngineResult(
            raw_text=json.dumps(data["structured_output"], ensure_ascii=False),
            observed_model=observed,
            input_tokens=None,
            output_tokens=None,
            cost_usd=data.get("total_cost_usd"),
        )
