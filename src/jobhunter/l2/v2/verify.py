"""The v2 verifier: one pure function over (schema-2 record, canonical markdown).

Deterministic only — attribution, reference integrity, re-derivation, block
accounting, usability coherence. Whether the record *understood* the posting is
the increment-2 auditor's question; nothing here calls a model, reads a file, or
guesses. Frozen together with `facts.py` under VALIDATOR_VERSION: a check that
changes verdicts bumps the identifier rather than editing this one in place.

Fail-fast order is load-bearing (the v1 lesson): wrong document, then unknown
annotation, then schema. Span and reference checks over unvalidated structure
raise KeyError instead of reporting, so they never run before the schema passes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from jobhunter.hashing import sha256_hex
from jobhunter.l2.quotes import occurrence_index
from jobhunter.l2.report import Report
from jobhunter.l2.schemas import validate_record

# assembly's own derivation, imported rather than re-implemented: one grammar and
# one state mapping, so "what assembly wrote" and "what verification recomputes"
# cannot drift into disagreeing about the same evidence.
from jobhunter.l2.v2.assemble import _derive as _rederive
from jobhunter.l2.v2.assemble import normalize_key
from jobhunter.l2.v2.facts import VALIDATOR_VERSION
from jobhunter.l2.v2.source import ANNOTATION_VERSION, annotate, blocks_by_id
from jobhunter.l2.v2.types import IMPORTANCE_KINDS, Block

SCHEMA_VERSION = "2"
MAX_GROUP_DEPTH = 5

# importance states that assert something the document must have said out loud;
# `unstated` is the one reading that may stand bare
_EVIDENCED_IMPORTANCE = frozenset({"required", "preferred", "not_required", "ambiguous"})

_DISPOSITIONS_NEEDING_REFS = frozenset({"statements", "facts"})

# The C02/C07 tripwire: an excluded block whose text still speaks in obligations.
# v1 made this class invisible — boilerplate exclusion removed English-proficiency
# footers from every downstream check, so nothing could report the omission. A
# warning, not an error: EEO text legitimately says "must", and the auditor, not
# this module, decides whether a real requirement was thrown away.
_REQUIREMENT_LANGUAGE = re.compile(
    r"(?i)\b(must|required?|minimum|at least|only candidates|need to|proficien\w*|fluen\w*)\b"
)

# presence family key (record) for each fact-entry family
_PRESENCE_KEY = {"experience": "experience", "compensation": "compensation",
                 "quantity": "quantities", "date": "dates"}
_PRESENCE_NEEDING_EVIDENCE = frozenset({"explicitly_absent", "unresolved"})


def iter_bound_refs(record: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (path, bound_ref) for every reference position in schema 2.

    One traversal, so a new evidence field cannot quietly escape attribution:
    forgetting to list it here is the only way a span goes unchecked. Assumes a
    schema-valid record (the caller fail-fasts before this runs).
    """

    def many(path: str, refs: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        for i, ref in enumerate(refs or []):
            yield f"{path}[{i}]", ref

    yield from many("source_assessment.evidence", record["source_assessment"]["evidence"])
    for i, statement in enumerate(record["statements"]):
        path = f"statements[{i}]"
        for key in ("evidence", "importance_evidence", "polarity_evidence",
                    "proficiency_evidence"):
            yield from many(f"{path}.{key}", statement[key])
        for j, issue in enumerate(statement["unresolved"]):
            yield from many(f"{path}.unresolved[{j}].evidence", issue["evidence"])
    relations = record["relations"]
    for kind in ("groups", "conditions", "example_sets"):
        for i, node in enumerate(relations[kind]):
            yield from many(f"relations.{kind}[{i}].evidence", node["evidence"])
    for family, presence in record["facts"]["presence"].items():
        yield from many(f"facts.presence.{family}.evidence", presence["evidence"])
    for i, entry in enumerate(record["facts"]["entries"]):
        path = f"facts.entries[{i}]"
        for aspect, refs in entry["evidence"].items():
            yield from many(f"{path}.evidence.{aspect}", refs)
        if entry["scope"] is not None:
            yield from many(f"{path}.scope.evidence", entry["scope"]["evidence"])
    for i, mention in enumerate(record["mentions"]):
        yield f"mentions[{i}].evidence", mention["evidence"]  # a single ref, never a list
    for i, area in enumerate(record["areas"]):
        yield from many(f"areas[{i}].evidence", area["evidence"])
    for i, entry in enumerate(record["block_accounting"]):
        yield from many(f"block_accounting[{i}].evidence", entry["evidence"])


def _check_attribution(
    record: dict[str, Any], md: str, blocks: dict[str, Block], report: Report
) -> None:
    """Every reference is exact: the span says what it quotes, sits inside the
    block it names, and names its own occurrence within that block."""
    n = len(md)
    for path, ref in iter_bound_refs(record):
        start, end = ref["span"][0], ref["span"][1]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= n:
            # draft 2020-12 "integer" accepts 5.0; slicing with it would crash
            report.error("attribution", path, "span_bounds", span=[start, end], doc_len=n)
            continue
        text = ref["text"]
        if md[start:end] != text:
            report.error("attribution", path, "text_mismatch",
                         expected=text, found=md[start:end], span=[start, end])
            continue
        block = blocks.get(ref["block_id"])
        if block is None or not (block.span[0] <= start and end <= block.span[1]):
            report.error("attribution", path, "outside_block",
                         block_id=ref["block_id"], span=[start, end],
                         block_span=list(block.span) if block else None)
            continue
        found = occurrence_index(block.text, text, start - block.span[0])
        if found != ref["occurrence"]:
            report.error("attribution", path, "occurrence_mismatch",
                         stated=ref["occurrence"], found=found, span=[start, end])


def _check_references(record: dict[str, Any], report: Report) -> None:
    """Ids are unique in their namespace and every reference resolves to an
    object of the right kind — a dangling id is a broken record, not a hint."""
    namespaces: dict[str, list[dict[str, Any]]] = {
        "statements": record["statements"],
        "relations.groups": record["relations"]["groups"],
        "relations.conditions": record["relations"]["conditions"],
        "relations.example_sets": record["relations"]["example_sets"],
        "facts.entries": record["facts"]["entries"],
        "mentions": record["mentions"],
        "areas": record["areas"],
    }
    known: dict[str, set[str]] = {}
    for path, items in namespaces.items():
        ids = [item["id"] for item in items]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            report.error("references", path, "duplicate_id", ids=duplicates)
        known[path] = set(ids)

    def resolves(path: str, ids: list[str], expected: str, valid: set[str]) -> None:
        for ref_id in ids:
            if ref_id not in valid:
                report.error("references", path, "unknown_reference",
                             ref_id=ref_id, expected=expected)

    statements, conditions = known["statements"], known["relations.conditions"]
    entries, groups = known["facts.entries"], known["relations.groups"]
    for i, statement in enumerate(record["statements"]):
        resolves(f"statements[{i}].condition_ids", statement["condition_ids"],
                 "condition", conditions)
        resolves(f"statements[{i}].fact_ids", statement["fact_ids"], "fact_entry", entries)
    for i, group in enumerate(record["relations"]["groups"]):
        # a member is a statement or a nested group; nothing else can be connected
        resolves(f"relations.groups[{i}].members", group["members"],
                 "statement|group", statements | groups)
    for i, condition in enumerate(record["relations"]["conditions"]):
        resolves(f"relations.conditions[{i}].statement_ids", condition["statement_ids"],
                 "statement", statements)
        resolves(f"relations.conditions[{i}].fact_ids", condition["fact_ids"],
                 "fact_entry", entries)
    for i, example_set in enumerate(record["relations"]["example_sets"]):
        resolves(f"relations.example_sets[{i}].parent_statement_id",
                 [example_set["parent_statement_id"]], "statement", statements)
        resolves(f"relations.example_sets[{i}].mention_ids", example_set["mention_ids"],
                 "mention", known["mentions"])
    for i, entry in enumerate(record["facts"]["entries"]):
        resolves(f"facts.entries[{i}].statement_ids", entry["statement_ids"],
                 "statement", statements)
        resolves(f"facts.entries[{i}].condition_ids", entry["condition_ids"],
                 "condition", conditions)
    for i, mention in enumerate(record["mentions"]):
        resolves(f"mentions[{i}].statement_ids", mention["statement_ids"],
                 "statement", statements)
    for i, area in enumerate(record["areas"]):
        resolves(f"areas[{i}].statement_ids", area["statement_ids"], "statement", statements)
    for i, entry in enumerate(record["block_accounting"]):
        # accounting points at what was extracted from the block: statements or facts
        resolves(f"block_accounting[{i}].ref_ids", entry["ref_ids"],
                 "statement|fact_entry", statements | entries)


def _check_group_nesting(record: dict[str, Any], report: Report) -> None:
    """Group membership is a DAG no deeper than MAX_GROUP_DEPTH.

    Iterative on purpose: a cyclic or thousand-deep membership graph is exactly
    the input a recursive walk would die on before it could report anything.
    """
    groups = {g["id"]: g for g in record["relations"]["groups"]}
    children = {gid: [m for m in g["members"] if m in groups] for gid, g in groups.items()}
    white, grey, black = 0, 1, 2
    color = dict.fromkeys(groups, white)
    cyclic: set[str] = set()
    for root in groups:
        if color[root] != white:
            continue
        color[root] = grey
        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            node, i = stack[-1]
            kids = children[node]
            if i == len(kids):
                color[node] = black
                stack.pop()
                continue
            stack[-1] = (node, i + 1)
            child = kids[i]
            if color[child] == grey:
                cyclic.update({node, child})
            elif color[child] == white:
                color[child] = grey
                stack.append((child, 0))
    for gid in sorted(cyclic):
        report.error("references", f"relations.groups[{gid}]", "reference_cycle", group_id=gid)

    depth: dict[str, int] = {}
    for root in groups:
        if root in depth or root in cyclic:
            continue
        pending: list[tuple[str, bool]] = [(root, False)]
        while pending:
            node, expanded = pending.pop()
            if node in depth or node in cyclic:
                continue
            if expanded:
                depth[node] = 1 + max(
                    (depth[c] for c in children[node] if c in depth), default=0
                )
                continue
            pending.append((node, True))
            pending.extend((c, False) for c in children[node] if c not in depth)
    for gid in sorted(g for g, value in depth.items() if value > MAX_GROUP_DEPTH):
        report.error("references", f"relations.groups[{gid}]", "depth_exceeded",
                     depth=depth[gid], max_depth=MAX_GROUP_DEPTH)


def _check_relations(record: dict[str, Any], report: Report) -> None:
    """An asserted connective is a reading of the document and cites it;
    `unresolved` is the honest state for "the operator was never written down"."""
    for i, group in enumerate(record["relations"]["groups"]):
        if group["operator"] in ("all_of", "any_of") and not group["evidence"]:
            report.error("relations", f"relations.groups[{i}]", "connective_evidence_missing",
                         operator=group["operator"], group_id=group["id"])


def _check_statements(record: dict[str, Any], report: Report) -> None:
    for i, statement in enumerate(record["statements"]):
        path = f"statements[{i}]"
        kind, importance = statement["kind"], statement["importance"]
        if kind in IMPORTANCE_KINDS and importance is None:
            report.error("statements", path, "importance_missing", kind=kind)
        if kind not in IMPORTANCE_KINDS and importance is not None:
            # a duty or a piece of employer context imposes no applicant rule;
            # an importance there is the C12 conflation this contract forbids
            report.error("statements", path, "importance_unexpected",
                         kind=kind, importance=importance)
        if statement["proficiency"] is not None and not statement["proficiency_evidence"]:
            report.error("statements", path, "evidence_missing",
                         field="proficiency", value=statement["proficiency"])
        if importance in _EVIDENCED_IMPORTANCE and not statement["importance_evidence"]:
            report.error("statements", path, "evidence_missing",
                         field="importance", value=importance)


def _check_facts(record: dict[str, Any], report: Report) -> None:
    entries = record["facts"]["entries"]
    for i, entry in enumerate(entries):
        path = f"facts.entries[{i}]"
        family = entry["family"]
        if (entry["date_kind"] is not None) != (family == "date"):
            report.error("facts", path, "fact_family_shape",
                         field="date_kind", family=family, value=entry["date_kind"])
        if entry["component"] is not None and family != "compensation":
            report.error("facts", path, "fact_family_shape",
                         field="component", family=family, value=entry["component"])
        if entry["scope"] is not None and family not in ("experience", "quantity"):
            report.error("facts", path, "fact_family_shape", field="scope", family=family)
        rederived = _rederive(family, entry["evidence"])
        if rederived != entry["derived"]:
            report.error("facts", path, "fact_mismatch",
                         derived=rederived, stored=entry["derived"])
    for family, key in _PRESENCE_KEY.items():
        presence = record["facts"]["presence"][key]
        path = f"facts.presence.{key}"
        has_entries = any(entry["family"] == family for entry in entries)
        if has_entries != (presence["state"] == "stated"):
            # "stated with nothing extracted" and "none_found with entries" are
            # the two halves of the same lie about what the document says
            report.error("facts", path, "presence_mismatch",
                         state=presence["state"], entries=has_entries)
        if presence["state"] in _PRESENCE_NEEDING_EVIDENCE and not presence["evidence"]:
            report.error("facts", path, "presence_mismatch",
                         state=presence["state"], reason="evidence_required")


def _check_mentions(record: dict[str, Any], report: Report) -> None:
    """A mention is grounded in the span it cites and keyed by the versioned
    alias policy — never by a vocabulary invented at write time."""
    for i, mention in enumerate(record["mentions"]):
        path = f"mentions[{i}]"
        surface = mention["surface"]
        if surface not in mention["evidence"]["text"]:
            report.error("mentions", path, "mention_ungrounded",
                         surface=surface, evidence=mention["evidence"]["text"])
        expected = normalize_key(surface)
        if mention["normalized_key"] != expected:
            report.error("mentions", path, "mention_ungrounded",
                         surface=surface, expected_key=expected,
                         stored_key=mention["normalized_key"])


def _check_accounting(record: dict[str, Any], blocks: list[Block], report: Report) -> None:
    """Every nonempty block is accounted for, and an exclusion says why.

    Coverage is structural, never semantic recall: an accounted block can still
    hide a missed clause, which is why the requirement-language tripwire below
    is a warning pointed at the auditor rather than a pass/fail verdict.
    """
    by_id = {block.id: block for block in blocks}
    accounted: set[str] = set()
    excluded: set[str] = set()
    for i, entry in enumerate(record["block_accounting"]):
        path = f"block_accounting[{i}]"
        block_id, disposition = entry["block_id"], entry["disposition"]
        block = by_id.get(block_id)
        if block is None:
            report.error("accounting", path, "unknown_block", block_id=block_id)
        else:
            accounted.add(block_id)
        if disposition == "excluded":
            excluded.add(block_id)
            if entry["exclusion_reason"] is None:
                report.error("accounting", path, "exclusion_reason_missing", block_id=block_id)
            if block is not None and _REQUIREMENT_LANGUAGE.search(block.text):
                report.warn("accounting", path, "exclusion_requirement_language",
                            block_id=block_id, reason=entry["exclusion_reason"],
                            excerpt=block.text.strip()[:80])
        elif entry["exclusion_reason"] is not None:
            report.error("accounting", path, "exclusion_reason_missing",
                         disposition=disposition, reason=entry["exclusion_reason"])
        if disposition in _DISPOSITIONS_NEEDING_REFS and not entry["ref_ids"]:
            report.error("accounting", path, "refs_missing",
                         block_id=block_id, disposition=disposition)
    for block in blocks:
        if block.id not in accounted:
            report.error("accounting", "block_accounting", "block_unaccounted",
                         block_id=block.id, excerpt=block.text.strip()[:80])
    report.metrics.update({
        "n_blocks": len(blocks),
        "blocks_accounted": len(accounted),
        "excluded_blocks": len(excluded & set(by_id)),
    })


def _check_usability(record: dict[str, Any], blocks: list[Block], report: Report) -> None:
    """Source insufficiency has to agree with the source and with the output."""
    path = "source_assessment"
    usability = record["source_assessment"]["usability"]
    evidence = record["source_assessment"]["evidence"]
    if usability == "empty" and blocks:
        report.error("usability", path, "usability_conflict",
                     usability=usability, n_blocks=len(blocks))
    if not blocks and usability != "empty":
        report.error("usability", path, "usability_conflict", usability=usability, n_blocks=0)
    if blocks and usability in ("partial", "placeholder", "unsupported") and not evidence:
        report.error("usability", path, "usability_conflict",
                     usability=usability, reason="evidence_required")
    n_statements, n_entries = len(record["statements"]), len(record["facts"]["entries"])
    if usability in ("empty", "placeholder") and (n_statements or n_entries):
        report.error("usability", path, "empty_with_content", usability=usability,
                     n_statements=n_statements, n_fact_entries=n_entries)


def verify(record: dict[str, Any], markdown: str) -> Report:
    """Check one schema-2 record against the document it claims to describe."""
    report = Report(validator_version=VALIDATOR_VERSION)
    document = record.get("document")
    document = document if isinstance(document, dict) else {}
    stored = document.get("document_hash")
    if sha256_hex(markdown.encode("utf-8")) != stored:
        report.error("doc_binding", "document", "hash_mismatch", stored=stored)
        return report  # hard fail-fast: wrong document, nothing else is meaningful

    annotation = document.get("annotation_version")
    if annotation != ANNOTATION_VERSION:
        report.error("annotation", "document.annotation_version", "annotation_version",
                     stored=annotation, expected=ANNOTATION_VERSION)
        return report  # block ids and spans mean nothing under another annotation

    for message in validate_record(record, SCHEMA_VERSION):
        report.error("schema", "<schema>", "invalid", message=message)
    if report.status == "fail":
        return report  # structure unknown; every check below would KeyError

    blocks = annotate(markdown)
    _check_attribution(record, markdown, blocks_by_id(blocks), report)
    _check_references(record, report)
    _check_group_nesting(record, report)
    _check_relations(record, report)
    _check_statements(record, report)
    _check_facts(record, report)
    _check_mentions(record, report)
    _check_accounting(record, blocks, report)
    _check_usability(record, blocks, report)
    report.metrics.update({
        "n_statements": len(record["statements"]),
        "n_mentions": len(record["mentions"]),
        "n_fact_entries": len(record["facts"]["entries"]),
    })
    return report
