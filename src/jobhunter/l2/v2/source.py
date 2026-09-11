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


# parsing-rules/5: a 1:1 typographic equivalence table. Models routinely
# normalize apostrophes, quotes, dashes and spaces when quoting; the DOCUMENT
# text is truth, so matching happens on the folded forms and the bound span
# stores the document's own bytes. Every mapping is same-length, so indexes
# map 1:1 back to the original block.
_TYPO_TRANS = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2012": "-",
    "\u00a0": " ", "\u2009": " ", "\u202f": " ",
})


def _typo(s: str) -> str:
    return s.translate(_TYPO_TRANS)


def _unique_global(blocks: dict[str, Block], text: str,
                   fold: bool) -> dict[str, Any] | None:
    """The one place `text` occurs document-wide under the given folding, or
    None when it occurs nowhere or ambiguously. Bound text is the document's."""
    needle = _typo(text).casefold() if fold else _typo(text)
    hits: list[tuple[Block, list[int]]] = []
    for b in blocks.values():
        hay = _typo(b.text).casefold() if fold else _typo(b.text)
        found = find_occurrences(hay, needle)
        if found:
            hits.append((b, found))
    if len(hits) != 1 or len(hits[0][1]) != 1:
        return None
    block, found = hits[0]
    start = found[0]
    return {"block_id": block.id, "text": block.text[start:start + len(text)],
            "span": [block.span[0] + start, block.span[0] + start + len(text)],
            "occurrence": 0}


def _first_global(blocks: dict[str, Block], text: str) -> dict[str, Any] | None:
    """First typographic+casefold occurrence in document order — the lenient
    tier reserved for mention evidence, where grounding needs existence, not
    a unique context."""
    needle = _typo(text).casefold()
    for b in sorted(blocks.values(), key=lambda x: x.span[0]):
        found = find_occurrences(_typo(b.text).casefold(), needle)
        if found:
            start = found[0]
            return {"block_id": b.id,
                    "text": b.text[start:start + len(text)],
                    "span": [b.span[0] + start, b.span[0] + start + len(text)],
                    "occurrence": 0}
    return None


def resolve(ref: dict[str, Any], blocks: dict[str, Block],
            *, lenient: bool = False) -> dict[str, Any]:
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
        # parsing-rules/3 → /5 tiers, all deterministic and unique-or-refuse:
        # exact-unique global, typographic-fold in the cited block, typographic
        # unique global; for mention evidence (lenient) a final first-match
        # typographic+casefold tier — grounding needs existence, not context.
        if occurrence == 0:
            bound = _unique_global(blocks, text, fold=False)
            if bound is not None:
                return bound
            folded = find_occurrences(_typo(block.text), _typo(text))
            if len(folded) == 1:
                start = folded[0]
                return {"block_id": block.id,
                        "text": block.text[start:start + len(text)],
                        "span": [block.span[0] + start,
                                 block.span[0] + start + len(text)],
                        "occurrence": 0}
            bound = _unique_global(blocks, text, fold=True)
            if bound is not None:
                return bound
            if lenient:
                bound = _first_global(blocks, text)
                if bound is not None:
                    return bound
        raise RefBindError(f"{block.id}: not a literal substring: {text[:80]!r}")
    if not 0 <= occurrence < len(starts):
        raise RefBindError(
            f"{block.id}: occurrence {occurrence} of {text[:80]!r}; block has {len(starts)}"
        )
    s = block.span[0] + starts[occurrence]
    return {"block_id": block.id, "text": text, "span": [s, s + len(text)],
            "occurrence": occurrence}
