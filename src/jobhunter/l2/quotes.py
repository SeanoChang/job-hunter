"""Quote objects and deterministic span resolution.

The LLM emits verbatim text (+ optional occurrence); code computes offsets
(harness spec §3.2). Offsets are Unicode codepoints, half-open [start, end).
No fuzzy repair, ever — a quote that does not appear exactly is a fabrication
signal, and any repair function would weaken exactly that signal.

`divergence` exists to keep that rule affordable. Rejecting without saying why
cost three attempts on one posting whose quotes differed from the document by a
single apostrophe; naming the offending character is a better error message,
not a repair, and the gate it feeds is still exact equality.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Quote:
    text: str
    span: tuple[int, int]
    occurrence: int


class QuoteNotFound(Exception):
    def __init__(self, text: str, longest_prefix: int) -> None:
        super().__init__(f"quote not found (longest matching prefix: {longest_prefix})")
        self.text = text
        self.longest_prefix = longest_prefix


class AmbiguousQuote(Exception):
    def __init__(self, text: str, starts: list[int]) -> None:
        super().__init__(f"quote occurs {len(starts)} times; occurrence index required")
        self.text = text
        self.starts = starts


def find_occurrences(md: str, text: str) -> list[int]:
    starts: list[int] = []
    i = md.find(text)
    while i != -1:
        starts.append(i)
        i = md.find(text, i + 1)
    return starts


def longest_matching_prefix(md: str, text: str) -> int:
    lo, hi = 0, len(text)
    while lo < hi:  # largest k such that text[:k] occurs in md; monotone, so bisect
        mid = (lo + hi + 1) // 2
        if text[:mid] in md:
            lo = mid
        else:
            hi = mid - 1
    return lo


@dataclass(frozen=True)
class Divergence:
    """Where an unmatched quote stops agreeing with the document.

    `emitted` is the character the model wrote at that point; `document` is
    what the document actually continues with, when the matching prefix pins a
    single place in the document (several occurrences leave it None rather than
    guess which one was meant).
    """

    prefix: int
    emitted: str | None
    document: str | None


def divergence(md: str, text: str, context: int = 24) -> Divergence:
    k = longest_matching_prefix(md, text)
    if k == len(text):
        return Divergence(prefix=k, emitted=None, document=None)  # no divergence to point at
    tails = {md[s + k : s + k + context] for s in find_occurrences(md, text[:k])} if k else set()
    return Divergence(
        prefix=k, emitted=text[k], document=tails.pop() if len(tails) == 1 else None
    )


def describe_not_found(md: str, text: str, clip: int = 80) -> str:
    """The reprompt-facing explanation of a failed quote."""
    d = divergence(md, text)
    msg = f"quote not found: {text[:clip]!r} — matches the document for {d.prefix} codepoints"
    if d.emitted is not None:
        msg += f", then you wrote {d.emitted!r}"
        if d.document is not None:
            msg += f" where the document continues {d.document!r}"
    return msg


def resolve_quote(md: str, text: str, occurrence: int | None = None) -> Quote:
    starts = find_occurrences(md, text)
    if not starts:
        raise QuoteNotFound(text, longest_matching_prefix(md, text))
    if len(starts) == 1:
        return Quote(text=text, span=(starts[0], starts[0] + len(text)), occurrence=0)
    if occurrence is None or not 0 <= occurrence < len(starts):
        raise AmbiguousQuote(text, starts)
    s = starts[occurrence]
    return Quote(text=text, span=(s, s + len(text)), occurrence=occurrence)


def occurrence_index(md: str, text: str, start: int) -> int:
    for k, s in enumerate(find_occurrences(md, text)):
        if s == start:
            return k
    return -1


def line_col(md: str, offset: int) -> tuple[int, int]:
    line = md.count("\n", 0, offset) + 1
    col = offset - (md.rfind("\n", 0, offset) + 1) + 1
    return line, col
