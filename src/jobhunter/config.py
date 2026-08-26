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
        return cls(
            archive_url=archive_url,
            registry_path=Path(e.get("JOB_HUNTER_REGISTRY", "companies.toml")),
            home=Path(e["JOB_HUNTER_HOME"]) if e.get("JOB_HUNTER_HOME") else home_default,
            database_url=e.get("JOB_HUNTER_DATABASE_URL") or None,
            drop_ratio=drop_ratio,
            ping_url=e.get("JOB_HUNTER_PING_URL") or None,
        )
