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
        # parsing-rules/3: exact-unique re-anchor. The dominant live failure
        # class is a verbatim quote carrying a neighbouring block's id (section
        # headings cited against the statements under them). When the exact
        # text occurs in EXACTLY one block document-wide, the reference binds
        # there — deterministic, no normalization, no edit distance; anything
        # ambiguous (0 or 2+ blocks) still refuses. The record stores the
        # corrected block id: where the text actually is.
        if occurrence == 0:
            hits = [
                (b, found)
                for b in blocks.values()
                if (found := find_occurrences(b.text, text))
            ]
            if len(hits) == 1 and len(hits[0][1]) == 1:
                hit_block, found = hits[0]
                s = hit_block.span[0] + found[0]
                return {"block_id": hit_block.id, "text": text,
                        "span": [s, s + len(text)], "occurrence": 0}
        raise RefBindError(f"{block.id}: not a literal substring: {text[:80]!r}")
    if not 0 <= occurrence < len(starts):
        raise RefBindError(
            f"{block.id}: occurrence {occurrence} of {text[:80]!r}; block has {len(starts)}"
        )
    s = block.span[0] + starts[occurrence]
    return {"block_id": block.id, "text": text, "span": [s, s + len(text)],
            "occurrence": occurrence}
