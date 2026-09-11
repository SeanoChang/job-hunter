"""Emit JSON → v2 record: bind every reference, derive every fact, attach the
code-owned document/extraction/quality blocks, hash the candidate. All binding
failures collect before raising so one reprompt carries the whole list.

Fail-fast was v1's mistake: a reprompt that names one bad quote burns a whole
retry ladder discovering the next one. Everything code owns — spans, derived
values, parse states, quality, the candidate hash — is computed here and never
read from the emit.
"""

from __future__ import annotations

import copy
from typing import Any, cast

from jobhunter.hashing import canonical_json, sha256_hex
from jobhunter.l2.v2.facts import (
    VALIDATOR_VERSION,
    derive_date,
    derive_money,
    derive_quantity,
)
from jobhunter.l2.v2.quality import assess
from jobhunter.l2.v2.source import (
    ANNOTATION_VERSION,
    RefBindError,
    annotate,
    blocks_by_id,
    resolve,
)
from jobhunter.l2.v2.types import Block
from jobhunter.markdown import NORMALIZER_VERSION

# /3 re-anchor · /4 derived presence · /5 typo tiers · /6 emphasis fold and
# code-owned single-occurrence selection
RULES_VERSION = "parsing-rules/6"
SCHEMA_VERSION = "2"
PROMPT_VERSION = "demand-profile/v6"
ALIAS_POLICY = "aliases/1"

# every nullable aspect of a fact entry's evidence, in schema order
_ASPECTS = ("comparison", "unit", "currency", "component", "applicability")
_PRESENCE_FAMILIES = ("experience", "compensation", "quantities", "dates")
# family → presence key; verify._PRESENCE_KEY mirrors this (verify imports
# from assemble, so the map lives here to avoid the cycle)
_PRESENCE_KEY = {"experience": "experience", "compensation": "compensation",
                 "quantity": "quantities", "date": "dates"}


class AssembleError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"{len(errors)} resolution error(s)")
        self.errors = errors


def normalize_key(surface: str) -> str:
    """Alias policy `aliases/1`: case-folded and trimmed, nothing else.

    Richer aliasing (synonyms, acronym expansion) belongs to concept linking;
    guessing it here would bake an unversioned vocabulary into candidate identity.
    """
    return surface.casefold().strip()


def candidate_hash(record: dict[str, Any]) -> str:
    """Identity of the extraction: everything but `quality` and the hash field.

    Quality is an audit outcome that keeps moving after assembly; folding it in
    would silently re-key a candidate and break repair parent links.
    """
    shadow = copy.deepcopy(record)
    shadow["extraction"]["candidate_hash"] = ""
    shadow["quality"] = None
    return sha256_hex(canonical_json(shadow))


class _Binder:
    """Binds emit references to spans, collecting every failure by path."""

    def __init__(self, blocks: dict[str, Block]) -> None:
        self.blocks = blocks
        self.errors: list[str] = []

    def ref(self, path: str, value: Any, *, lenient: bool = False) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.errors.append(f"{path}: expected a reference object")
            return None
        try:
            return resolve(value, self.blocks, lenient=lenient)
        except RefBindError as exc:
            self.errors.append(f"{path}: {exc.message}")
            return None

    def refs(self, path: str, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            self.errors.append(f"{path}: expected a non-empty list of references")
            return []
        bound: list[dict[str, Any]] = []
        for i, item in enumerate(value):
            one = self.ref(f"{path}[{i}]", item)
            if one is not None:
                bound.append(one)
        return bound

    def opt_refs(self, path: str, value: Any) -> list[dict[str, Any]] | None:
        if value is None:
            return None
        return self.refs(path, value) or None

    def field(self, path: str, node: dict[str, Any], key: str) -> list[dict[str, Any]] | None:
        return self.opt_refs(f"{path}.{key}", node.get(key))


def _texts(bound: list[dict[str, Any]] | None) -> str | None:
    """One aspect's cited text: the bound refs joined by a space, in source order."""
    if bound is None:
        return None
    ordered = sorted(bound, key=lambda r: (r["span"][0], r["span"][1]))
    return " ".join(str(r["text"]) for r in ordered)


def _derive(family: Any, evidence: dict[str, Any]) -> dict[str, Any]:
    """Code-owned parse of the cited spans. A grammar miss is `present_unparsed`,
    never a dropped fact: stated-but-unparsed must stay distinguishable from
    unstated. `conflicting` is the increment-2 auditor's state, never assembly's."""
    value = _texts(evidence["value"]) or ""
    if family in ("experience", "quantity"):
        quantity = derive_quantity(value, _texts(evidence["comparison"]))
        return {
            "state": "parsed" if quantity else "present_unparsed",
            "quantity": quantity, "money": None, "date": None,
        }
    if family == "compensation":
        money = derive_money(
            value,
            _texts(evidence["comparison"]),
            _texts(evidence["currency"]),
            _texts(evidence["unit"]),
        )
        return {
            "state": "parsed" if money else "present_unparsed",
            "quantity": None, "money": money, "date": None,
        }
    date = derive_date(value)
    state = (
        "present_unparsed" if date is None else "ambiguous" if date["date"] is None else "parsed"
    )
    return {"state": state, "quantity": None, "money": None, "date": date}


def _statement(binder: _Binder, index: int, node: dict[str, Any]) -> dict[str, Any]:
    path = f"statements[{index}]"
    return {
        "id": node.get("id"),
        "kind": node.get("kind"),
        "subject": node.get("subject"),
        "topic": node.get("topic"),
        "evidence": binder.refs(f"{path}.evidence", node.get("evidence")),
        "importance": node.get("importance"),
        "importance_evidence": binder.field(path, node, "importance_evidence"),
        "polarity": node.get("polarity"),
        "polarity_evidence": binder.field(path, node, "polarity_evidence"),
        "proficiency": node.get("proficiency"),
        "proficiency_evidence": binder.field(path, node, "proficiency_evidence"),
        "condition_ids": list(node.get("condition_ids") or []),
        "fact_ids": list(node.get("fact_ids") or []),
        "unresolved": [
            {
                "reason": issue.get("reason"),
                "evidence": binder.refs(f"{path}.unresolved[{j}].evidence", issue.get("evidence")),
            }
            for j, issue in enumerate(node.get("unresolved") or [])
        ],
    }


def _group(binder: _Binder, index: int, node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "operator": node.get("operator"),
        "members": list(node.get("members") or []),
        "evidence": binder.field(f"relations.groups[{index}]", node, "evidence"),
    }


def _condition(binder: _Binder, index: int, node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "kind": node.get("kind"),
        "evidence": binder.refs(f"relations.conditions[{index}].evidence", node.get("evidence")),
        "statement_ids": list(node.get("statement_ids") or []),
        "fact_ids": list(node.get("fact_ids") or []),
    }


def _example_set(binder: _Binder, index: int, node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "parent_statement_id": node.get("parent_statement_id"),
        "mention_ids": list(node.get("mention_ids") or []),
        "exhaustive": node.get("exhaustive"),
        "evidence": binder.field(f"relations.example_sets[{index}]", node, "evidence"),
    }


def _presence(binder: _Binder, family: str, node: Any) -> dict[str, Any]:
    node = node if isinstance(node, dict) else {}
    return {
        "state": node.get("state"),
        "evidence": binder.field(f"facts.presence.{family}", node, "evidence"),
    }


def _entry(binder: _Binder, index: int, node: dict[str, Any]) -> dict[str, Any]:
    path = f"facts.entries[{index}]"
    emitted = node.get("evidence")
    emitted = emitted if isinstance(emitted, dict) else {}
    evidence: dict[str, Any] = {
        "value": binder.refs(f"{path}.evidence.value", emitted.get("value"))
    }
    for aspect in _ASPECTS:
        evidence[aspect] = binder.field(f"{path}.evidence", emitted, aspect)
    scope = node.get("scope")
    if isinstance(scope, dict):
        scope = {
            "kind": scope.get("kind"),
            "evidence": binder.field(f"{path}.scope", scope, "evidence"),
        }
    return {
        "id": node.get("id"),
        "family": node.get("family"),
        "statement_ids": list(node.get("statement_ids") or []),
        "condition_ids": list(node.get("condition_ids") or []),
        "scope": scope,
        "date_kind": node.get("date_kind"),
        "component": node.get("component"),
        "evidence": evidence,
        "derived": _derive(node.get("family"), evidence),
    }


def _mention(binder: _Binder, index: int, node: dict[str, Any]) -> dict[str, Any]:
    surface = node.get("surface")
    return {
        "id": node.get("id"),
        "surface": surface,
        "evidence": binder.ref(f"mentions[{index}].evidence", node.get("evidence"), lenient=True),
        "statement_ids": list(node.get("statement_ids") or []),
        "role": node.get("role"),
        "normalized_key": normalize_key(surface if isinstance(surface, str) else ""),
    }


def _area(binder: _Binder, index: int, node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "kind": node.get("kind"),
        "statement_ids": list(node.get("statement_ids") or []),
        "evidence": binder.field(f"areas[{index}]", node, "evidence"),
    }


def _accounting(binder: _Binder, index: int, node: dict[str, Any]) -> dict[str, Any]:
    return {
        "block_id": node.get("block_id"),
        "disposition": node.get("disposition"),
        "ref_ids": list(node.get("ref_ids") or []),
        "exclusion_reason": node.get("exclusion_reason"),
        "evidence": binder.field(f"block_accounting[{index}]", node, "evidence"),
    }


def assemble(
    emit: dict[str, Any],
    markdown: str,
    *,
    document_hash: str,
    observed_model: str,
    at: str,
    normalizer_version: str = NORMALIZER_VERSION,
    prompt_version: str = PROMPT_VERSION,
    parent_candidate_hash: str | None = None,
) -> dict[str, Any]:
    """Bind, derive and seal one emit into a schema-2 record.

    Raises AssembleError carrying every binding failure at once.
    """
    binder = _Binder(blocks_by_id(annotate(markdown)))
    assessment = emit.get("source_assessment") or {}
    relations = emit.get("relations") or {}
    facts = emit.get("facts") or {}
    presence = facts.get("presence") or {}
    record: dict[str, Any] = {
        "document": {
            "document_hash": document_hash,
            "normalizer_version": normalizer_version,
            "annotation_version": ANNOTATION_VERSION,
        },
        "source_assessment": {
            "usability": assessment.get("usability"),
            "evidence": binder.field("source_assessment", assessment, "evidence"),
            "note": assessment.get("note"),
        },
        "statements": [
            _statement(binder, i, s) for i, s in enumerate(emit.get("statements") or [])
        ],
        "relations": {
            "groups": [_group(binder, i, g) for i, g in enumerate(relations.get("groups") or [])],
            "conditions": [
                _condition(binder, i, c) for i, c in enumerate(relations.get("conditions") or [])
            ],
            "example_sets": [
                _example_set(binder, i, e)
                for i, e in enumerate(relations.get("example_sets") or [])
            ],
        },
        "facts": {
            "presence": {
                family: _presence(binder, family, presence.get(family))
                for family in _PRESENCE_FAMILIES
            },
            "entries": [_entry(binder, i, e) for i, e in enumerate(facts.get("entries") or [])],
        },
        "mentions": [_mention(binder, i, m) for i, m in enumerate(emit.get("mentions") or [])],
        "areas": [_area(binder, i, a) for i, a in enumerate(emit.get("areas") or [])],
        "block_accounting": [
            _accounting(binder, i, e) for i, e in enumerate(emit.get("block_accounting") or [])
        ],
        "extraction": {
            "model": observed_model,
            "prompt_version": prompt_version,
            "schema_version": SCHEMA_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "rules_version": RULES_VERSION,
            "at": at,
            "candidate_hash": "",
            "parent_candidate_hash": parent_candidate_hash,
        },
        # usability is raw model output at this point — cast, not asserted; an
        # invalid value still fails record schema validation downstream.
        "quality": assess(source=cast(str, assessment.get("usability")), evidence="pass"),
    }
    if binder.errors:
        raise AssembleError(binder.errors)
    _reconcile_presence(record)
    record["extraction"]["candidate_hash"] = candidate_hash(record)
    return record


def _reconcile_presence(record: dict[str, Any]) -> None:
    """parsing-rules/4: presence states are code-derived from the entries.

    The live failure class this resolves: models declare a family "stated"
    and then fail to produce its (hard) per-aspect entries — 94 of ~230
    verify errors on the 2026-09-10 quarantine set, all honest
    under-extraction. Entries are proof of statedness, so code owns the
    consistency: entries present ⇒ stated; a "stated" claim without entries
    downgrades to `unresolved` when it carries evidence (the document says
    it, extraction did not resolve it) and `none_found` when it does not.
    Omission detection moves to the auditor, where the spec places semantic
    recall — the deterministic layer never fails a record for a claim code
    can reconcile.
    """
    entries = record["facts"]["entries"]
    for family, key in _PRESENCE_KEY.items():
        node = record["facts"]["presence"][key]
        has_entries = any(e["family"] == family for e in entries)
        if has_entries:
            node["state"] = "stated"
        elif node["state"] == "stated":
            node["state"] = "unresolved" if node["evidence"] else "none_found"
