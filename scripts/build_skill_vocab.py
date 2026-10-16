"""Rebuild validator/26's frozen skill vocabulary from the store. Read-only.

    uv run python scripts/build_skill_vocab.py [--out src/jobhunter/l2/v2/skill_vocab.json]

The rule (`l2/v2/skill_pass.py` says why):

1. Candidates: every skill key (`skill_pass.term_key` of a skill mention's
   `normalized_key`, else its surface) the model emitted in at least
   MIN_EMITTED documents, over the validated extractions of one engine tuple,
   minus the parse eval's documents (`evals/parse/cases.jsonl`), so the eval
   that measures the pass never fed it.
2. For each candidate: `seen` = documents where the term stands in a
   qualification or responsibility statement quote (`skill_pass.grams`, the
   pass's own matcher); `emitted` = of those, documents where the model also
   emitted it as a skill.
3. Kept: seen >= MIN_SEEN and emitted / seen >= MIN_RATE, and not a spoken
   language (LANGUAGES, an explicit list: languages are not skills, by ruling)
   or an ordinary word that is rarely a tool in a demand quote (STOPLIST).

Only model mentions count. A mention the pass added (`origin: skill_pass`) is
skipped everywhere, so a vocabulary rebuilt over validator/26 records cannot
feed on its own output. The file records its inputs beside the terms.
"""

from __future__ import annotations

import argparse
import collections
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg

from jobhunter.config import Settings, env_snapshot
from jobhunter.l2.v2.skill_pass import (
    MAX_TOKENS,
    QUOTED_KINDS,
    grams,
    is_pass_mention,
    term_key,
)
from jobhunter.timeutil import utcnow

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src/jobhunter/l2/v2/skill_vocab.json"
CASES = ROOT / "evals/parse/cases.jsonl"
PROMPT_VERSION = "demand-profile/v15"
VALIDATOR_VERSION = "25"
STATUS = "validated"
MIN_EMITTED = 20
MIN_SEEN = 30
MIN_RATE = 0.4
#: spoken languages are not skills (owner ruling, 2026-10-16): never kept,
#: whatever their rate. Keys in `term_key` form.
LANGUAGES = frozenset({
    "afrikaans", "albanian", "amharic", "arabic", "armenian", "bahasa", "bahasa indonesia",
    "bahasa malaysia", "bengali", "bulgarian", "burmese", "cantonese", "catalan",
    "chinese", "croatian", "czech", "danish", "dutch", "english", "estonian", "farsi",
    "filipino", "finnish", "flemish", "french", "german", "greek", "gujarati",
    "hebrew", "hindi", "hungarian", "icelandic", "indonesian", "irish", "italian",
    "japanese", "kannada", "kazakh", "khmer", "korean", "lao", "latvian",
    "lithuanian", "malay", "malayalam", "mandarin", "mandarin chinese", "marathi",
    "mongolian", "nepali", "norwegian", "persian", "polish", "portuguese", "punjabi",
    "romanian", "russian", "serbian", "sinhala", "slovak", "slovenian", "somali",
    "spanish", "swahili", "swedish", "tagalog", "tamil", "telugu", "thai",
    "turkish", "ukrainian", "urdu", "uzbek", "vietnamese", "welsh", "zulu",
    "english language", "business english", "fluent english", "written english",
    "spoken english", "english proficiency", "bilingual", "multilingual",
    "영어", "英語", "英语", "中文", "日本語", "한국어", "inglés", "anglais", "englisch",
    "français", "deutsch", "español", "português", "italiano",
})
#: ordinary words that are rarely a tool in a demand quote, never kept. The
#: first ten from the 2026-10-16 replay of 1,500 v15/25 records, which found
#: go a tool in 1 of 43 additions, excel 5/24, windows 1/6, swift 1/3,
#: outlook 1/4 and spark, shell, glue, word, jest 0 of 7 ("go live", "you excel
#: at", "process windows"). The rest from a scan of the 1,611-term vocabulary:
#: words whose everyday sense ("spin up", "rally the team", "glean insights")
#: or non-skill sense (a chef, a foundry, HVAC airflow, Meta the employer) is
#: common in postings.
STOPLIST = frozenset({
    "go", "excel", "windows", "swift", "outlook", "spark", "shell", "glue", "word",
    "jest",
    "airflow", "amplitude", "atlas", "bedrock", "challenger", "chef", "chrome", "clay",
    "dash", "eagle", "far", "foundry", "glean", "heap", "ironclad", "iterable",
    "lattice", "meta", "notion", "omni", "pit", "rally", "sage", "salt", "sat",
    "shock", "spin", "splash", "tempo", "temporal", "unity", "var", "x",
})

_SQL = ("SELECT document_hash, profile FROM extractions"
        " WHERE prompt_version = %s AND validator_version = %s AND status = %s")


def emitted_keys(profile: dict[str, Any]) -> set[str]:
    """The skill keys the MODEL emitted in one profile."""
    return {term_key(str(m.get("normalized_key") or m.get("surface") or ""))
            for m in profile.get("mentions") or []
            if m.get("type") == "skill" and not is_pass_mention(m)}


def seen_keys(profile: dict[str, Any]) -> set[str]:
    """Every term key standing in a qualification or responsibility quote."""
    seen: set[str] = set()
    for statement in profile.get("statements") or []:
        if statement.get("kind") in QUOTED_KINDS:
            for ref in statement.get("evidence") or []:
                seen |= grams(str(ref.get("text") or ""))
    return seen


def _profiles(conn: psycopg.Connection[Any], skip: set[str]) -> Iterator[dict[str, Any]]:
    with conn.cursor(name="skill_vocab") as cur:
        cur.itersize = 2000
        cur.execute(_SQL, (PROMPT_VERSION, VALIDATOR_VERSION, STATUS))
        for document_hash, profile in cur:
            if document_hash not in skip and profile:
                yield profile


def _dump(meta: dict[str, Any], terms: dict[str, list[int]]) -> str:
    """Sorted JSON, one term per line, so a rebuild diffs term by term."""
    lines = [f"  {json.dumps(t, ensure_ascii=False)}: {json.dumps(terms[t])}"
             for t in sorted(terms)]
    head = json.dumps(meta, indent=2, ensure_ascii=False)
    return head[:-2] + ',\n  "terms": {\n  ' + ",\n  ".join(lines) + "\n  }\n}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    skip = {json.loads(line)["document_hash"]
            for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()}
    emitted_docs: collections.Counter[str] = collections.Counter()
    seen: collections.Counter[str] = collections.Counter()
    emitted: collections.Counter[str] = collections.Counter()
    documents = 0
    with psycopg.connect(Settings.load(env_snapshot()).require_database_url()) as conn:
        conn.read_only = True
        for profile in _profiles(conn, skip):
            documents += 1
            emitted_docs.update(emitted_keys(profile))
        candidates = {t for t, n in emitted_docs.items() if n >= MIN_EMITTED}
        for profile in _profiles(conn, skip):
            mine = emitted_keys(profile)
            for term in candidates & seen_keys(profile):
                seen[term] += 1
                emitted[term] += term in mine
    passing = {t: [emitted[t], seen[t]] for t in candidates
               if seen[t] >= MIN_SEEN and emitted[t] / seen[t] >= MIN_RATE}
    kept = {t: n for t, n in passing.items() if t not in LANGUAGES | STOPLIST}
    meta = {
        "vocab_version": "skill-vocab/1",
        "built_at": utcnow().date().isoformat(),
        "source": {"prompt_version": PROMPT_VERSION, "validator_version": VALIDATOR_VERSION,
                   "status": STATUS, "documents": documents,
                   "excluded": "evals/parse/cases.jsonl", "excluded_documents": len(skip)},
        "rule": {"min_emitted_documents": MIN_EMITTED, "min_seen": MIN_SEEN,
                 "min_rate": MIN_RATE, "max_tokens": MAX_TOKENS,
                 "statement_kinds": sorted(QUOTED_KINDS), "model_mentions_only": True,
                 "excluded_terms": "spoken languages (LANGUAGES), ordinary words (STOPLIST)",
                 "matching": "skill_pass.grams: word boundaries, no comma-spanning terms"},
        "candidates": len(candidates),
        "languages_dropped": sorted(set(passing) & LANGUAGES),
        "stoplist_dropped": sorted(set(passing) & STOPLIST),
        "term_format": "term: [emitted, seen] documents",
    }
    args.out.write_text(_dump(meta, kept), encoding="utf-8")
    print(json.dumps({"documents": documents, "candidates": len(candidates),
                      "kept": len(kept), "out": str(args.out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
