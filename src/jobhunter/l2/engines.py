"""Engine backends behind one small protocol. The runner is engine-agnostic;
the integration suite drives it with a scripted fake. `observed_model` always
comes from the response, never from the request (harness spec §4.1) — an
engine that cannot say what served the call reports None, and the runner
treats that as `model_rejected`."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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


class EngineFatalError(Exception):
    """Non-retryable request failure (credentials, malformed request). Retrying
    or laddering cannot help; the run aborts and the CLI exits systemic."""


def engine_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The structural schema an engine needs, without our meta declaration.

    Our schemas carry `$schema: .../draft/2020-12/schema` because the
    jsonschema library resolves it when WE validate. Engines only need the
    structure, and some refuse the meta-ref outright: `claude -p --json-schema`
    exits 1 with 'no schema with key or ref "https://json-schema.org/draft/
    2020-12/schema"' (found by the first real extraction run), and OpenAI-style
    strict json_schema modes reject unknown top-level keys.
    """
    return {k: v for k, v in schema.items() if k != "$schema"}


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
        prices: tuple[float, float] | None = None,  # USD per 1M tokens (in, out)
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.Client(timeout=timeout, headers=headers)
        if client is not None and api_key:
            client.headers.update(headers)
        self._base_url = base_url.rstrip("/")
        self._extra_body = extra_body or {}
        self._prices = prices

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8192,  # gpt-oss-class models default to 256 (spec §8)
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "demand_profile",
                    "strict": True,
                    "schema": engine_schema(schema),
                },
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
        if resp.status_code in (401, 403):
            raise EngineFatalError(f"credentials rejected (HTTP {resp.status_code})")
        if resp.status_code == 400:
            text = resp.text[:300]
            if "model" in text.lower():
                # e.g. OpenRouter's 400 for an unknown model id: ladder falls through
                raise EngineModelNotFound(f"{model}: {text}")
            raise EngineFatalError(f"bad request (HTTP 400): {text}")
        if 400 <= resp.status_code < 500:
            raise EngineFatalError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 500:
            raise EngineTransportError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, LookupError, TypeError) as exc:
            raise EngineTransportError(f"malformed completion body: {exc!r}") from exc
        if not isinstance(content, str) or not content:
            raise EngineTransportError("empty completion content")
        usage = data.get("usage") or {}
        in_tok, out_tok = usage.get("prompt_tokens"), usage.get("completion_tokens")
        cost = usage.get("cost")  # OpenRouter reports credits (USD) here when available
        if cost is None and self._prices and in_tok is not None and out_tok is not None:
            cost = in_tok / 1e6 * self._prices[0] + out_tok / 1e6 * self._prices[1]
        return EngineResult(
            raw_text=content,
            observed_model=data.get("model") or None,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=float(cost) if cost is not None else None,
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
            "--mcp-config", '{"mcpServers":{}}',
            "--json-schema", json.dumps(engine_schema(schema)),
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


def observed_from_events(stdout: str) -> tuple[str | None, int | None, int | None]:
    """(observed_model, input_tokens, output_tokens) from `codex exec --json`.

    The JSONL event schema is not a stable contract across codex versions, so
    this scans events for a model id and a usage block rather than binding to
    one shape: the first string `model` found at the top level or one nesting
    level down wins, and token counts come from a `usage` object under either
    the OpenAI (`prompt_tokens`) or codex (`input_tokens`) spelling. Because
    the engine runs with --ignore-user-config and a single requested model,
    the only model in play is the one that served the call. Nothing found ->
    None, which the runner treats as `model_rejected` (never fall back to the
    requested id: that forges provenance).
    """
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None

    def _usage(block: object) -> None:
        nonlocal tokens_in, tokens_out
        if not isinstance(block, dict):
            return
        for key in ("input_tokens", "prompt_tokens"):
            if isinstance(block.get(key), int):
                tokens_in = block[key]
        for key in ("output_tokens", "completion_tokens"):
            if isinstance(block.get(key), int):
                tokens_out = block[key]

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        candidates: list[Any] = [event]
        candidates.extend(v for v in event.values() if isinstance(v, dict))
        for scope in candidates:
            if model is None and isinstance(scope.get("model"), str) and scope["model"]:
                model = scope["model"]
            _usage(scope.get("usage"))
    return model, tokens_in, tokens_out


class CodexCli:
    """OpenAI Codex via `codex exec`, locked down to a pure completion.

    `codex exec` is an agentic loop by default: it loads ~/.codex/config.toml,
    connects MCP servers, reads plugin skills and can shell out — a live trace
    showed it spending 18k tokens reading SKILL.md files before answering
    "hi". Extraction must be a pure function of one document (Invariant I1),
    and the extraction cache identity is only sound if the prompt bytes fully
    determine the request, so the engine refuses all of that:
    --ignore-user-config (no MCP, no plugins, no configured model/effort),
    --ephemeral (no session files), a read-only sandbox, an empty scratch cwd,
    and closed stdin (codex reads stdin when it is not a TTY and blocks).
    Model and reasoning effort are passed explicitly by the harness; effort
    defaults to low because extraction is schema-bound labeling, not reasoning.

    PROVENANCE LIMIT (verified against codex-cli 0.149.1): the --json event
    stream — thread.started / turn.started / item.completed / turn.completed —
    carries token usage but no model id, so the observed-model rule (spec
    §4.1) cannot be satisfied. The engine reports None by default, which the
    runner records as `model_rejected`. `trust_requested_model=True`
    (JOB_HUNTER_L2_TRUST_REQUESTED_MODEL=1) makes it record the requested id
    instead — an ASSERTION, not an observation: a silent server-side model
    swap is undetectable in that mode, so a series built on it is only as
    trustworthy as the operator's config. A model id actually present in the
    stream always wins over the assertion.
    """

    name = "codex-cli"

    def __init__(
        self,
        reasoning_effort: str = "low",
        trust_requested_model: bool = False,
        timeout: float = 300.0,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._reasoning_effort = reasoning_effort
        self._trust_requested_model = trust_requested_model
        self._timeout = timeout
        self._run = run
        self._which = which
        self._sleep = sleep

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        with tempfile.TemporaryDirectory(prefix="jh-codex-") as work:
            schema_path = Path(work) / "schema.json"
            schema_path.write_text(json.dumps(engine_schema(schema)), encoding="utf-8")
            out_path = Path(work) / "last-message.txt"
            args = [
                "exec",
                "--json",
                "--ephemeral",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "-s", "read-only",
                "-m", model,
                "-c", f'model_reasoning_effort="{self._reasoning_effort}"',
                "--output-schema", str(schema_path),
                "--output-last-message", str(out_path),
                prompt,
            ]
            for attempt in range(3):  # the CLI self-updates; the binary can vanish briefly
                exe = self._which("codex")
                if exe:
                    try:
                        proc = self._run(
                            [exe, *args],
                            capture_output=True,
                            text=True,
                            timeout=self._timeout,
                            stdin=subprocess.DEVNULL,
                            cwd=work,
                        )
                        break
                    except FileNotFoundError:
                        pass
                    except subprocess.TimeoutExpired as exc:
                        raise EngineTransportError(f"codex exec timed out: {exc}") from exc
                self._sleep(2.0 * (attempt + 1))
            else:
                raise EngineTransportError("codex CLI not found on PATH")

            stderr = (proc.stderr or "")[:400]
            if proc.returncode != 0:
                lowered = stderr.lower()
                if any(s in lowered for s in ("not logged in", "codex login", "unauthorized")):
                    raise EngineFatalError(f"codex auth failed: {stderr}")
                raise EngineTransportError(f"codex exited {proc.returncode}: {stderr}")
            try:
                raw_text = out_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise EngineTransportError(f"codex wrote no final message: {exc}") from exc
            if not raw_text:
                raise EngineTransportError("codex final message was empty")

        observed, tokens_in, tokens_out = observed_from_events(proc.stdout or "")
        if observed is None and self._trust_requested_model:
            observed = model  # asserted provenance; see PROVENANCE LIMIT above
        return EngineResult(
            raw_text=raw_text,
            observed_model=observed,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=None,  # codex reports no per-call cost; price via JOB_HUNTER_L2_PRICE
        )
