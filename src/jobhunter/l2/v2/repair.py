"""The semantic repair contract (`semantic-repair/v2`, spec §4).

One audited candidate, its findings and the original source go in; a list of
typed operations comes back, and this module decides whether that list may
touch the record at all. It mirrors audit.py: the prompt bytes, the closed
engine-facing schema, and one judge — here `apply()`, which either returns a
NEW assembled record or raises. Pure: no I/O, no model call, no env.

Four rules shape everything below.

1. Operations address typed objects, never JSON paths. A path patch can reach
   `extraction.candidate_hash` or a span inside an evidence reference; a
   `replace` of statement `s2` cannot. The addressable kinds are closed
   (`KINDS`), the object a kind carries is the emit object of that kind in the
   BASE RECORD's schema, and code-owned fields (`span`, `derived`,
   `normalized_key`, and the `object_hash` the prompt shows) are stripped from
   whatever the model sends before the object is held to that schema — spec
   §3: code-owned fields are never accepted from the model. `section_heading`
   is the one code-owned field that is refused rather than stripped: the
   prompt shows it because it is context worth reading, and assembly derives
   it from the evidence the operation itself cites, so an object that sends
   one back is proposing a field no evidence supports (parsing contract v3
   §2.1, and `verify` re-derives it for the same reason).
2. The old-object hash is shown, not computed. No model can hash canonical
   JSON, so `render` prints each addressable object's hash beside it and an
   operation echoes it back. That is what makes a stale edit — one written
   against an object the candidate no longer holds — a rejection rather than
   a silent overwrite.
3. A failed repair never mutates. Every defect collects into one
   `RepairJudgeError` (the whole list at once, as in assemble.py and
   audit.py), the base record is never written to, and the caller settles the
   base candidate exactly as it would have without the repair round.
4. The rebuild goes through assembly, not through edits. The base record is
   turned back into the emit it came from, the operations are applied there,
   and `assemble` binds every reference, derives every fact, reconciles
   presence and hashes the result — so a repaired candidate is a whole
   assembled record (spec §4) that no partial-edit path could produce, and
   `parent_candidate_hash` is the only thing linking it to its base.

What this module does NOT check is record-level integrity: a removal that
orphans a support link, a group whose members vanished. `verify()` owns that
grammar and the runner re-verifies the repaired candidate before it can
settle, so restating it here would be two copies of one rule.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from functools import cache
from typing import Any

import jsonschema

from jobhunter.hashing import canonical_json, sha256_hex
from jobhunter.l2.schemas import emit_schema as packaged_emit_schema
from jobhunter.l2.v2.assemble import SCHEMA_VERSION, AssembleError, assemble
from jobhunter.l2.v2.source import RefBindError, annotate, blocks_by_id, resolve

# version history (bump, never edit in place):
#   v1: the spec §4 contract as written
#   v2: parsing contract v3 §6 — operations follow the schema-3 statement
#       shape. `modality_evidence` is an editable reference family like
#       `evidence`; `importance`/`proficiency` are not fields any more and an
#       object carrying one is refused; `section_heading` is code's and is
#       refused too. The shape is taken from the BASE RECORD rather than from
#       a module constant, because the archive holds schema-2 candidates until
#       migration reaches them (spec §7) and a repair addresses the objects the
#       candidate it repairs actually has — and the shape ADVERTISED to the
#       engine is the round's own schema version, passed in by the runner from
#       `bundle.schema_version`, so the schema a round is answered under and
#       the schema it is judged under move together. Policy — one round, one
#       re-audit, old-object-hash guards, failed repair mutates nothing — is
#       unchanged.
#   v3: parsing contract v4 §4 — operations follow the schema-4 shapes. A
#       mention carries its `type`; `presence` reaches the three authorization
#       families, each held to its own definition (work_authorization takes no
#       polarity); and `tracks` is a kind of its own, because
#       `relations.tracks` is one nullable object rather than a collection:
#       added when the candidate has none, replaced or removed (back to null)
#       under its printed hash. The code-owned `authorization` block is never
#       sent back — assembly re-derives it from the repaired presence. Policy
#       is v2's. The schema-4 handling keys off the BASE RECORD's shape, so v2
#       (which only ever sees schema 2 and 3 records) is unaffected, and its
#       bytes and advertised schemas stay as bundle v2 ships them.
REPAIR_VERSION = "semantic-repair/v2"
REPAIR_VERSION_V3 = "semantic-repair/v3"

#: spec §4: "Operations add/replace/remove objects in statements, relations,
#: fact entries, mentions, and areas, or replace a fact-presence object,
#: source assessment, or block-accounting entry."
OPS = ("add", "replace", "remove")
KINDS = ("statement", "group", "condition", "example_set", "fact_entry",
         "mention", "area", "accounting_entry", "presence", "source_assessment")

#: The three kinds that are FIELDS of the record rather than members of a
#: collection: there is nothing to add and nothing to take away, only a
#: corrected value to put in place.
_REPLACE_ONLY = frozenset({"accounting_entry", "presence", "source_assessment"})

#: kind → the list holding it, by the same path in the record and in the emit
#: (which is what lets an operation resolved against the record apply at the
#: same index in the rebuilt emit).
_LISTS: dict[str, tuple[str, ...]] = {
    "statement": ("statements",),
    "group": ("relations", "groups"),
    "condition": ("relations", "conditions"),
    "example_set": ("relations", "example_sets"),
    "fact_entry": ("facts", "entries"),
    "mention": ("mentions",),
    "area": ("areas",),
    "accounting_entry": ("block_accounting",),
}

#: kind → its definition in emit schema 2. `source_assessment` is a top-level
#: property there rather than a `$def`, and is lifted in `_object_def`.
_DEFS = {"statement": "statement", "group": "group", "condition": "condition",
         "example_set": "example_set", "fact_entry": "fact_entry",
         "mention": "mention", "area": "area",
         "accounting_entry": "accounting_entry", "presence": "presence"}

_PRESENCE_FAMILIES = ("experience", "compensation", "quantities", "dates")

# --- schema 4 (semantic-repair/v3) ------------------------------------------
#: the record shapes that carry parsing contract v4's additions
_CONTRACT_4_SCHEMAS = frozenset({"4"})
#: schema 4's own kind: `relations.tracks`, one nullable object
_TRACKS = "tracks"
#: the authorization presence families and the definition each is held to
_AUTHORIZATION_DEFS = {"sponsorship": "authorization_presence",
                       "citizenship": "authorization_presence",
                       "work_authorization": "work_authorization_presence"}


def _contract_4(schema_version: str) -> bool:
    return schema_version in _CONTRACT_4_SCHEMAS


def _presence_families(schema_version: str) -> tuple[str, ...]:
    if _contract_4(schema_version):
        return (*_PRESENCE_FAMILIES, *_AUTHORIZATION_DEFS)
    return _PRESENCE_FAMILIES


def _kinds(schema_version: str) -> tuple[str, ...]:
    """The kinds a round over a record of this shape may address."""
    return (*KINDS, _TRACKS) if _contract_4(schema_version) else KINDS

#: the emit's own top-level keys — everything a repair may reach
_EMIT_KEYS = ("source_assessment", "statements", "relations", "facts",
              "mentions", "areas", "block_accounting")

#: spec §4: "Do not change document identity, versions, provenance, or quality
#: assessments." The kinds above cannot address these blocks at all; the check
#: exists so an object that smuggles one back gets told which rule it broke.
_IMMUTABLE = frozenset({"document", "extraction", "quality"})

#: fields code owns and recomputes: bound spans, derived fact values,
#: normalized mention keys, and the old-object hash the prompt itself added.
#: Stripped from a model-sent object rather than refused, because every one of
#: them is printed inside the candidate the model is shown and echoing what you
#: were given is not a proposal (the semantic-audit/v3 hash-echo lesson).
_CODE_OWNED = frozenset({"span", "derived", "normalized_key", "object_hash"})

#: The record-only fields on top of those: `section_heading` (schema 3) is
#: derived by assembly and re-derived by `verify`, so it exists in the record
#: and never in an emit. It is dropped when WE turn a record back into the emit
#: it came from — and refused, by the emit schema itself, when a MODEL sends
#: one, which is the difference between echoing code's bookkeeping and claiming
#: a document structure no cited evidence supports.
_RECORD_ONLY = _CODE_OWNED | {"section_heading"}

_HASH_KEY = "object_hash"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# code-owned bookkeeping the repairer must not see, for audit.py's reasons:
# `quality` is the verdict this round is trying to move (circular) and
# `extraction` names the model that produced the candidate.
_NOT_SENT = ("quality", "extraction")


def object_hash(obj: Any) -> str:
    """The hash `render` shows beside an object and an operation echoes back."""
    return sha256_hex(canonical_json(obj))


_GUARD = """\
You are repairing ONE candidate extraction of ONE job posting document. The \
document is below as numbered source blocks, then the candidate, then the \
audit findings you must address.
"""

# spec §4 Repair, verbatim. A contract, not a paraphrase target.
_REPAIRER = """\
Propose changes to address the cited findings using the original source.
Return only the allowed typed repair operations for the supplied base hash.
Preserve unaffected supported statements and their IDs.

Every addition, replacement, or removal needs source evidence and a reason.
A missing statement may be added. A false statement may be removed with
evidence. Difficulty parsing is not a reason to remove supported information.
Keep ambiguity or unsupported numeric grammar visible as unresolved.

Do not change document identity, versions, provenance, or quality assessments.
Do not silently change unrelated statements or remove their support links.
"""

_EMIT_FORMAT_NOTE = """\
Return only JSON: no prose, no markdown fences. The operation schema itself is
not repeated here — it travels with this call as the engine's own output
schema, and your JSON must conform to it exactly.

Echo the base candidate hash back exactly as given, in "base_candidate_hash".
Emit one operation per change, and no operation that changes nothing.

Each operation has "op", "kind", "target_id", "object", "old_object_hash",
"finding_id", "evidence" and "reason".

  "op" is add, replace or remove.
  "kind" names what the operation addresses:
    statement, group, condition, example_set, fact_entry, mention, area —
      added, replaced or removed; "target_id" is the object's id
    accounting_entry — replaced only; "target_id" is its block id
    presence — replaced only; "target_id" is experience, compensation,
      quantities or dates
    source_assessment — replaced only; "target_id" is null
  "object" is the COMPLETE object in the emit schema's shape for that kind,
    null for a removal. A replacement keeps the id it replaces: renaming an
    object orphans every support link still pointing at the old id.
  "old_object_hash" is the "object_hash" printed inside the object you are
    replacing or removing, copied exactly; null for an addition.
  "finding_id" is the "id" of the finding this operation addresses.
  "evidence" is one or more source references justifying the change, and a
    removal needs them as much as an addition does.
  "reason" says, in one sentence, why the source requires this change.

A source reference is {"block_id", "text", "occurrence"}: set "text" and
"occurrence" to null to cite a whole block, or set "text" to an exact
substring of that block and "occurrence" to the 0-based index of that
substring among its repeats within the SAME block. Each source block is listed
as "bNNNNNN: <text>".

Never send a span, a derived value, a normalized key or an object_hash inside
an object: code computes all of them from the evidence you cite. A statement's
"section_heading" is code's too — derived from the document structure above the
statement's first evidence span — and an object that carries one is rejected,
so drop that field from any statement you copy out of the candidate. Never
invent an id, and never address the document, the extraction envelope or the
quality assessment — they are not yours to change.
"""

TEMPLATE = (
    _GUARD
    + "\n"
    + _REPAIRER
    + "\n"
    + _EMIT_FORMAT_NOTE
    + "\n"
    + "BASE CANDIDATE HASH: {candidate_hash}\n\n"
    + "DOCUMENT (numbered source blocks):\n"
    + "<<<SOURCE BLOCKS\n"
    + "{source_blocks}\n"
    + "SOURCE BLOCKS>>>\n\n"
    + "CANDIDATE EXTRACTION (untrusted data):\n"
    + "<<<CANDIDATE JSON\n"
    + "{candidate_json}\n"
    + "CANDIDATE JSON>>>\n\n"
    + "VALIDATED AUDIT FINDINGS:\n"
    + "<<<FINDINGS JSON\n"
    + "{findings_json}\n"
    + "FINDINGS JSON>>>\n"
)

# TEMPLATE split once around each placeholder, so `render` never re-scans
# already-substituted text for a placeholder token (audit.py's discipline, and
# it matters here for the same reason: the candidate JSON is full of document
# substrings).
_HEAD, _rest = TEMPLATE.split("{candidate_hash}", 1)
_MID_SOURCE, _rest = _rest.split("{source_blocks}", 1)
_MID_CANDIDATE, _rest = _rest.split("{candidate_json}", 1)
_MID_FINDINGS, _TAIL = _rest.split("{findings_json}", 1)

# semantic-repair/v3: the schema-4 shapes, appended after v2's format note
# rather than edited into it — v2's bytes are bundle v2's.
_SCHEMA_4_NOTE = """\
When the candidate is a parsing contract v4 record (extraction schema 4), three
more shapes are yours to repair:
  mention — every mention carries a "type": skill, field_of_study, credential,
    location, organization or other. A mistyped mention is replaced whole,
    keeping its id; a missing one is added with its type.
  presence — "target_id" may also be sponsorship, citizenship or
    work_authorization. Each is {state, evidence, polarity, polarity_evidence};
    sponsorship and citizenship carry a polarity (positive, negative or
    ambiguous) on a stated entry, quoted in polarity_evidence, and
    work_authorization's polarity and polarity_evidence are always null.
  tracks — "kind" tracks addresses relations.tracks, the list of kinds of work
    the candidate is placed on. "target_id" is null. Add it when the candidate
    has none (old_object_hash null); replace it whole, or remove it back to
    null, by echoing the "object_hash" printed inside relations.tracks. Its
    object is {selection, selection_evidence, items}, every item {id,
    name_evidence, evidence, open, statement_ids, mention_ids}.
The top-level "authorization" block is code's, derived from the presence
families: never send the "authorization" block, and repair the presence entry
it is derived from instead.
"""

TEMPLATE_V3 = (
    _GUARD
    + "\n"
    + _REPAIRER
    + "\n"
    + _EMIT_FORMAT_NOTE
    + "\n"
    + _SCHEMA_4_NOTE
    + "\n"
    + "BASE CANDIDATE HASH: {candidate_hash}\n\n"
    + "DOCUMENT (numbered source blocks):\n"
    + "<<<SOURCE BLOCKS\n"
    + "{source_blocks}\n"
    + "SOURCE BLOCKS>>>\n\n"
    + "CANDIDATE EXTRACTION (untrusted data):\n"
    + "<<<CANDIDATE JSON\n"
    + "{candidate_json}\n"
    + "CANDIDATE JSON>>>\n\n"
    + "VALIDATED AUDIT FINDINGS:\n"
    + "<<<FINDINGS JSON\n"
    + "{findings_json}\n"
    + "FINDINGS JSON>>>\n"
)


def _parts(template: str) -> tuple[str, str, str, str, str]:
    head, rest = template.split("{candidate_hash}", 1)
    mid_source, rest = rest.split("{source_blocks}", 1)
    mid_candidate, rest = rest.split("{candidate_json}", 1)
    mid_findings, tail = rest.split("{findings_json}", 1)
    return head, mid_source, mid_candidate, mid_findings, tail


_PARTS_V3 = _parts(TEMPLATE_V3)


def template_sha() -> str:
    return sha256_hex(TEMPLATE.encode("utf-8"))


def template_sha_v3() -> str:
    return sha256_hex(TEMPLATE_V3.encode("utf-8"))


def _stamp(obj: Any) -> None:
    """Print the object's hash inside it — over its own fields, never over a
    hash a previous rendering left behind."""
    if isinstance(obj, dict):
        obj[_HASH_KEY] = object_hash({k: v for k, v in obj.items() if k != _HASH_KEY})


def _shown(record: dict[str, Any]) -> dict[str, Any]:
    """The candidate as the repairer sees it: every addressable object carrying
    the hash an operation must echo to touch it."""
    shown = copy.deepcopy({k: v for k, v in record.items() if k not in _NOT_SENT})
    for kind in _LISTS:
        for obj in _list_at(shown, kind):
            _stamp(obj)
    presence = shown.get("facts", {}).get("presence")
    if isinstance(presence, dict):
        for obj in presence.values():
            _stamp(obj)
    _stamp(shown.get("source_assessment"))
    # schema 4: `relations.tracks` is addressed whole; null prints nothing
    _stamp((shown.get("relations") or {}).get(_TRACKS))
    return shown


def _render(
    parts: tuple[str, str, str, str, str],
    markdown: str,
    candidate_hash: str,
    record: dict[str, Any],
    findings: list[dict[str, Any]],
) -> str:
    head, mid_source, mid_candidate, mid_findings, tail = parts
    source_blocks = "\n".join(f"{b.id}: {b.text}" for b in annotate(markdown))
    listed = [{**finding, "id": f"f{i + 1}"} for i, finding in enumerate(findings)]
    return (
        head
        + candidate_hash
        + mid_source
        + source_blocks
        + mid_candidate
        + json.dumps(_shown(record), indent=2, sort_keys=True, ensure_ascii=False)
        + mid_findings
        + json.dumps(listed, indent=2, sort_keys=True, ensure_ascii=False)
        + tail
    )


def render(
    markdown: str,
    candidate_hash: str,
    record: dict[str, Any],
    findings: list[dict[str, Any]],
) -> str:
    """The repair prompt: the base hash, the numbered `blocks/1` listing of the
    full source, the candidate with its per-object hashes, and the validated
    findings the round exists to address.

    Findings are numbered here — an audit emit carries no ids — so `finding_id`
    on an operation names a finding the model was actually shown.
    """
    return _render((_HEAD, _MID_SOURCE, _MID_CANDIDATE, _MID_FINDINGS, _TAIL),
                   markdown, candidate_hash, record, findings)


def render_v3(
    markdown: str,
    candidate_hash: str,
    record: dict[str, Any],
    findings: list[dict[str, Any]],
) -> str:
    """`render` over the semantic-repair/v3 bytes (a schema-4 candidate)."""
    return _render(_PARTS_V3, markdown, candidate_hash, record, findings)


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _typed(node: dict[str, Any]) -> dict[str, Any]:
    """An enum node with its type spelled out: strict output-schema modes
    reject a bare enum (the emit_guard.py lesson), and emit schema 2 — written
    for OUR validator — has several."""
    enum = node.get("enum")
    if not isinstance(enum, list) or "type" in node:
        return node
    types = sorted({_json_type(value) for value in enum})
    return {**node, "type": types[0] if len(types) == 1 else types}


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    """One packaged emit schema with `$ref` resolved in place and every enum typed.

    Inlined rather than re-declared so the objects a repair may carry are the
    packaged emit contract itself — one grammar, not a copy that can drift —
    and `$ref`-free because an engine's output schema cannot resolve local
    refs (audit.py's emit_schema makes the same call).
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            return _inline(defs[ref.removeprefix("#/$defs/")], defs)
        return _typed({key: _inline(value, defs) for key, value in node.items()})
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    return node


def _object_def(kind: str, schema_version: str) -> dict[str, Any]:
    """The emit definition an object is held to. `kind` is an operation kind,
    or — schema 4 — the name of one of the extra definitions an operation
    kind resolves to (`_def_key`)."""
    schema = packaged_emit_schema(schema_version)
    defs = schema["$defs"]
    node = (
        schema["properties"]["source_assessment"]
        if kind == "source_assessment"
        else defs[_DEFS.get(kind, kind)]
    )
    inlined: dict[str, Any] = _inline(node, defs)
    return inlined


def _def_key(kind: str, target: Any, schema_version: str) -> str:
    """Which definition this operation's object is held to: the kind's own,
    except a schema-4 authorization family, whose presence entry is its own
    shape (a bound polarity; none at all for work_authorization)."""
    if kind == "presence" and _contract_4(schema_version) and target in _AUTHORIZATION_DEFS:
        return _AUTHORIZATION_DEFS[str(target)]
    return kind


def _object_defs(schema_version: str) -> list[str]:
    """Every definition an operation's object may take under this shape."""
    keys = list(KINDS)
    if _contract_4(schema_version):
        keys += sorted(set(_AUTHORIZATION_DEFS.values())) + [_TRACKS]
    return keys


def _evidence_def(schema_version: str) -> dict[str, Any]:
    schema = packaged_emit_schema(schema_version)
    inlined: dict[str, Any] = _inline(schema["$defs"]["evidence"], schema["$defs"])
    return inlined


def _operation_schema(
    schema_version: str,
    kinds: tuple[str, ...] = KINDS,
    object_keys: tuple[str, ...] | list[str] = KINDS,
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["op", "kind", "target_id", "object", "old_object_hash",
                     "finding_id", "evidence", "reason"],
        "properties": {
            "op": {"type": "string", "enum": list(OPS),
                   "description": "add and remove apply to collection members only"},
            "kind": {"type": "string", "enum": list(kinds),
                     "description": "what this operation addresses; decides the "
                                    "shape of object and the meaning of target_id"},
            "target_id": {
                "type": ["string", "null"],
                "description": "the addressed object's id; a block id for an "
                               "accounting_entry, a family for presence, null for "
                               "source_assessment and for every addition",
            },
            "object": {
                "anyOf": [
                    *(_object_def(kind, schema_version) for kind in object_keys),
                    {"type": "null"},
                ],
                "description": "the complete object in this kind's emit shape; "
                               "null for a removal. No spans, no derived values, "
                               "no normalized keys, no object_hash, no "
                               "section_heading.",
            },
            "old_object_hash": {
                "type": ["string", "null"], "pattern": "^[0-9a-f]{64}$",
                "description": "the object_hash printed inside the object being "
                               "replaced or removed; null for an addition",
            },
            "finding_id": {"type": "string", "minLength": 1, "maxLength": 40,
                           "description": "the id of the finding this addresses"},
            "evidence": {**_evidence_def(schema_version),
                         "description": "source references justifying the change"},
            "reason": {"type": "string", "minLength": 1, "maxLength": 600,
                       "description": "why the source requires this change"},
        },
    }


def emit_schema(schema_version: str = SCHEMA_VERSION) -> dict[str, Any]:
    """The engine-facing repair schema: closed op and object kinds.

    Written against the packaged emit schema's own definitions (inlined, typed)
    so the object a repair carries is the object the extractor emits — the
    kind↔object pairing is then the one thing left for `apply` to enforce, and
    it must enforce it anyway: a schema is a hint to the engine, never the gate.

    The version travels from the bundle to here: the runner registers this one
    module under every live SCHEMA version (`_REPAIR_CONTRACTS`) and
    `_repair_pass` calls it with `bundle.schema_version`, so what a round
    advertises is the shape that round's bundle assembles, and nothing in this
    module has to move when the bundle bumps. That matters because `apply`
    holds every operation to the BASE RECORD's own shape whatever was
    advertised here, and a schema that disagrees with it cannot be recovered
    from: the engine answers under it (structured output, not a hint), the
    answer is refused, and `x_repair_key` is write-once, so the candidate's one
    round is spent on a round it could not have won.

    The default is `assemble.SCHEMA_VERSION` — schema 2, the oldest live shape
    and the one the archived corpus holds, the same convention as
    `emit_guard.engine_emit_schema`. It is a convenience for callers reading
    the base contract (tests, a replay of an archived candidate), never an
    advertisement: no caller in `src/` takes it.
    """
    return _emit_schema(REPAIR_VERSION, _operation_schema(schema_version))


def emit_schema_v3(schema_version: str = SCHEMA_VERSION) -> dict[str, Any]:
    """semantic-repair/v3's operation schema for a round at `schema_version`.

    Schema 2 and 3 advertise exactly v2's operations. Schema 4 widens the kind
    enum with `tracks` and the object union with the definitions a schema-4
    operation may carry: the two authorization presence shapes and the tracks
    object. `apply` still holds each object to the one definition its kind and
    target resolve to (`_def_key`); the union is the engine's hint.
    """
    return _emit_schema(REPAIR_VERSION_V3, _operation_schema(
        schema_version, _kinds(schema_version), _object_defs(schema_version)))


def _emit_schema(version: str, operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"job-hunter L2 semantic repair emit schema ({version})",
        "type": "object",
        "additionalProperties": False,
        "required": ["base_candidate_hash", "operations"],
        "properties": {
            "base_candidate_hash": {
                "type": "string", "pattern": "^[0-9a-f]{64}$",
                "description": "the supplied base candidate hash, echoed verbatim",
            },
            "operations": {
                "type": "array",
                "items": operation,
                "description": "the typed repair operations, one per change",
            },
        },
    }


@cache
def _object_validator(kind: str, schema_version: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_object_def(kind, schema_version))


@cache
def _reference_validator(schema_version: str) -> jsonschema.Draft202012Validator:
    """A source reference held to the packaged `reference` definition — the same
    grammar the extractor emits under, so an operation's own citation is typed
    exactly like the citations inside the objects it carries."""
    schema = packaged_emit_schema(schema_version)
    inlined: dict[str, Any] = _inline(schema["$defs"]["reference"], schema["$defs"])
    return jsonschema.Draft202012Validator(inlined)


def _schema_version_of(record: dict[str, Any]) -> str:
    """The shape of the candidate being repaired, off its own envelope.

    A repair rebuilds the record it was handed (rule 4), so the schema is the
    base record's, never the contract's idea of a current one: the archive
    holds schema-2 candidates until migration reaches them (spec §7) and an
    operation against one addresses schema-2 objects. A record with no readable
    version falls back to the shape this tuple assembles — the same default
    `emit_schema` advertises, so the two halves of a round never disagree about
    a record that could not say — and `apply`'s object checks then say what is
    wrong in the language of the fields.
    """
    extraction = record.get("extraction")
    version = extraction.get("schema_version") if isinstance(extraction, dict) else None
    return version if isinstance(version, str) and version else SCHEMA_VERSION


class RepairJudgeError(Exception):
    """Every concrete defect in one repair emit. A failed repair never mutates
    the base candidate (spec §4) and never settles anything on its own."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"{len(errors)} repair validity error(s): {'; '.join(errors)}")
        self.errors = errors


@dataclass(frozen=True)
class _Op:
    """One resolved operation: where it lands, and what it puts there."""

    op: str
    kind: str
    index: int          # position in the kind's list; -1 when not list-addressed
    family: str         # the presence family; "" otherwise
    object: dict[str, Any] | None


def _excerpt(value: Any, limit: int = 80) -> str:
    """A bounded, quoted rendering of untrusted emit content: these strings land
    in an archived error artifact."""
    if isinstance(value, str):
        return repr(value[:limit])
    return repr(value)


def _list_at(node: Any, kind: str) -> list[Any]:
    for key in _LISTS[kind]:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, list) else []


def _set_list(node: dict[str, Any], kind: str, value: list[Any]) -> None:
    path = _LISTS[kind]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _ids(record: dict[str, Any], kind: str) -> dict[str, int]:
    return {
        obj["id"]: i
        for i, obj in enumerate(_list_at(record, kind))
        if isinstance(obj, dict) and isinstance(obj.get("id"), str)
    }


def _strip(node: Any, fields: frozenset[str] | set[str]) -> Any:
    """`node` with `fields` dropped at every depth."""
    if isinstance(node, dict):
        return {k: _strip(v, fields) for k, v in node.items() if k not in fields}
    if isinstance(node, list):
        return [_strip(item, fields) for item in node]
    return node


def _to_emit(node: Any) -> Any:
    """A MODEL-sent node, normalized: every code-owned field dropped.

    Spans, derived fact values and normalized mention keys are computed by
    assembly from the cited evidence, and `object_hash` is printed by `render`;
    none of the four is ever read back from a model, so all four are removed
    before the object is held to its schema. `section_heading` deliberately is
    not: it is refused there rather than stripped (rule 1).
    """
    return _strip(node, _CODE_OWNED)


def _record_to_emit(node: Any) -> Any:
    """A node of OUR record, back in emit shape: code-owned and record-only
    fields dropped, so the rebuild feeds `assemble` the emit the record came
    from rather than the record itself."""
    return _strip(node, _RECORD_ONLY)


def _text(path: str, value: Any, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}: expected a non-empty value, got {_excerpt(value)}")


def _schema_defects(
    path: str, validator: jsonschema.Draft202012Validator, obj: Any, errors: list[str]
) -> bool:
    """Hold a model-sent object to its packaged definition; True when it failed.

    Every defect is collected and located, and the walk is ordered so two runs
    over one emit list the same problems in the same order.
    """
    problems = sorted(
        validator.iter_errors(obj), key=lambda e: [str(p) for p in e.absolute_path]
    )
    for problem in problems:
        location = "/".join(str(p) for p in problem.absolute_path)
        errors.append(f"{path}{'/' + location if location else ''}: {problem.message}")
    return bool(problems)


def _op_evidence(path: str, value: Any, blocks: dict[str, Any], errors: list[str],
                 schema_version: str) -> None:
    """Spec §4: "Every addition, replacement, or removal needs source evidence".

    The reference is typed before it is bound: `resolve` reads `text` and
    `occurrence` as a string and an integer, and this is the one thing an
    operation carries that no object schema covers — so an untyped citation
    would leave the binder to raise something that is not a judged defect, and
    a failed repair must always be a judged defect (rule 3).

    Bound leniently after that, the tier audit findings already use: this
    citation justifies a change, it does not become record evidence (the
    object's own references bind exactly, through assembly). What it must prove
    is that the cited words are IN the document.
    """
    if not isinstance(value, list) or not value:
        errors.append(f"{path}: a repair operation needs at least one source reference")
        return
    for i, ref in enumerate(value):
        if not isinstance(ref, dict):
            errors.append(f"{path}[{i}]: expected a reference object, got {_excerpt(ref)}")
            continue
        # a citation copied out of the candidate JSON carries the bound span
        # code printed there: stripped, exactly as an object's fields are
        cited = _to_emit(ref)
        if _schema_defects(
            f"{path}[{i}]", _reference_validator(schema_version), cited, errors
        ):
            continue
        try:
            resolve(cited, blocks, lenient=True)
        except RefBindError as exc:
            errors.append(f"{path}[{i}]: {exc.message}")


def _object(path: str, op: str, kind: str, value: Any, errors: list[str],
            schema_version: str, target: Any = None) -> dict[str, Any] | None:
    if op == "remove":
        if value is not None:
            errors.append(f"{path}.object: a removal carries no object")
        return None
    if not isinstance(value, dict):
        errors.append(
            f"{path}.object: the {op} operation needs the complete {kind}, "
            f"got {_excerpt(value)}"
        )
        return None
    immutable = sorted(_IMMUTABLE & set(value))
    if immutable:
        errors.append(
            f"{path}.object: {', '.join(immutable)} is immutable — document identity, "
            "versions, provenance and quality assessments are not repairable"
        )
        return None
    obj = _to_emit(value)
    broken = _schema_defects(
        f"{path}.object",
        _object_validator(_def_key(kind, target, schema_version), schema_version),
        obj, errors,
    )
    return None if broken else obj


def _locate(
    path: str,
    op: str,
    kind: str,
    node: dict[str, Any],
    record: dict[str, Any],
    obj: dict[str, Any] | None,
    errors: list[str],
    schema_version: str = SCHEMA_VERSION,
) -> tuple[str, int, str] | None:
    """Where the operation lands: (conflict slot, list index, presence family).

    None when it lands nowhere — an unknown target, a stale old-object hash, an
    id already taken. The index is a position in the RECORD's list, which is
    the position in the rebuilt emit: `_to_emit` preserves order.
    """
    target = node.get("target_id")
    old = node.get("old_object_hash")
    if kind == _TRACKS:
        return _locate_tracks(path, op, target, old, record, errors)
    if op == "add":
        if old is not None:
            errors.append(f"{path}.old_object_hash: an addition replaces nothing")
            return None
        if target is not None:
            errors.append(
                f"{path}.target_id: an addition names no target — the new id is the "
                "object's own"
            )
            return None
        if obj is None:
            return None
        new_id = obj.get("id")
        if new_id in _ids(record, kind):
            errors.append(f"{path}.object.id: {_excerpt(new_id)} is already a {kind} id")
            return None
        return f"{kind}:{new_id}", -1, ""
    if not isinstance(old, str) or not _HEX64.fullmatch(old):
        errors.append(
            f"{path}.old_object_hash: a {op} must echo the object's printed hash, "
            f"got {_excerpt(old)}"
        )
        return None
    if kind == "source_assessment":
        if target is not None:
            errors.append(f"{path}.target_id: the source assessment has no id")
            return None
        if object_hash(record["source_assessment"]) != old:
            errors.append(f"{path}.old_object_hash: the source assessment has changed")
            return None
        return "source_assessment", -1, ""
    if kind == "presence":
        if target not in _presence_families(schema_version):
            errors.append(
                f"{path}.target_id: no such fact-presence family: {_excerpt(target)}"
            )
            return None
        family = str(target)
        if object_hash(record["facts"]["presence"][family]) != old:
            errors.append(f"{path}.old_object_hash: presence {family!r} has changed")
            return None
        return f"presence:{family}", -1, family
    items = _list_at(record, kind)
    if kind == "accounting_entry":
        matches = [
            i for i, entry in enumerate(items)
            if isinstance(entry, dict)
            and entry.get("block_id") == target
            and object_hash(entry) == old
        ]
        if len(matches) != 1:
            errors.append(
                f"{path}: {len(matches)} accounting entries for block "
                f"{_excerpt(target)} carry that old_object_hash; exactly one must"
            )
            return None
        if obj is not None and obj.get("block_id") != target:
            errors.append(f"{path}.object.block_id: a replacement keeps its block")
            return None
        return f"{kind}:@{matches[0]}", matches[0], ""
    index = _ids(record, kind).get(target) if isinstance(target, str) else None
    if index is None:
        errors.append(f"{path}.target_id: no {kind} with id {_excerpt(target)}")
        return None
    if object_hash(items[index]) != old:
        errors.append(
            f"{path}.old_object_hash: {kind} {_excerpt(target)} is not the object "
            "this operation was written against"
        )
        return None
    if obj is not None and obj.get("id") != target:
        errors.append(
            f"{path}.object.id: a replacement keeps the id it replaces "
            f"({_excerpt(target)}); renaming orphans every link to it"
        )
        return None
    return f"{kind}:{target}", index, ""


def _locate_tracks(
    path: str, op: str, target: Any, old: Any, record: dict[str, Any], errors: list[str]
) -> tuple[str, int, str] | None:
    """Schema 4's `relations.tracks`: one nullable object with no id.

    An addition fills a null (anything else would overwrite tracks the model
    never echoed a hash for); a replacement or removal needs tracks to exist
    and the hash printed inside them. The slot is the same for all three, so
    two operations on tracks conflict.
    """
    if target is not None:
        errors.append(f"{path}.target_id: tracks have no id — target_id is null")
        return None
    current = (record.get("relations") or {}).get(_TRACKS)
    if op == "add":
        if old is not None:
            errors.append(f"{path}.old_object_hash: an addition replaces nothing")
            return None
        if current is not None:
            errors.append(
                f"{path}: the candidate already has tracks — replace them under their "
                "printed object_hash"
            )
            return None
        return _TRACKS, -1, ""
    if current is None:
        errors.append(f"{path}: the candidate has no tracks to {op} — add them instead")
        return None
    if not isinstance(old, str) or object_hash(current) != old:
        errors.append(f"{path}.old_object_hash: tracks have changed")
        return None
    return _TRACKS, -1, ""


def _operation(
    path: str,
    node: Any,
    record: dict[str, Any],
    blocks: dict[str, Any],
    slots: set[str],
    errors: list[str],
    schema_version: str,
) -> _Op | None:
    if not isinstance(node, dict):
        errors.append(f"{path}: expected a repair operation object, got {_excerpt(node)}")
        return None
    op, kind = node.get("op"), node.get("kind")
    _text(f"{path}.finding_id", node.get("finding_id"), errors)
    _text(f"{path}.reason", node.get("reason"), errors)
    _op_evidence(f"{path}.evidence", node.get("evidence"), blocks, errors, schema_version)
    if not isinstance(op, str) or op not in OPS:
        errors.append(f"{path}.op: unknown operation {_excerpt(op)}")
        return None
    if not isinstance(kind, str) or kind not in _kinds(schema_version):
        errors.append(f"{path}.kind: unknown object kind {_excerpt(kind)}")
        return None
    if kind in _REPLACE_ONLY and op != "replace":
        errors.append(f"{path}.op: a {kind} is part of the record — it can only be replaced")
        return None
    obj = _object(path, op, kind, node.get("object"), errors, schema_version,
                  node.get("target_id"))
    placed = _locate(path, op, kind, node, record, obj, errors, schema_version)
    if placed is None:
        return None
    slot, index, family = placed
    if slot in slots:
        errors.append(f"{path}: conflicts with an earlier operation on the same object")
        return None
    slots.add(slot)
    if obj is None and op != "remove":
        return None
    return _Op(op=op, kind=kind, index=index, family=family, object=obj)


def _operations(
    value: Any, record: dict[str, Any], blocks: dict[str, Any], errors: list[str],
    schema_version: str,
) -> list[_Op]:
    if not isinstance(value, list):
        errors.append(f"operations: expected a list, got {_excerpt(value)}")
        return []
    if not value:
        errors.append("operations: a repair with no operations changes nothing")
        return []
    slots: set[str] = set()
    resolved: list[_Op] = []
    for i, node in enumerate(value):
        one = _operation(
            f"operations[{i}]", node, record, blocks, slots, errors, schema_version
        )
        if one is not None:
            resolved.append(one)
    return resolved


def _rebuild(record: dict[str, Any], ops: list[_Op]) -> dict[str, Any]:
    """The base record back in emit shape, with every operation applied.

    Removals and replacements are resolved positionally in one pass per
    collection, so no index shifts under an earlier deletion, and additions are
    appended in the order the model sent them.
    """
    working: dict[str, Any] = {key: _record_to_emit(record[key]) for key in _EMIT_KEYS}
    for kind in _LISTS:
        kind_ops = [o for o in ops if o.kind == kind]
        if not kind_ops:
            continue
        removed = {o.index for o in kind_ops if o.op == "remove"}
        replaced = {o.index: o.object for o in kind_ops if o.op == "replace"}
        rebuilt: list[Any] = [
            replaced.get(i, item)
            for i, item in enumerate(_list_at(working, kind))
            if i not in removed
        ]
        rebuilt += [o.object for o in kind_ops if o.op == "add"]
        _set_list(working, kind, rebuilt)
    for one in ops:
        if one.kind == "presence":
            working["facts"]["presence"][one.family] = one.object
        elif one.kind == "source_assessment":
            working["source_assessment"] = one.object
        elif one.kind == _TRACKS:  # schema 4: a removal puts null back
            working["relations"][_TRACKS] = one.object
    return working


def apply(
    emit: dict[str, Any], record: dict[str, Any], markdown: str, candidate_hash: str
) -> dict[str, Any]:
    """Validate one repair emit against the candidate it claims to repair, and
    return the repaired candidate.

    Valid means: the emit repairs THIS candidate, every operation addresses an
    object the candidate actually holds under the hash it was read at, no two
    operations touch the same object, every object is a contract-valid object
    of its kind carrying no code-owned or immutable field, and every operation
    carries a finding id, cited source evidence and a reason — removals
    included. The repaired emit is then assembled exactly as a fresh
    extraction is: references bound, facts derived, presence reconciled,
    quality reassessed from the (possibly repaired) source assessment, and a
    new candidate hash computed with `parent_candidate_hash` pointing at the
    base.

    Document identity, the extractor's provenance and the prompt/schema
    versions are carried through from the base record: a repair produces
    another candidate in the same extraction's lineage, not a new extraction.
    The record's own `schema_version` is therefore what every operation is
    checked against and what the repaired candidate is rebuilt into — a
    schema-2 candidate out of the archive takes schema-2 operations and comes
    back a schema-2 record (spec §7: migration is offline, and until it has
    run both shapes are live).

    Anything else raises `RepairJudgeError` with the whole defect list, and
    the base record is left exactly as it was found.
    """
    if not isinstance(emit, dict):
        raise RepairJudgeError([f"<root>: expected a repair object, got {_excerpt(emit)}"])
    errors: list[str] = []
    extraction = record.get("extraction")
    extraction = extraction if isinstance(extraction, dict) else {}
    if extraction.get("candidate_hash") != candidate_hash:
        errors.append(
            f"record: holds candidate {_excerpt(extraction.get('candidate_hash'))}, "
            f"not {_excerpt(candidate_hash)}"
        )
    if emit.get("base_candidate_hash") != candidate_hash:
        errors.append(
            f"base_candidate_hash: repairs {_excerpt(emit.get('base_candidate_hash'))}, "
            f"not this candidate {_excerpt(candidate_hash)}"
        )
    # a record the rebuild cannot round-trip is a judged failure, not a crash
    # inside the phase: the caller archives this exactly as it archives a bad
    # operation list, and settles the base candidate.
    schema_version = _schema_version_of(record)
    missing = [key for key in _EMIT_KEYS if key not in record]
    if missing:
        errors.append(
            f"record: not a schema-{schema_version} record; missing {', '.join(missing)}"
        )
    if errors:
        raise RepairJudgeError(errors)
    blocks = blocks_by_id(annotate(markdown))
    ops = _operations(emit.get("operations"), record, blocks, errors, schema_version)
    if errors:
        raise RepairJudgeError(errors)
    document = record.get("document")
    document = document if isinstance(document, dict) else {}
    try:
        return assemble(
            _rebuild(record, ops),
            markdown,
            document_hash=str(document.get("document_hash")),
            observed_model=str(extraction.get("model")),
            at=str(extraction.get("at")),
            normalizer_version=str(document.get("normalizer_version")),
            prompt_version=str(extraction.get("prompt_version")),
            parent_candidate_hash=candidate_hash,
            schema_version=schema_version,
        )
    except AssembleError as exc:
        raise RepairJudgeError(
            [f"repaired candidate: {message}" for message in exc.errors]
        ) from None
