"""UTC helpers. Every timestamp in the system is tz-aware UTC."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def from_epoch_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)
