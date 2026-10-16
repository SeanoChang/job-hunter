"""validator/26 (schema 4): the deterministic skill pass.

A vocabulary term standing in a qualification or responsibility quote, and not
already a model skill mention, becomes a skill mention marked
`origin: skill_pass`; verify re-derives the list and refuses any difference.

Quotes are real v15 statement evidence from the 2026-10-12 eval baseline
unless a test says it is synthetic.
"""

from __future__ import annotations

import copy
import json
from importlib import resources
from typing import Any

import pytest

from jobhunter.l2.v2 import facts, repair, serve
from jobhunter.l2.v2.skill_pass import (
    ORIGIN,
    grams,
    is_pass_mention,
    pass_mentions,
    term_key,
    vocabulary,
)
from jobhunter.l2.v2.source import annotate, blocks_by_id
from jobhunter.l2.v2.verify import verify
from tests.l2.v2.conftest import _settled, assemble4, s4_emit, s4_mention, s4_statement

SQL_R = ("- Experience using SQL and a scripting language such as Python or R to clean,"
         " explore, and analyze data through research, projects, or prior work.")
R_AND_D = ("This role requires interface with R&D, factory process engineering, Test"
           " Engineering and reliability engineering")
STACK = "Experience with our stack: React or Next.js, Python, Go, PostgreSQL, Redis, Docker, AWS."


def _record(lines: list[tuple[str, str]], mentions: list[dict[str, Any]] | None = None
            ) -> tuple[dict[str, Any], str]:
    """A schema-4 record with one whole-line statement per (kind, line)."""
    markdown = "## Qualifications\n" + "".join(f"{line}\n" for _, line in lines)
    statements = [s4_statement(f"s{i + 1}", kind, f"b{i + 2:06d}")
                  for i, (kind, _) in enumerate(lines)]
    accounting = [("b000001", []), *((f"b{i + 2:06d}", [f"s{i + 1}"]) for i in range(len(lines)))]
    emit = s4_emit(statements, accounting, mentions=mentions)
    return assemble4(emit, markdown), markdown


def _added(record: dict[str, Any]) -> list[str]:
    return [m["surface"] for m in record["mentions"] if is_pass_mention(m)]


def test_schema_4_is_judged_by_validator_26() -> None:
    assert facts.SCHEMA_4_VALIDATOR_VERSION == "26"
    assert facts.validator_version_for("4") == "26"
    assert facts.validator_version_for("3") == "20"


def test_the_packaged_vocabulary_is_sorted_and_obeys_its_own_rule() -> None:
    raw = resources.files("jobhunter.l2.v2").joinpath("skill_vocab.json").read_text("utf-8")
    data = json.loads(raw)
    rule, terms = data["rule"], data["terms"]
    assert list(terms) == sorted(terms)
    assert data["source"]["validator_version"] == "25"
    for term, (emitted, seen) in terms.items():
        assert seen >= rule["min_seen"] and emitted / seen >= rule["min_rate"], term
        assert term == term_key(term), term  # keys, in the pass's own normalization
    assert vocabulary() == frozenset(terms)
    assert {"python", "sql", "c++", "c#", "r", "node.js", "aws"} <= vocabulary()
    # generic words and ambiguous short ones fall out by the rate
    assert not {"teams", "data", "go", "c", "net"} & vocabulary()


@pytest.mark.parametrize(("text", "key"), [
    ("Node.js", "node.js"), ("Node.JS.", "node.js"), ("C++", "c++"), ("C#", "c#"),
    (".NET", "net"), ("Machine-Learning", "machine learning"), ("CI/CD", "ci/cd"),
    ("R&D", "r&d"), ("  Python  ", "python"),
])
def test_term_keys(text: str, key: str) -> None:
    assert term_key(text) == key


@pytest.mark.parametrize(("quote", "present", "absent"), [
    ("game development experience in either C++ or C#", {"c++", "c#"}, {"c"}),
    ("Experience with C, C++, or Python", {"c", "c++", "python"}, {"c python"}),
    (R_AND_D, {"r&d"}, {"r", "d"}),
    ("- Hands-on experience with R/C and/or autonomous flight vehicles", {"r/c"}, {"r"}),
    (SQL_R, {"sql", "python", "r"}, {"clean explore"}),
    (STACK, {"go", "next.js", "react"}, {"google"}),
    ("Google Cloud and Golang", {"google", "golang"}, {"go"}),
    # a term never spans a comma
    ("Python, SQL", {"python", "sql"}, {"python sql"}),
])
def test_matching_is_on_token_boundaries(quote: str, present: set[str], absent: set[str]
                                         ) -> None:
    found = grams(quote)
    assert present <= found
    assert not absent & found


def test_a_vocabulary_skill_the_model_left_out_is_added() -> None:
    record, markdown = _record(
        [("qualification", SQL_R)],
        mentions=[s4_mention("m1", "Python", "b000002", ["s1"], "skill")])
    assert _added(record) == ["SQL", "R"]
    sql = next(m for m in record["mentions"] if m["surface"] == "SQL")
    start = markdown.index("SQL")
    assert sql == {
        "id": "sp1", "surface": "SQL",
        "evidence": {"block_id": "b000002", "text": "SQL", "span": [start, start + 3],
                     "occurrence": 0},
        "statement_ids": ["s1"], "role": "direct", "normalized_key": "sql",
        "type": "skill", "origin": ORIGIN,
    }
    assert markdown[slice(*sql["evidence"]["span"])] == "SQL"
    report = verify(record, markdown, schema_version="4")
    assert report.status == "pass", report.findings


def test_what_the_model_already_emitted_is_not_added_again() -> None:
    # case and edge punctuation are the same key: "python" covers "Python"
    record, markdown = _record(
        [("qualification", SQL_R)],
        mentions=[s4_mention("m1", "python", "b000002", ["s1"], "skill"),
                  s4_mention("m2", "SQL", "b000002", ["s1"], "skill"),
                  s4_mention("m3", "R", "b000002", ["s1"], "skill")])
    assert _added(record) == []
    assert verify(record, markdown, schema_version="4").status == "pass"


def test_a_model_mention_of_another_type_does_not_block_a_skill() -> None:
    record, _ = _record([("qualification", SQL_R)],
                        mentions=[s4_mention("m1", "SQL", "b000002", ["s1"], "other")])
    assert "SQL" in _added(record)


def test_only_qualification_and_responsibility_quotes_are_read() -> None:
    record, _ = _record([("employer_context", SQL_R), ("hiring_policy", STACK)])
    assert _added(record) == []
    record, _ = _record([("responsibility", STACK)])
    assert _added(record) == ["React", "Next.js", "Python", "PostgreSQL", "Redis", "Docker",
                              "AWS"]


def test_r_and_d_is_not_r() -> None:
    record, _ = _record([("responsibility", R_AND_D)])
    assert "R" not in _added(record)


def test_synthetic_dotted_names_keep_their_own_spelling() -> None:
    # synthetic: no eval quote names .NET or Node.js. ".NET" keys as "net",
    # which the rate kept out of the vocabulary; a sentence-final dot is not
    # part of the surface
    record, _ = _record([("qualification", "Experience with .NET and Node.js.")])
    assert _added(record) == ["Node.js"]
    blocks = blocks_by_id(annotate("Use .NET daily.\n"))
    ref = {"block_id": "b000001", "text": "Use .NET daily.", "span": [0, 15], "occurrence": 0}
    statement = {"id": "s1", "kind": "qualification", "evidence": [ref]}
    out = pass_mentions([statement], [], blocks, vocab=frozenset({"net"}))
    assert [m["surface"] for m in out] == [".NET"]


def test_a_term_links_every_demand_statement_that_quotes_it() -> None:
    record, _ = _record([("qualification", SQL_R),
                         ("responsibility", "Write SQL queries for weekly reports.")])
    sql = next(m for m in record["mentions"] if m["surface"] == "SQL")
    assert sql["statement_ids"] == ["s1", "s2"]
    assert sql["evidence"]["block_id"] == "b000002"  # the first occurrence


def test_pass_ids_skip_the_models_ids() -> None:
    record, markdown = _record(
        [("qualification", SQL_R)],
        mentions=[s4_mention("sp1", "Python", "b000002", ["s1"], "skill")])
    assert [m["id"] for m in record["mentions"] if is_pass_mention(m)] == ["sp2", "sp3"]
    assert verify(record, markdown, schema_version="4").status == "pass"


def _codes(record: dict[str, Any], markdown: str) -> set[str]:
    report = verify(record, markdown, schema_version="4")
    return {f.code for f in report.findings if f.severity == "error"}


def test_verify_refuses_a_forged_or_edited_pass_mention() -> None:
    record, markdown = _record([("qualification", SQL_R)])
    assert _codes(record, markdown) == set()
    # "data" stands in the quote but is not a vocabulary term
    forged = copy.deepcopy(record)
    start = markdown.index("data")
    forged["mentions"].append({
        "id": "sp9", "surface": "data",
        "evidence": {"block_id": "b000002", "text": "data", "span": [start, start + 4],
                     "occurrence": 0},
        "statement_ids": ["s1"], "role": "direct", "normalized_key": "data",
        "type": "skill", "origin": ORIGIN})
    assert "skill_pass_mismatch" in _codes(forged, markdown)
    dropped = copy.deepcopy(record)
    dropped["mentions"] = [m for m in dropped["mentions"] if m["surface"] != "SQL"]
    assert "skill_pass_mismatch" in _codes(dropped, markdown)
    edited = copy.deepcopy(record)
    edited["mentions"][0]["role"] = "contextual"
    assert "skill_pass_mismatch" in _codes(edited, markdown)


def test_verify_refuses_a_model_mention_relabelled_as_the_pass() -> None:
    record, markdown = _record(
        [("qualification", "Proficiency with SQL")],
        mentions=[s4_mention("m1", "Proficiency", "b000002", ["s1"], "skill")])
    record["mentions"][0]["origin"] = ORIGIN
    assert "skill_pass_mismatch" in _codes(record, markdown)


def test_pass_mentions_serve_like_model_mentions() -> None:
    record, _ = _record([("qualification", SQL_R)],
                        mentions=[s4_mention("m1", "Python", "b000002", ["s1"], "skill")])
    settled = _settled(record, "needs_review")
    blob = serve.profile_of(settled)
    assert serve.summary(blob)["mentions"] == ["Python", "SQL", "R"]
    assert [row[0] for row in serve.mention_rows(settled)] == ["Python", "SQL", "R"]


def test_a_repair_rebuild_leaves_the_pass_to_assembly() -> None:
    record, _ = _record([("qualification", SQL_R)],
                        mentions=[s4_mention("m1", "Python", "b000002", ["s1"], "skill")])
    rebuilt = repair._rebuild(record, [])
    assert [m["surface"] for m in rebuilt["mentions"]] == ["Python"]
