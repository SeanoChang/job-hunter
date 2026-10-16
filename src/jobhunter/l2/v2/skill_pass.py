"""validator/26 (schema 4): the deterministic skill pass.

The model misses skills that stand inside its own quoted qualification and
responsibility statements, and which ones it names varies from run to run. On
the 2026-10-12 eval baseline (50 reviewed postings, k=3) skill recall was
0.43/0.49 (train/test) and the pairwise stability of the skill set 0.56.

The pass closes part of that gap without a model call. A frozen corpus
vocabulary (`skill_vocab.json`, built by `scripts/build_skill_vocab.py`) lists
the terms the model itself calls a skill in at least 40% of the documents
where the term stands in a qualification or responsibility quote, and that
it saw at least 30 times. Generic words fall out by that rate ("teams" 0%,
"data" 2%). After assembly binds a schema-4 record, every vocabulary term
found in such a quote and not already a skill mention becomes one more skill
mention, quoted from the document and linked to the statements that contain
it. Verification re-derives the same list from the record and refuses any
difference, so a pass mention is as checked as a model one.

Matching is on tokens, never on substrings: a token is a run of word
characters and `+ # . / &`, so "c" never matches inside "c++" or "c#", "go"
never inside "google", "r" never inside "r&d", and "node.js" is one token.
The words of a multi-word term may be separated only by whitespace, hyphens
or apostrophes, so a term never spans a comma or a parenthesis. A token's key
is its lowercase text with the edge "." and "/" stripped, so "Python." and
"python" are one key. `term_key` applies that key to any string; the
vocabulary build and the duplicate check against model mentions both use it.

Pure: no I/O apart from reading the packaged vocabulary once.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from functools import cache
from importlib import resources
from typing import Any

from jobhunter.l2.quotes import occurrence_index
from jobhunter.l2.v2.types import Block

#: the vocabulary file, packaged beside this module
VOCAB_FILE = "skill_vocab.json"
#: the marker a pass mention carries in its `origin` field; a model mention
#: has no `origin` key at all
ORIGIN = "skill_pass"
#: the statement kinds whose quotes the pass reads (and the build counts)
QUOTED_KINDS = frozenset({"qualification", "responsibility"})
#: the longest term, in tokens
MAX_TOKENS = 5
#: the schema's cap on a mention surface
_MAX_SURFACE = 120

_TOKEN = re.compile(r"[\w+#./&]+")
#: what may stand between two words of one term
_GAP = re.compile(r"[\s\-‐‑–—'’]+")
_EDGE = "./"


def _key(token: str) -> str:
    """One token's key: lowercase, edge "." and "/" stripped (kept when the
    token is nothing else, so "/" stays "/")."""
    lowered = token.lower()
    return lowered.strip(_EDGE) or lowered


def _tokens(text: str) -> list[tuple[int, int, str]]:
    """(start, end, key) for every token of `text`, in order."""
    return [(m.start(), m.end(), _key(m.group())) for m in _TOKEN.finditer(text)]


def term_key(text: str) -> str:
    """The vocabulary key of a name: its token keys joined by one space.

    A model mention's `normalized_key` and a vocabulary term compare on this,
    so "Node.js", "node.js" and "Node.JS." are one skill.
    """
    return " ".join(key for _, _, key in _tokens(text))


def _windows(text: str) -> Iterator[tuple[str, int, int]]:
    """(key, start, end) of every run of 1..MAX_TOKENS tokens whose gaps hold
    only whitespace, hyphens or apostrophes, by start then length."""
    tokens = _tokens(text)
    for i in range(len(tokens)):
        for n in range(1, MAX_TOKENS + 1):
            j = i + n - 1
            if j >= len(tokens):
                break
            if n > 1 and not _GAP.fullmatch(text[tokens[j - 1][1]:tokens[j][0]]):
                break
            yield " ".join(t[2] for t in tokens[i:j + 1]), tokens[i][0], tokens[j][1]


def grams(text: str) -> set[str]:
    """Every term key `text` contains. The vocabulary build counts a term as
    seen in a document through exactly this function."""
    return {key for key, _, _ in _windows(text)}


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    """The window without the edge characters its key drops: a trailing "."
    or "/" ("Python." is "Python") and a leading one, except a single leading
    dot before a letter or digit (".NET" keeps its dot)."""
    while end - start > 1 and text[end - 1] in _EDGE:
        end -= 1
    lead = start
    while lead < end - 1 and text[lead] in _EDGE:
        lead += 1
    if lead - start == 1 and text[start] == "." and text[lead].isalnum():
        lead = start
    return lead, end


@cache
def vocabulary() -> frozenset[str]:
    """The frozen vocabulary's terms."""
    raw = resources.files("jobhunter.l2.v2").joinpath(VOCAB_FILE).read_text("utf-8")
    return frozenset(json.loads(raw)["terms"])


def is_pass_mention(mention: Any) -> bool:
    return isinstance(mention, dict) and mention.get("origin") == ORIGIN


def pass_mentions(statements: list[dict[str, Any]], mentions: list[dict[str, Any]],
                  blocks: dict[str, Block],
                  vocab: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """The skill mentions the pass adds to a bound schema-4 record.

    `mentions` are the record's model mentions (pass mentions, if any, are
    ignored). A term is added once per record, in the order its first
    occurrence is read: statements in record order, each statement's
    evidence quotes in order, then position in the quote. Its evidence is
    that first occurrence, and its `statement_ids` every qualification or
    responsibility statement whose quotes contain the term.

    The mention is shaped like a model's. `role` is `direct`: the term is
    named outright in a demand statement's own quote. `example` belongs to a
    member of an example set, a relation the pass never builds, and
    `contextual` says "named, not demanded", which is the opposite of what the
    vocabulary selected for. `normalized_key` follows aliases/1 like every
    other mention's (verify holds it to that), so a pass mention keys the same
    as the model mention it stands in for. Ids are `sp1`, `sp2`, ..., skipping
    any id the model used, so they are unique and re-derivable.
    """
    terms = vocabulary() if vocab is None else vocab
    model = [m for m in mentions if not is_pass_mention(m)]
    present = {term_key(str(m.get("normalized_key") or m.get("surface") or ""))
               for m in model if m.get("type") == "skill"}
    found: dict[str, dict[str, Any]] = {}
    for statement in statements:
        if statement.get("kind") not in QUOTED_KINDS:
            continue
        for ref in statement.get("evidence") or []:
            quote = str(ref["text"])
            for key, start, end in _windows(quote):
                if key not in terms or key in present:
                    continue
                hit = found.get(key)
                if hit is None:
                    lo, hi = _trim(quote, start, end)
                    surface = quote[lo:hi]
                    # an unknown block is attribution's finding, not a crash here
                    block = blocks.get(str(ref["block_id"]))
                    if block is None or len(surface) > _MAX_SURFACE:
                        continue
                    at = int(ref["span"][0]) + lo
                    hit = found[key] = {"surface": surface, "statement_ids": [], "evidence": {
                        "block_id": block.id, "text": surface, "span": [at, at + len(surface)],
                        "occurrence": occurrence_index(block.text, surface, at - block.span[0]),
                    }}
                if statement["id"] not in hit["statement_ids"]:
                    hit["statement_ids"].append(statement["id"])
    taken = {m.get("id") for m in model}
    ids = (f"sp{n}" for n in range(1, len(found) + len(taken) + 2) if f"sp{n}" not in taken)
    return [
        {
            "id": next(ids),
            "surface": hit["surface"],
            "evidence": hit["evidence"],
            "statement_ids": hit["statement_ids"],
            "role": "direct",
            # aliases/1, `assemble.normalize_key` (assemble imports this module)
            "normalized_key": hit["surface"].casefold().strip(),
            "type": "skill",
            "origin": ORIGIN,
        }
        for hit in found.values()
    ]
