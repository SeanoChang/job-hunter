"""companies.toml -> validated Board list + revision hash."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobhunter.hashing import canonical_json, sha256_hex
from jobhunter.models import Board

VALID_SOURCES = frozenset({"greenhouse", "lever", "ashby"})
_BOARD_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class RegistryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Registry:
    boards: tuple[Board, ...]
    revision: str

    def snapshot_json(self) -> bytes:
        return _snapshot(self.boards)


def _snapshot(boards: tuple[Board, ...]) -> bytes:
    rows = [
        {
            "board": b.board,
            "company": b.company,
            "country": b.country,
            "source": b.source,
            "tags": list(b.tags),
        }
        for b in boards
    ]
    return canonical_json(rows)


def _board(entry: Any, i: int) -> Board:
    if not isinstance(entry, dict):
        raise RegistryError(f"boards[{i}]: expected a table")
    company = entry.get("company")
    source = entry.get("source")
    board = entry.get("board")
    if not isinstance(company, str) or not company.strip():
        raise RegistryError(f"boards[{i}]: company must be a non-empty string")
    if source not in VALID_SOURCES:
        raise RegistryError(f"boards[{i}]: unknown source {source!r}")
    if not isinstance(board, str) or not _BOARD_RE.match(board):
        raise RegistryError(f"boards[{i}]: board must match {_BOARD_RE.pattern}")
    country = entry.get("country")
    if country is not None and not isinstance(country, str):
        raise RegistryError(f"boards[{i}]: country must be a string")
    tags = entry.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise RegistryError(f"boards[{i}]: tags must be a list of strings")
    return Board(company=company.strip(), source=source, board=board, country=country,
                 tags=tuple(tags))


def load(path: Path) -> Registry:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw = data.get("boards")
    if not isinstance(raw, list):
        raise RegistryError("boards must be an array of tables")
    boards = [_board(e, i) for i, e in enumerate(raw)]
    seen: set[str] = set()
    for b in boards:
        if b.key in seen:
            raise RegistryError(f"duplicate board {b.key}")
        seen.add(b.key)
    ordered = tuple(sorted(boards, key=lambda b: (b.source, b.board)))
    return Registry(boards=ordered, revision=sha256_hex(_snapshot(ordered)))
