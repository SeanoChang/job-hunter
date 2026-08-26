"""Settings from the environment. Nothing else reads os.environ."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    archive_url: str
    registry_path: Path
    home: Path
    database_url: str | None
    drop_ratio: float = 0.5
    ping_url: str | None = None
    l2_engine: str = "openai-compat"
    l2_base_url: str | None = None
    l2_api_key: str | None = None
    l2_models: tuple[str, ...] = ("*",)
    l2_model_candidates: tuple[str, ...] = ()
    l2_max_docs: int = 300
    l2_max_usd: float = 5.0

    def require_l2(self) -> None:
        if self.l2_engine == "openai-compat" and (
            not self.l2_base_url or not self.l2_model_candidates
        ):
            raise ConfigError(
                "engine openai-compat needs JOB_HUNTER_L2_BASE_URL and "
                "JOB_HUNTER_L2_MODEL_CANDIDATES"
            )

    def require_database_url(self) -> str:
        if not self.database_url:
            raise ConfigError(
                "JOB_HUNTER_DATABASE_URL is required for this command (Postgres DSN)"
            )
        return self.database_url

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
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
        if engine not in ("openai-compat", "claude-cli"):
            raise ConfigError(f"JOB_HUNTER_L2_ENGINE must be openai-compat or claude-cli: {engine}")

        def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = e.get(name)
            if raw is None:
                return default
            parts = tuple(p.strip() for p in raw.split(",") if p.strip())
            return parts or default

        try:
            l2_max_docs = int(e.get("JOB_HUNTER_L2_MAX_DOCS", "300"))
            l2_max_usd = float(e.get("JOB_HUNTER_L2_MAX_USD", "5.0"))
        except ValueError as ex:
            raise ConfigError(f"JOB_HUNTER_L2_MAX_DOCS / _MAX_USD must be numeric: {ex}") from ex
        if l2_max_docs <= 0 or l2_max_usd < 0:
            raise ConfigError("JOB_HUNTER_L2_MAX_DOCS must be > 0 and _MAX_USD must be >= 0")
        return cls(
            archive_url=archive_url,
            registry_path=Path(e.get("JOB_HUNTER_REGISTRY", "companies.toml")),
            home=Path(e["JOB_HUNTER_HOME"]) if e.get("JOB_HUNTER_HOME") else home_default,
            database_url=e.get("JOB_HUNTER_DATABASE_URL") or None,
            drop_ratio=drop_ratio,
            ping_url=e.get("JOB_HUNTER_PING_URL") or None,
            l2_engine=engine,
            l2_base_url=e.get("JOB_HUNTER_L2_BASE_URL") or None,
            l2_api_key=e.get("JOB_HUNTER_L2_API_KEY") or None,
            l2_models=_csv("JOB_HUNTER_L2_MODELS", ("*",)),
            l2_model_candidates=_csv("JOB_HUNTER_L2_MODEL_CANDIDATES", ()),
            l2_max_docs=l2_max_docs,
            l2_max_usd=l2_max_usd,
        )
