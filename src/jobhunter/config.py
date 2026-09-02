"""Settings from the environment. Nothing else reads os.environ.

Values come from three layers, highest first: process env, `./.env`,
`~/.config/job-hunter/env`. Passing `env` to `Settings.load` bypasses the
files entirely — callers that supply an environment mean exactly it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


# A stray PATH= or SHELL= in a .env is the author's shell business, not ours.
_FILE_KEY_PREFIXES = ("JOB_HUNTER_", "AWS_")


def _read_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` lines; `#` comments and blanks skipped, values taken verbatim."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith(_FILE_KEY_PREFIXES):
            out[key] = value.strip()
    return out


def load_env_files(environ: Mapping[str, str]) -> dict[str, str]:
    """Merge config layers: process env > ./.env > ~/.config/job-hunter/env.

    `environ` is the winning layer. Where the config file *lives* is a property
    of the running process, so XDG_CONFIG_HOME/HOME fall back to os.environ when
    a caller passes a partial mapping.
    """

    def hint(name: str, default: str = "") -> str:
        return environ.get(name) or os.environ.get(name) or default

    xdg = hint("XDG_CONFIG_HOME") or str(Path(hint("HOME", "~")).expanduser() / ".config")
    merged = _read_env_file(Path(xdg) / "job-hunter" / "env")
    merged.update(_read_env_file(Path.cwd() / ".env"))
    merged.update(environ)
    return merged


def env_snapshot() -> dict[str, str]:
    """The merged layers as one mapping.

    `doctor` reports per variable — including the AWS_* ones no `Settings` field
    carries — and this module stays the only reader of `os.environ`.
    """
    return load_env_files(os.environ)


@dataclass(frozen=True, slots=True)
class Settings:
    archive_url: str
    registry_path: Path
    home: Path
    database_url: str | None
    state_dir: Path = Path("~/.local/state/job-hunter")
    drop_ratio: float = 0.5
    ping_url: str | None = None
    mcp_token: str | None = None  # the bearer the hosted MCP server demands
    l2_engine: str = "openai-compat"
    l2_base_url: str | None = None
    l2_api_key: str | None = None
    l2_models: tuple[str, ...] = ("*",)
    l2_model_candidates: tuple[str, ...] = ()
    l2_max_docs: int = 300
    l2_max_usd: float = 5.0
    l2_price: tuple[float, float] | None = None  # USD per 1M tokens (in, out)
    l2_reasoning_effort: str = "low"  # codex-cli: extraction is labeling, not reasoning
    # codex reports no model id; opt in to recording the requested one as
    # asserted (not observed) provenance — a silent swap becomes undetectable
    l2_trust_requested_model: bool = False

    def require_l2(self) -> None:
        if not self.l2_model_candidates:
            raise ConfigError("JOB_HUNTER_L2_MODEL_CANDIDATES is required for extraction")
        if self.l2_engine == "openai-compat" and not self.l2_base_url:
            raise ConfigError("engine openai-compat needs JOB_HUNTER_L2_BASE_URL")

    def require_database_url(self) -> str:
        if not self.database_url:
            raise ConfigError(
                "JOB_HUNTER_DATABASE_URL is required for this command (Postgres DSN)"
            )
        return self.database_url

    def require_mcp_token(self) -> str:
        if not self.mcp_token:
            raise ConfigError(
                "JOB_HUNTER_MCP_TOKEN is required to serve MCP (the bearer clients send)"
            )
        return self.mcp_token

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> Settings:
        e: Mapping[str, str] = load_env_files(os.environ) if env is None else env
        archive_url = e.get("JOB_HUNTER_ARCHIVE_URL")
        if not archive_url:
            raise ConfigError(
                "JOB_HUNTER_ARCHIVE_URL is required (s3://bucket/prefix or file:///path)"
            )
        home_default = Path(e.get("HOME", "~")).expanduser() / ".local/share/job-hunter"
        raw_ratio = e.get("JOB_HUNTER_DROP_RATIO", "0.5")
        try:
            drop_ratio = float(raw_ratio)
        except ValueError as ex:
            raise ConfigError(
                f"JOB_HUNTER_DROP_RATIO must be a number, got {raw_ratio!r}"
            ) from ex
        if not 0 < drop_ratio <= 1:
            raise ConfigError(f"JOB_HUNTER_DROP_RATIO must be in (0, 1], got {drop_ratio}")
        engine = e.get("JOB_HUNTER_L2_ENGINE", "openai-compat")
        engines = ("openai-compat", "claude-cli", "codex-cli")
        if engine not in engines:
            raise ConfigError(
                f"JOB_HUNTER_L2_ENGINE must be one of {', '.join(engines)}: {engine}"
            )
        effort = e.get("JOB_HUNTER_L2_REASONING_EFFORT", "low")
        if effort not in ("low", "medium", "high", "max"):
            raise ConfigError(
                f"JOB_HUNTER_L2_REASONING_EFFORT must be low|medium|high|max: {effort}"
            )

        def _csv(name: str) -> tuple[str, ...] | None:
            raw = e.get(name)
            if raw is None:
                return None
            parts = tuple(p.strip() for p in raw.split(",") if p.strip())
            if not parts:
                raise ConfigError(f"{name} must not be empty when set")
            return parts

        candidates = _csv("JOB_HUNTER_L2_MODEL_CANDIDATES") or ()
        # default acceptance = exactly what was requested; a provider silently
        # rerouting then fails the observed-model gate unless the operator
        # explicitly widens JOB_HUNTER_L2_MODELS (e.g. to *)
        models = _csv("JOB_HUNTER_L2_MODELS")
        if models is None:
            models = candidates or ("*",)

        price_raw = e.get("JOB_HUNTER_L2_PRICE")
        l2_price: tuple[float, float] | None = None
        if price_raw:
            try:
                lo, hi = (float(x) for x in price_raw.split(","))
                l2_price = (lo, hi)
            except ValueError as ex:
                raise ConfigError(
                    f"JOB_HUNTER_L2_PRICE must be 'in_usd_per_mtok,out_usd_per_mtok': {price_raw!r}"
                ) from ex
        try:
            l2_max_docs = int(e.get("JOB_HUNTER_L2_MAX_DOCS", "300"))
            l2_max_usd = float(e.get("JOB_HUNTER_L2_MAX_USD", "5.0"))
        except ValueError as ex:
            raise ConfigError(f"JOB_HUNTER_L2_MAX_DOCS / _MAX_USD must be numeric: {ex}") from ex
        if l2_max_docs <= 0 or l2_max_usd < 0:
            raise ConfigError("JOB_HUNTER_L2_MAX_DOCS must be > 0 and _MAX_USD must be >= 0")
        if e.get("JOB_HUNTER_STATE_DIR"):
            state_dir = Path(e["JOB_HUNTER_STATE_DIR"])
        else:
            xdg_state = e.get("XDG_STATE_HOME") or str(
                Path(e.get("HOME", "~")).expanduser() / ".local/state"
            )
            state_dir = Path(xdg_state) / "job-hunter"
        return cls(
            archive_url=archive_url,
            registry_path=Path(e.get("JOB_HUNTER_REGISTRY", "companies.toml")),
            home=Path(e["JOB_HUNTER_HOME"]) if e.get("JOB_HUNTER_HOME") else home_default,
            database_url=e.get("JOB_HUNTER_DATABASE_URL") or None,
            state_dir=state_dir,
            drop_ratio=drop_ratio,
            ping_url=e.get("JOB_HUNTER_PING_URL") or None,
            mcp_token=e.get("JOB_HUNTER_MCP_TOKEN") or None,
            l2_engine=engine,
            l2_base_url=e.get("JOB_HUNTER_L2_BASE_URL") or None,
            l2_api_key=e.get("JOB_HUNTER_L2_API_KEY") or None,
            l2_models=models,
            l2_model_candidates=candidates,
            l2_max_docs=l2_max_docs,
            l2_max_usd=l2_max_usd,
            l2_price=l2_price,
            l2_reasoning_effort=effort,
            l2_trust_requested_model=e.get("JOB_HUNTER_L2_TRUST_REQUESTED_MODEL", "")
            .strip()
            .lower()
            in ("1", "true", "yes"),
        )
