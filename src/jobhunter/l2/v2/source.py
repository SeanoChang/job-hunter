"""Source annotation (blocks/1) and evidence reference binding.

Code assigns block ids over the canonical markdown; the model only ever names
them. Binding is exact: unknown blocks, non-literal substrings, impossible
occurrences, and half-null text/occurrence pairs raise — no fuzzy repair, ever
(same rule as v1 quotes.py, for the same reason: repair is the hole every
fabricated quote would walk through).
"""

from __future__ import annotations

from typing import Any

from jobhunter.l2.quotes import find_occurrences
from jobhunter.l2.v2.types import Block

ANNOTATION_VERSION = "blocks/1"


class RefBindError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def annotate(markdown: str) -> list[Block]:
    blocks: list[Block] = []
    pos = 0
    n = 0
    for line in markdown.split("\n"):
        if line.strip():
            n += 1
            blocks.append(Block(id=f"b{n:06d}", text=line, span=(pos, pos + len(line))))
        pos += len(line) + 1  # the split-away newline
    return blocks


def blocks_by_id(blocks: list[Block]) -> dict[str, Block]:
    return {b.id: b for b in blocks}


def resolve(ref: dict[str, Any], blocks: dict[str, Block]) -> dict[str, Any]:
    block = blocks.get(ref.get("block_id", ""))
    if block is None:
        raise RefBindError(f"unknown block: {ref.get('block_id')!r}")
    text, occurrence = ref.get("text"), ref.get("occurrence")
    if text is None and occurrence is None:
        return {"block_id": block.id, "text": block.text,
                "span": [block.span[0], block.span[1]], "occurrence": 0}
    if text is None or occurrence is None:
        raise RefBindError(
            f"{block.id}: text and occurrence must both be null (whole block) or both set"
        )
    starts = find_occurrences(block.text, text)
    if not starts:
        raise RefBindError(f"{block.id}: not a literal substring: {text[:80]!r}")
    if not 0 <= occurrence < len(starts):
        raise RefBindError(
            f"{block.id}: occurrence {occurrence} of {text[:80]!r}; block has {len(starts)}"
        )
    s = block.span[0] + starts[occurrence]
    return {"block_id": block.id, "text": text, "span": [s, s + len(text)],
            "occurrence": occurrence}
