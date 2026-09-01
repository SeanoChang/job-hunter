"""Opt-in live check of the codex-cli engine. Writes nothing to the store.

`codex exec` must run from a real terminal on some setups, so this is a script
rather than a test. It exercises the exact invocation CodexCli uses and prints
what the engine extracted, so the event-parsing can be validated against the
codex version actually installed:

    uv run python scripts/codex_smoke.py [MODEL]

Exit 0 = the engine works and the model id was observed. Exit 1 = the call
worked but no model id could be parsed from the event stream (the runner would
record model_rejected — report the raw events so the parser can be fixed).
"""

from __future__ import annotations

import json
import subprocess
import sys

from jobhunter.l2.engines import (
    CodexCli,
    EngineFatalError,
    EngineTransportError,
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok", "who"],
    "properties": {"ok": {"type": "boolean"}, "who": {"type": "string"}},
}


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "gpt-5.6-sol"
    captured: dict[str, str] = {}

    def capturing_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(argv, **kwargs)  # type: ignore[call-overload,no-any-return]
        captured["stdout"] = proc.stdout or ""
        captured["argv"] = " ".join(argv[:2]) + " …"
        return proc  # type: ignore[no-any-return]

    engine = CodexCli(run=capturing_run)
    print(f"requesting model: {model} (effort low, fully isolated)")
    try:
        result = engine.complete('Return JSON only: ok=true and who="codex".', SCHEMA, model)
    except EngineFatalError as exc:
        print(f"FATAL (would abort the run, exit 2): {exc}")
        return 2
    except EngineTransportError as exc:
        print(f"TRANSPORT (would retry, then leave the doc pending): {exc}")
        return 2

    print(f"raw_text        : {result.raw_text[:200]}")
    print(f"observed_model  : {result.observed_model!r}")
    print(f"tokens in/out   : {result.input_tokens} / {result.output_tokens}")
    try:
        parsed = json.loads(result.raw_text)
        print(f"schema honoured : {parsed == {'ok': True, 'who': 'codex'}} -> {parsed}")
    except ValueError:
        print("schema honoured : NO — final message was not JSON")

    if result.observed_model is None:
        print(
            "\nNo model id found in the event stream. The engine fails safe "
            "(model_rejected), but observed_from_events needs the real shape.\n"
            "First 5 events:"
        )
        for line in (captured.get("stdout") or "").splitlines()[:5]:
            print(f"  {line[:300]}")
        return 1
    print("\nOK — engine usable; observed-model gate satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
