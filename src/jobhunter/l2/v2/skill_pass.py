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
"data" 2%), and spoken languages are left out by ruling. After assembly binds
a schema-4 record, every vocabulary term found in such a quote and not
already a model mention becomes one more skill mention, quoted from the
document and linked to the statements that contain it. Verification derives
the same list again from the record and refuses any difference, so a pass
mention is as checked as a model one.

Matching is on tokens, never on substrings. A token is a run of word
characters and `+ # . / &`, so "c" never matches inside "c++" or "c#", "go"
never inside "google", "r" never inside "r&d", and "node.js" is one token. A
token's key is its lowercase text with the edge "." and "/" stripped, so
"Python." and "python" are one key; `term_key` applies that key to any
string. On top of the tokens:

- The words of a multi-word term may be separated only by whitespace,
  hyphens or apostrophes, so a term never spans a comma or a parenthesis.
- A match stands on real word boundaries in the source line: a hyphen,
  apostrophe, colon or emphasis mark that joins it to a letter or digit
  makes it part of a longer word ("X-ray" is not "ray", "AI-assisted" is not
  "ai", "Manage**r" is not "r").
- An all-caps token with a lowercase plural "s" ("SAs") is not the term its
  letters spell ("sas").
- Leftmost-longest: a quote is read left to right, and the longest term at a
  position consumes its words, so "generative AI" yields one term, never
  "ai" as well.
- The model's own mentions, of every type, win their words. Their keys take
  part in the longest match and block what they cover ("Claude Code" never
  yields "claude"), a term whose key is any model mention's key is not
  added, and neither is one whose span overlaps a model mention's evidence.

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
#: characters that join a match to the word beside it when a letter or digit
#: stands on their far side: hyphens, apostrophes, a colon ("erste:r") and
#: Markdown emphasis
_JOINERS = frozenset("-‐‑'’:*_")


def _key(token: str) -> str:
    """One token's key: lowercase, edge "." and "/" stripped (kept when the
    token is nothing else, so "/" stays "/")."""
    lowered = token.lower()
    return lowered.strip(_EDGE) or lowered


def term_key(text: str) -> str:
    """The vocabulary key of a name: its token keys joined by one space.

    A model mention's `normalized_key` and a vocabulary term compare on this,
    so "Node.js", "node.js" and "Node.JS." are one skill.
    """
    return " ".join(_key(m.group()) for m in _TOKEN.finditer(text))


def _joined(text: str, at: int, step: int) -> bool:
    """Does the character at `at` (just outside a match, walking `step`) join
    the match to a neighbouring word?"""
    i = at
    while 0 <= i < len(text) and text[i] in _JOINERS:
        i += step
    return 0 <= i < len(text) and text[i].isalnum()


def _acronym_plural(raw: str) -> bool:
    """"SAs": an all-caps token with a lowercase plural "s"."""
    word = raw.strip(_EDGE)
    return len(word) >= 3 and word.endswith("s") and word[:-1].isupper()


def _windows(text: str, lo: int = 0, hi: int | None = None) -> Iterator[tuple[str, int, int]]:
    """(key, start, end) of every candidate term in `text[lo:hi]`, by start
    then length: runs of 1..MAX_TOKENS tokens whose gaps hold only
    whitespace, hyphens or apostrophes, standing on word boundaries judged
    against the whole of `text` (the source line, for a quote)."""
    end_at = len(text) if hi is None else hi
    tokens = [(m.start(), m.end(), m.group()) for m in _TOKEN.finditer(text, lo, end_at)]
    for i, (start, _, _) in enumerate(tokens):
        if _joined(text, start - 1, -1):
            continue
        for j in range(i, min(i + MAX_TOKENS, len(tokens))):
            if j > i and not _GAP.fullmatch(text[tokens[j - 1][1]:tokens[j][0]]):
                break
            end, raw = tokens[j][1], tokens[j][2]
            if _joined(text, end, 1) or _acronym_plural(raw):
                continue
            yield " ".join(_key(t[2]) for t in tokens[i:j + 1]), start, end


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


def _longest(windows: Iterator[tuple[str, int, int]], wanted: set[str] | frozenset[str],
             blocking: set[str]) -> Iterator[tuple[str, int, int]]:
    """Leftmost-longest, non-overlapping: the longest window at each start
    whose key is a term or a model key, starting after the last one taken."""
    by_start: dict[int, tuple[int, str]] = {}
    for key, start, end in windows:
        if (key in wanted or key in blocking) and end > by_start.get(start, (-1, ""))[0]:
            by_start[start] = (end, key)
    taken_to = -1
    for start in sorted(by_start):
        if start >= taken_to:
            end, key = by_start[start]
            taken_to = end
            yield key, start, end


def pass_mentions(record: dict[str, Any], blocks: dict[str, Block],
                  vocab: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """The skill mentions the pass adds to a bound schema-4 record.

    Reads the record's statements and its MODEL mentions (pass mentions, if
    any, are ignored). A term is added once per record, in the order its
    first occurrence is read: statements in record order, each statement's
    evidence quotes in order, then position in the quote. Its evidence is
    that first occurrence, and its `statement_ids` every qualification or
    responsibility statement where it matched.

    The mention is shaped like a model's. `role` is `direct`: the term is
    named outright in a demand statement's own quote. `example` belongs to a
    member of an example set, a relation the pass never builds, and
    `contextual` says "named, not demanded", which is the opposite of what the
    vocabulary selected for. `normalized_key` follows aliases/1 like every
    other mention's (verify holds it to that), so a pass mention keys the same
    as the model mention it stands in for. Ids are `sp1`, `sp2`, ..., skipping
    every id the record already uses or links to, so they are unique and
    re-derivable and never complete a dangling link.
    """
    terms = vocabulary() if vocab is None else vocab
    model = [m for m in record.get("mentions") or [] if not is_pass_mention(m)]
    model_keys = {term_key(str(m.get("normalized_key") or m.get("surface") or ""))
                  for m in model}
    model_spans = [(int(m["evidence"]["span"][0]), int(m["evidence"]["span"][1]))
                   for m in model if isinstance(m.get("evidence"), dict)]
    found: dict[str, dict[str, Any]] = {}
    for statement in record.get("statements") or []:
        if statement.get("kind") not in QUOTED_KINDS:
            continue
        for ref in statement.get("evidence") or []:
            # an unknown block or a span off its block is attribution's
            # finding, never a crash here
            block = blocks.get(str(ref["block_id"]))
            lo = int(ref["span"][0]) - block.span[0] if block is not None else -1
            if block is None or block.text[lo:lo + len(str(ref["text"]))] != ref["text"]:
                continue
            line = block.text
            for key, start, end in _longest(_windows(line, lo, lo + len(str(ref["text"]))),
                                            terms, model_keys):
                if key in model_keys:
                    continue  # the model's words, of whatever type
                first, last = _trim(line, start, end)
                at = block.span[0] + first
                if any(a < at + last - first and at < b for a, b in model_spans):
                    continue
                hit = found.get(key)
                if hit is None:
                    surface = line[first:last]
                    if len(surface) > _MAX_SURFACE:
                        continue
                    hit = found[key] = {"surface": surface, "statement_ids": [], "evidence": {
                        "block_id": block.id, "text": surface, "span": [at, at + len(surface)],
                        "occurrence": occurrence_index(line, surface, first),
                    }}
                if statement["id"] not in hit["statement_ids"]:
                    hit["statement_ids"].append(statement["id"])
    relations = record.get("relations") or {}
    linked = [ref_id for node in relations.get("example_sets") or []
              for ref_id in node.get("mention_ids") or []]
    tracks = relations.get("tracks")
    if isinstance(tracks, dict):
        linked += [ref_id for item in tracks.get("items") or []
                   for ref_id in item.get("mention_ids") or []]
    taken = {m.get("id") for m in model} | set(linked)
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
