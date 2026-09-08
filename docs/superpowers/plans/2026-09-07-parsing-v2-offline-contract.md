# Parsing v2 Increment 1 — Offline Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Tasks are also sized for dispatch to a Codex CLI worker: each task's Files/Interfaces/Steps blocks are a complete, self-contained brief.

**Goal:** Build the v2 semantic contract offline — versioned schemas, source
block annotation, typed statements/relations/facts, deterministic derivation
and verification, pure quality/projection policy — and encode the audit's
twelve cases as regression contracts. Zero model calls, zero database or
archive I/O, zero changes to the v1 production path except one v1 grammar
repair that ships first as validator/9.

**Architecture:** A new pure sub-package `src/jobhunter/l2/v2/` mirroring the
v1 layering: `source.py` (block annotation `blocks/1` + reference binding),
`types.py` (closed enums + typed derivation results), `facts.py` (versioned
derivation grammars, `VALIDATOR_VERSION = "10"`), `assemble.py` (emit→record,
collect-all-errors), `verify.py` (pure checks over (record, markdown), v2
finding codes), `quality.py` (the seven quality dimensions + `search_eligible`
policy), `project.py` (pure mention/statement row projection). Schemas land as
`schemas_data/2/{emit,record}.schema.json` served by the existing
version-parameterized loader. The runner, store, CLI, and MCP are untouched
(increments 2–3).

**Tech Stack:** Python ≥ 3.12, uv, jsonschema, pytest, mypy --strict, ruff
(line 100). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-07-parsing-contract-v2-design.md`
(approved, amended [A1]–[A4]). Audit evidence:
`docs/2026-09-06-l2-data-quality-audit.md`; local case data:
`data/l2-audit-2026-09-06/casebook.json` (present on this machine, gitignored).

## Roadmap (spec §10)

| increment | scope | plan |
| --- | --- | --- |
| **1 (this plan)** | offline contract: v1 floor repair (validator/9), schemas `2`, `l2/v2/` pure modules (validator/10), twelve case contracts, synthetic minimal pairs | this document |
| 2 | extraction-quality harness: prompts `demand-profile/v6` + `semantic-audit/v1` + `semantic-repair/v1`, runner bundle selection ([A2]), archive-outside-transaction restructuring ([A1]), recorded codex-cli responses, audit/repair artifacts, quality settlement, bounded benchmark | own plan when this merges |
| 3 | persistence + explicit reads: additive migration (needs approval), v2 mention table, live/replay parity, `q profile --schema 2`, MCP `profile_v2`/`claims_v2`, shadow rollout | own plan |

Engine note: live model calls belong to increment 2 and use the existing
`codex-cli` engine (free tier) per decision D-20260907-H9K8; nothing in this
plan invokes any engine.

## Global Constraints

- Python ≥ 3.12; uv only (`uv run pytest`, `uv run ruff check .`, `uv run mypy`); ruff line 100; `mypy --strict`.
- `l2/v2/` is pure: no I/O, no network, no env reads, no LLM, no imports from `store/`, `config`, or `runner`.
- Offsets are Unicode codepoints, half-open `[start, end)`; `markdown[s:e] == text` always; no fuzzy repair anywhere.
- Hashing only via `jobhunter.hashing` (`canonical_json`, `sha256_hex`); time never read (timestamps are inputs).
- Identifiers freeze at the end of this plan: annotation `blocks/1`, schema `2`, rules `parsing-rules/2`, v1 validator `9`, v2 validator `10`. Never edit a frozen artifact in place — bump.
- Code assigns block IDs, spans, derived values, parse states, quality, candidate hashes. The model's emit never contains any of them (spec §3 "code-owned").
- Null-over-guess governs every grammar branch: unknown language → `present_unparsed`, never a value; `$` alone never implies a currency; ambiguous numeric dates stay ambiguous.
- Work in an isolated worktree on branch `l2/parsing-v2-offline` (this workspace carries unrelated changes); commit per task; never commit `data/` or audit scratch files (`git status` before every commit).
- V1 behavior changes are confined to Task 1. Nothing else in this plan may alter any `l2/*.py` file outside `l2/v2/`, `schemas_data/`, and `transforms.py`.

---

### Task 1: v1 floor grammar repair — validator/9

The audit's defect 1 (C01): `parse_experience_months("a minimum of 8 years of
experience")` returns `{"min": 96, "max": 96}` because `_FLOOR` only knows
`N+/N or more` and `at least N`. 72 validated records carry a false exact
interval. Fix the grammar, bump the validator.

Semantics note (documented in code): the v1 record schema has only integer
`min`/`max`, so "more than 8 years" (a strict floor, >96) is stored as the
inclusive `{"min": 96, "max": null}` — it under-states the floor by less than
a year and invents no upper bound. V2 represents `gt` exactly (Task 5).

**Files:**
- Modify: `src/jobhunter/l2/transforms.py` (lines 19–27: `VALIDATOR_VERSION`, `_FLOOR`)
- Test: `tests/l2/test_transforms.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `parse_experience_months` floor behavior for "minimum/more than/over"; `VALIDATOR_VERSION == "9"`. `TRANSFORMS` re-keys automatically (it is keyed off the constant).

- [ ] **Step 1: Write the failing tests** — append to `tests/l2/test_transforms.py`:

```python
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a minimum of 8 years of experience", {"min": 96, "max": None}),
        ("Minimum 5 years in consulting", {"min": 60, "max": None}),
        ("minimum of 10 years", {"min": 120, "max": None}),
        ("more than 8 years of experience", {"min": 96, "max": None}),
        ("over 12 years", {"min": 144, "max": None}),
        # regressions: exact stays exact, ranges stay ranges
        ("5 years of experience", {"min": 60, "max": 60}),
        ("5-7 years", {"min": 60, "max": 84}),
        ("at least 8 years of experience", {"min": 96, "max": None}),
        ("8+ years", {"min": 96, "max": None}),
    ],
)
def test_experience_floor_wordings_validator9(
    text: str, expected: dict[str, object] | None
) -> None:
    assert parse_experience_months(text) == expected


def test_validator_version_is_9() -> None:
    # the grammar changed; stored validator/8 rows keep their meaning
    assert VALIDATOR_VERSION == "9"
    assert VALIDATOR_VERSION in TRANSFORMS
```

(Imports `VALIDATOR_VERSION`, `TRANSFORMS` — already imported at the top of the file; add if missing.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/l2/test_transforms.py -k "validator9 or version_is_9" -v`
Expected: FAIL — `{"min": 96, "max": 96}` for the minimum/more-than/over cases; version assertion fails on `"8"`.

- [ ] **Step 3: Implement** — in `src/jobhunter/l2/transforms.py` replace the version constant and `_FLOOR`:

```python
# validator/7: the possible_omission completeness warning (verify._check_omissions)
# validator/8: "at least N years" is a floor; omission scan skips boilerplate lines
# validator/9: "minimum (of) N", "more than N", "over N" are floors (audit 2026-09-06
# defect 1, C01: 72 validated rows held a false exact interval). The v1 record
# cannot express a strict floor, so "more than N" maps to the inclusive
# {"min": N*12, "max": null} — conservative, no invented upper bound; v2's
# comparison operators represent gt exactly.
VALIDATOR_VERSION = "9"

_FLOOR = re.compile(
    r"(?:(\d+)\s*(?:\+|or\s+more)"
    r"|\b(?:at\s+least|a\s+minimum\s+of|minimum\s+of|minimum|more\s+than|over)\s+(\d+))"
    r"\s*(?:years?|yrs?|yoe)\b",
    re.IGNORECASE,
)  # the leading \b keeps "turnover 5 years" from matching the "over" branch
```

`parse_experience_months` itself is unchanged (`int(m.group(1) or m.group(2)) * 12` still holds: group 1 is the `N+` branch, group 2 the worded-floor branch).

- [ ] **Step 4: Run the full l2 suite**

Run: `uv run pytest tests/l2/ -v`
Expected: PASS, including the existing validator-bump guard test (`test_validator_version_bumped_for_the_grammar_change`) — if that test pins "8", update its pinned value to "9" in the same commit; it exists to force this conscious step.

- [ ] **Step 5: Gates and commit**

Run: `uv run ruff check . && uv run mypy`
```bash
git add src/jobhunter/l2/transforms.py tests/l2/test_transforms.py
git commit -m "feat(l2): validator/9 — minimum/more-than/over are floors, not exact intervals"
```

---

### Task 2: Schema bundle 2 — `emit` and `record` JSON Schemas

The wire contract. Emit is what the model returns; record is emit plus
code-owned resolution and derivation. Both are closed (`additionalProperties:
false` everywhere, no free-form objects — the v1 `claim.threshold` "any
object" bridge has no v2 equivalent).

Design decisions locked here (spec §3, choices this plan makes concrete):
- A reference's `text`/`occurrence` nullability pair (whole-block selection) is
  enforced by binding code, not schema `anyOf` cross-constraints — keeps
  `strict_schema()` transformation trivial.
- Family-specific fact fields are one closed shape with nullable fields
  (`scope`, `date_kind`, `component`); family-appropriateness is a verify
  check. One shape is strict-mode friendly and simpler for the model.
- `component` (base/total/bonus/equity/unspecified) and `scope.kind` are
  model-emitted classifications with mandatory evidence — they are labels, not
  derived numbers; the increment-2 auditor reviews them. Numeric values,
  comparisons, inclusivity, units, and parse states remain code-owned.

**Files:**
- Create: `src/jobhunter/l2/schemas_data/2/emit.schema.json`
- Create: `src/jobhunter/l2/schemas_data/2/record.schema.json`
- Test: `tests/l2/v2/test_schemas2.py` (create `tests/l2/v2/__init__.py`, empty)

**Interfaces:**
- Consumes: `jobhunter.l2.schemas` (`emit_schema`, `record_schema`, `validate_emit`, `validate_record`, `strict_schema`, `normalize_emit`) — already version-parameterized; no changes to `schemas.py`.
- Produces: version string `"2"` resolvable by the loader; the field names every later task uses.

- [ ] **Step 1: Write `emit.schema.json`** — full content:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "job-hunter L2 emit schema (what the LLM returns), schema_version 2",
  "type": "object",
  "additionalProperties": false,
  "required": ["source_assessment", "statements", "relations", "facts",
               "mentions", "areas", "block_accounting"],
  "$defs": {
    "id": { "type": "string", "pattern": "^[A-Za-z0-9_-]{1,40}$" },
    "reference": {
      "type": "object",
      "additionalProperties": false,
      "required": ["block_id", "text", "occurrence"],
      "properties": {
        "block_id": { "type": "string", "pattern": "^b[0-9]{6}$" },
        "text": { "type": ["string", "null"], "minLength": 1 },
        "occurrence": { "type": ["integer", "null"], "minimum": 0 }
      },
      "description": "text=null AND occurrence=null selects the whole block; otherwise text is an exact substring of the block and occurrence indexes its occurrences within the block. Binding enforces the pairing."
    },
    "evidence": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/reference" } },
    "importance": { "enum": ["required", "preferred", "not_required", "unstated", "ambiguous"] },
    "polarity": { "enum": ["positive", "negative", "ambiguous"] },
    "proficiency": { "enum": ["expert", "proficient", "working", "exposure", null] },
    "unresolved_issue": {
      "type": "object",
      "additionalProperties": false,
      "required": ["reason", "evidence"],
      "properties": {
        "reason": { "type": "string", "minLength": 1, "maxLength": 300 },
        "evidence": { "$ref": "#/$defs/evidence" }
      }
    },
    "statement": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "kind", "subject", "topic", "evidence", "importance",
                   "importance_evidence", "polarity", "polarity_evidence",
                   "proficiency", "proficiency_evidence", "condition_ids",
                   "fact_ids", "unresolved"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "kind": { "enum": ["qualification", "responsibility", "employment_constraint",
                            "compensation_statement", "hiring_policy", "employer_context"] },
        "subject": { "enum": ["candidate", "employer", "role", "unstated"] },
        "topic": { "type": "string", "minLength": 1, "maxLength": 80 },
        "evidence": { "$ref": "#/$defs/evidence" },
        "importance": { "anyOf": [{ "$ref": "#/$defs/importance" }, { "type": "null" }] },
        "importance_evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
        "polarity": { "$ref": "#/$defs/polarity" },
        "polarity_evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
        "proficiency": { "$ref": "#/$defs/proficiency" },
        "proficiency_evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
        "condition_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "fact_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "unresolved": { "type": "array", "items": { "$ref": "#/$defs/unresolved_issue" } }
      }
    },
    "group": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "operator", "members", "evidence"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "operator": { "enum": ["all_of", "any_of", "unresolved"] },
        "members": { "type": "array", "minItems": 2, "items": { "$ref": "#/$defs/id" } },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
      },
      "description": "members reference statement or group ids; all_of/any_of require connective evidence (verify enforces); unresolved marks ambiguous coordination."
    },
    "condition": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "kind", "evidence", "statement_ids", "fact_ids"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "kind": { "enum": ["qualification_route", "role_level", "geography",
                            "employment_type", "schedule", "other"] },
        "evidence": { "$ref": "#/$defs/evidence" },
        "statement_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "fact_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } }
      }
    },
    "example_set": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "parent_statement_id", "mention_ids", "exhaustive", "evidence"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "parent_statement_id": { "$ref": "#/$defs/id" },
        "mention_ids": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/id" } },
        "exhaustive": { "type": "boolean" },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
      }
    },
    "presence": {
      "type": "object",
      "additionalProperties": false,
      "required": ["state", "evidence"],
      "properties": {
        "state": { "enum": ["stated", "none_found", "explicitly_absent", "unresolved"] },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
      },
      "description": "explicitly_absent and unresolved require evidence (verify enforces); none_found means the model searched and found nothing."
    },
    "fact_entry": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "family", "statement_ids", "condition_ids", "scope",
                   "date_kind", "component", "evidence"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "family": { "enum": ["experience", "compensation", "quantity", "date"] },
        "statement_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "condition_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "scope": {
          "anyOf": [
            {
              "type": "object",
              "additionalProperties": false,
              "required": ["kind", "evidence"],
              "properties": {
                "kind": { "enum": ["overall", "management", "domain", "unstated"] },
                "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
              }
            },
            { "type": "null" }
          ]
        },
        "date_kind": { "enum": ["application_deadline", "interview_date", "other", null] },
        "component": { "enum": ["base", "total", "bonus", "equity", "unspecified", null] },
        "evidence": {
          "type": "object",
          "additionalProperties": false,
          "required": ["value", "comparison", "unit", "currency", "component", "applicability"],
          "properties": {
            "value": { "$ref": "#/$defs/evidence" },
            "comparison": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
            "unit": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
            "currency": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
            "component": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
            "applicability": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
          }
        }
      }
    },
    "mention": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "surface", "evidence", "statement_ids", "role"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "surface": { "type": "string", "minLength": 1, "maxLength": 120 },
        "evidence": { "$ref": "#/$defs/reference" },
        "statement_ids": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/id" } },
        "role": { "enum": ["direct", "example", "contextual"] }
      }
    },
    "area": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "name", "kind", "statement_ids", "evidence"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "name": { "type": "string", "minLength": 1, "maxLength": 80 },
        "kind": { "enum": ["technical", "capability", "trait", "credential", "constraint"] },
        "statement_ids": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/id" } },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
      },
      "description": "Presentation only. No importance, no proficiency, no operators."
    },
    "accounting_entry": {
      "type": "object",
      "additionalProperties": false,
      "required": ["block_id", "disposition", "ref_ids", "exclusion_reason", "evidence"],
      "properties": {
        "block_id": { "type": "string", "pattern": "^b[0-9]{6}$" },
        "disposition": { "enum": ["statements", "facts", "context", "excluded", "unresolved"] },
        "ref_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "exclusion_reason": { "enum": ["eeo", "benefits", "employer_description",
                                        "contact_privacy_admin", "navigation", null] },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
      },
      "description": "One or more entries per nonempty block; mixed blocks repeat the block_id with different dispositions. excluded requires exclusion_reason; statements/facts require ref_ids."
    }
  },
  "properties": {
    "source_assessment": {
      "type": "object",
      "additionalProperties": false,
      "required": ["usability", "evidence", "note"],
      "properties": {
        "usability": { "enum": ["usable", "partial", "placeholder", "empty", "unsupported"] },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
        "note": { "type": ["string", "null"], "maxLength": 300 }
      }
    },
    "statements": { "type": "array", "items": { "$ref": "#/$defs/statement" } },
    "relations": {
      "type": "object",
      "additionalProperties": false,
      "required": ["groups", "conditions", "example_sets"],
      "properties": {
        "groups": { "type": "array", "items": { "$ref": "#/$defs/group" } },
        "conditions": { "type": "array", "items": { "$ref": "#/$defs/condition" } },
        "example_sets": { "type": "array", "items": { "$ref": "#/$defs/example_set" } }
      }
    },
    "facts": {
      "type": "object",
      "additionalProperties": false,
      "required": ["presence", "entries"],
      "properties": {
        "presence": {
          "type": "object",
          "additionalProperties": false,
          "required": ["experience", "compensation", "quantities", "dates"],
          "properties": {
            "experience": { "$ref": "#/$defs/presence" },
            "compensation": { "$ref": "#/$defs/presence" },
            "quantities": { "$ref": "#/$defs/presence" },
            "dates": { "$ref": "#/$defs/presence" }
          }
        },
        "entries": { "type": "array", "items": { "$ref": "#/$defs/fact_entry" } }
      }
    },
    "mentions": { "type": "array", "items": { "$ref": "#/$defs/mention" } },
    "areas": { "type": "array", "items": { "$ref": "#/$defs/area" } },
    "block_accounting": { "type": "array", "items": { "$ref": "#/$defs/accounting_entry" } }
  }
}
```

- [ ] **Step 2: Write `record.schema.json`** — the record is the emit shape with every `reference` replaced by a bound quote, plus code-owned blocks. Full content:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "job-hunter L2 extraction record, schema_version 2",
  "type": "object",
  "additionalProperties": false,
  "required": ["document", "source_assessment", "statements", "relations", "facts",
               "mentions", "areas", "block_accounting", "extraction", "quality"],
  "$defs": {
    "id": { "type": "string", "pattern": "^[A-Za-z0-9_-]{1,40}$" },
    "span": {
      "type": "array", "minItems": 2, "maxItems": 2,
      "prefixItems": [{ "type": "integer", "minimum": 0 }, { "type": "integer", "minimum": 1 }],
      "items": false,
      "description": "Half-open [start,end) in Unicode CODEPOINTS into the canonical markdown whose UTF-8 encoding hashes to document_hash."
    },
    "bound_ref": {
      "type": "object",
      "additionalProperties": false,
      "required": ["block_id", "text", "span", "occurrence"],
      "properties": {
        "block_id": { "type": "string", "pattern": "^b[0-9]{6}$" },
        "text": { "type": "string", "minLength": 1 },
        "span": { "$ref": "#/$defs/span" },
        "occurrence": { "type": "integer", "minimum": 0 }
      },
      "description": "Materialized evidence: text == markdown[span); occurrence is the index of text among its occurrences WITHIN the block. A whole-block reference materializes the block's own text/span with occurrence 0."
    },
    "evidence": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/bound_ref" } },
    "importance": { "enum": ["required", "preferred", "not_required", "unstated", "ambiguous"] },
    "polarity": { "enum": ["positive", "negative", "ambiguous"] },
    "proficiency": { "enum": ["expert", "proficient", "working", "exposure", null] },
    "unresolved_issue": {
      "type": "object", "additionalProperties": false,
      "required": ["reason", "evidence"],
      "properties": {
        "reason": { "type": "string", "minLength": 1, "maxLength": 300 },
        "evidence": { "$ref": "#/$defs/evidence" }
      }
    },
    "statement": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "kind", "subject", "topic", "evidence", "importance",
                   "importance_evidence", "polarity", "polarity_evidence",
                   "proficiency", "proficiency_evidence", "condition_ids",
                   "fact_ids", "unresolved"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "kind": { "enum": ["qualification", "responsibility", "employment_constraint",
                            "compensation_statement", "hiring_policy", "employer_context"] },
        "subject": { "enum": ["candidate", "employer", "role", "unstated"] },
        "topic": { "type": "string", "minLength": 1, "maxLength": 80 },
        "evidence": { "$ref": "#/$defs/evidence" },
        "importance": { "anyOf": [{ "$ref": "#/$defs/importance" }, { "type": "null" }] },
        "importance_evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
        "polarity": { "$ref": "#/$defs/polarity" },
        "polarity_evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
        "proficiency": { "$ref": "#/$defs/proficiency" },
        "proficiency_evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
        "condition_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "fact_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "unresolved": { "type": "array", "items": { "$ref": "#/$defs/unresolved_issue" } }
      }
    },
    "group": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "operator", "members", "evidence"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "operator": { "enum": ["all_of", "any_of", "unresolved"] },
        "members": { "type": "array", "minItems": 2, "items": { "$ref": "#/$defs/id" } },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
      }
    },
    "condition": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "kind", "evidence", "statement_ids", "fact_ids"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "kind": { "enum": ["qualification_route", "role_level", "geography",
                            "employment_type", "schedule", "other"] },
        "evidence": { "$ref": "#/$defs/evidence" },
        "statement_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "fact_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } }
      }
    },
    "example_set": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "parent_statement_id", "mention_ids", "exhaustive", "evidence"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "parent_statement_id": { "$ref": "#/$defs/id" },
        "mention_ids": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/id" } },
        "exhaustive": { "type": "boolean" },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
      }
    },
    "presence": {
      "type": "object", "additionalProperties": false,
      "required": ["state", "evidence"],
      "properties": {
        "state": { "enum": ["stated", "none_found", "explicitly_absent", "unresolved"] },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
      }
    },
    "derived_quantity": {
      "type": "object", "additionalProperties": false,
      "required": ["dimension", "comparison", "min_value", "max_value",
                   "inclusive_min", "inclusive_max", "unit"],
      "properties": {
        "dimension": { "enum": ["duration", "count", "percentage", "frequency"] },
        "comparison": { "enum": ["gte", "gt", "lte", "lt", "eq", "range", "unstated"] },
        "min_value": { "type": ["number", "null"] },
        "max_value": { "type": ["number", "null"] },
        "inclusive_min": { "type": ["boolean", "null"] },
        "inclusive_max": { "type": ["boolean", "null"] },
        "unit": { "enum": ["month", "percent", "per_week", "per_month", "per_day", null] }
      }
    },
    "derived_money": {
      "type": "object", "additionalProperties": false,
      "required": ["comparison", "min_amount", "max_amount", "currency", "period"],
      "properties": {
        "comparison": { "enum": ["gte", "gt", "lte", "lt", "eq", "range", "unstated"] },
        "min_amount": { "type": ["string", "null"], "pattern": "^[0-9]+(\\.[0-9]{1,2})?$" },
        "max_amount": { "type": ["string", "null"], "pattern": "^[0-9]+(\\.[0-9]{1,2})?$" },
        "currency": { "type": ["string", "null"], "pattern": "^[A-Z]{3}$" },
        "period": { "enum": ["year", "month", "week", "day", "hour", null] }
      },
      "description": "Amounts are decimal strings — cents are never discarded."
    },
    "derived_date": {
      "type": "object", "additionalProperties": false,
      "required": ["date", "candidates"],
      "properties": {
        "date": { "type": ["string", "null"], "format": "date" },
        "candidates": {
          "anyOf": [{ "type": "array", "minItems": 2,
                      "items": { "type": "string", "format": "date" } },
                    { "type": "null" }]
        }
      },
      "description": "Ambiguous locale dates: date null, candidates listed."
    },
    "derived": {
      "type": "object", "additionalProperties": false,
      "required": ["state", "quantity", "money", "date"],
      "properties": {
        "state": { "enum": ["parsed", "present_unparsed", "ambiguous", "conflicting"] },
        "quantity": { "anyOf": [{ "$ref": "#/$defs/derived_quantity" }, { "type": "null" }] },
        "money": { "anyOf": [{ "$ref": "#/$defs/derived_money" }, { "type": "null" }] },
        "date": { "anyOf": [{ "$ref": "#/$defs/derived_date" }, { "type": "null" }] }
      }
    },
    "fact_entry": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "family", "statement_ids", "condition_ids", "scope",
                   "date_kind", "component", "evidence", "derived"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "family": { "enum": ["experience", "compensation", "quantity", "date"] },
        "statement_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "condition_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "scope": {
          "anyOf": [
            { "type": "object", "additionalProperties": false,
              "required": ["kind", "evidence"],
              "properties": {
                "kind": { "enum": ["overall", "management", "domain", "unstated"] },
                "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
              } },
            { "type": "null" }
          ]
        },
        "date_kind": { "enum": ["application_deadline", "interview_date", "other", null] },
        "component": { "enum": ["base", "total", "bonus", "equity", "unspecified", null] },
        "evidence": {
          "type": "object", "additionalProperties": false,
          "required": ["value", "comparison", "unit", "currency", "component", "applicability"],
          "properties": {
            "value": { "$ref": "#/$defs/evidence" },
            "comparison": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
            "unit": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
            "currency": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
            "component": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
            "applicability": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
          }
        },
        "derived": { "$ref": "#/$defs/derived" }
      }
    },
    "mention": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "surface", "evidence", "statement_ids", "role", "normalized_key"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "surface": { "type": "string", "minLength": 1, "maxLength": 120 },
        "evidence": { "$ref": "#/$defs/bound_ref" },
        "statement_ids": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/id" } },
        "role": { "enum": ["direct", "example", "contextual"] },
        "normalized_key": { "type": "string", "minLength": 1 }
      }
    },
    "area": {
      "type": "object", "additionalProperties": false,
      "required": ["id", "name", "kind", "statement_ids", "evidence"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "name": { "type": "string", "minLength": 1, "maxLength": 80 },
        "kind": { "enum": ["technical", "capability", "trait", "credential", "constraint"] },
        "statement_ids": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/id" } },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
      }
    },
    "accounting_entry": {
      "type": "object", "additionalProperties": false,
      "required": ["block_id", "disposition", "ref_ids", "exclusion_reason", "evidence"],
      "properties": {
        "block_id": { "type": "string", "pattern": "^b[0-9]{6}$" },
        "disposition": { "enum": ["statements", "facts", "context", "excluded", "unresolved"] },
        "ref_ids": { "type": "array", "items": { "$ref": "#/$defs/id" } },
        "exclusion_reason": { "enum": ["eeo", "benefits", "employer_description",
                                        "contact_privacy_admin", "navigation", null] },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] }
      }
    }
  },
  "properties": {
    "document": {
      "type": "object", "additionalProperties": false,
      "required": ["document_hash", "normalizer_version", "annotation_version"],
      "properties": {
        "document_hash": { "type": "string", "pattern": "^[0-9a-f]{64}$" },
        "normalizer_version": { "type": "string" },
        "annotation_version": { "type": "string" }
      }
    },
    "source_assessment": {
      "type": "object", "additionalProperties": false,
      "required": ["usability", "evidence", "note"],
      "properties": {
        "usability": { "enum": ["usable", "partial", "placeholder", "empty", "unsupported"] },
        "evidence": { "anyOf": [{ "$ref": "#/$defs/evidence" }, { "type": "null" }] },
        "note": { "type": ["string", "null"], "maxLength": 300 }
      }
    },
    "statements": { "type": "array", "items": { "$ref": "#/$defs/statement" } },
    "relations": {
      "type": "object", "additionalProperties": false,
      "required": ["groups", "conditions", "example_sets"],
      "properties": {
        "groups": { "type": "array", "items": { "$ref": "#/$defs/group" } },
        "conditions": { "type": "array", "items": { "$ref": "#/$defs/condition" } },
        "example_sets": { "type": "array", "items": { "$ref": "#/$defs/example_set" } }
      }
    },
    "facts": {
      "type": "object", "additionalProperties": false,
      "required": ["presence", "entries"],
      "properties": {
        "presence": {
          "type": "object", "additionalProperties": false,
          "required": ["experience", "compensation", "quantities", "dates"],
          "properties": {
            "experience": { "$ref": "#/$defs/presence" },
            "compensation": { "$ref": "#/$defs/presence" },
            "quantities": { "$ref": "#/$defs/presence" },
            "dates": { "$ref": "#/$defs/presence" }
          }
        },
        "entries": { "type": "array", "items": { "$ref": "#/$defs/fact_entry" } }
      }
    },
    "mentions": { "type": "array", "items": { "$ref": "#/$defs/mention" } },
    "areas": { "type": "array", "items": { "$ref": "#/$defs/area" } },
    "block_accounting": { "type": "array", "items": { "$ref": "#/$defs/accounting_entry" } },
    "extraction": {
      "type": "object", "additionalProperties": false,
      "required": ["model", "prompt_version", "schema_version", "validator_version",
                   "rules_version", "at", "candidate_hash", "parent_candidate_hash"],
      "properties": {
        "model": { "type": "string" },
        "prompt_version": { "type": "string" },
        "schema_version": { "type": "string" },
        "validator_version": { "type": "string" },
        "rules_version": { "type": "string" },
        "at": { "type": "string" },
        "candidate_hash": { "type": "string", "pattern": "^[0-9a-f]{64}$" },
        "parent_candidate_hash": { "type": ["string", "null"], "pattern": "^[0-9a-f]{64}$" },
        "cost_usd": { "type": ["number", "null"] }
      }
    },
    "quality": {
      "type": "object", "additionalProperties": false,
      "required": ["source", "evidence", "semantics", "completeness", "sampling",
                   "human_review", "search_eligible"],
      "properties": {
        "source": { "enum": ["usable", "partial", "placeholder", "empty", "unsupported"] },
        "evidence": { "enum": ["pass", "fail", "not_checked"] },
        "semantics": { "enum": ["no_findings", "findings", "not_checked", "error"] },
        "completeness": { "enum": ["no_findings", "findings", "not_checked", "error"] },
        "sampling": { "enum": ["not_requested", "complete", "incomplete", "disagreement"] },
        "human_review": { "enum": ["none", "accepted", "rejected"] },
        "search_eligible": { "type": "boolean" }
      }
    }
  }
}
```

- [ ] **Step 3: Write the failing tests** — `tests/l2/v2/test_schemas2.py`:

```python
"""Schema bundle 2 loads through the existing version-parameterized loader."""

from jobhunter.l2.schemas import (
    emit_schema,
    normalize_emit,
    record_schema,
    strict_schema,
    validate_emit,
)

MINIMAL_EMIT: dict[str, object] = {
    "source_assessment": {"usability": "usable", "evidence": None, "note": None},
    "statements": [],
    "relations": {"groups": [], "conditions": [], "example_sets": []},
    "facts": {
        "presence": {
            "experience": {"state": "none_found", "evidence": None},
            "compensation": {"state": "none_found", "evidence": None},
            "quantities": {"state": "none_found", "evidence": None},
            "dates": {"state": "none_found", "evidence": None},
        },
        "entries": [],
    },
    "mentions": [],
    "areas": [],
    "block_accounting": [],
}


def test_version_2_resolves() -> None:
    assert emit_schema("2")["title"].endswith("schema_version 2")
    assert record_schema("2")["title"].endswith("schema_version 2")


def test_minimal_emit_validates() -> None:
    assert validate_emit(MINIMAL_EMIT, "2") == []


def test_unknown_top_level_key_rejected() -> None:
    bad = {**MINIMAL_EMIT, "demand_profile": {}}
    assert any("demand_profile" in e for e in validate_emit(bad, "2"))


def test_area_importance_rejected() -> None:
    # v2's core rule: no authoritative area-level importance exists (spec §3)
    bad = {
        **MINIMAL_EMIT,
        "statements": [_stmt("s1")],
        "areas": [{"id": "a1", "name": "Skills", "kind": "technical",
                   "statement_ids": ["s1"], "evidence": None, "importance": "required"}],
    }
    assert any("importance" in e for e in validate_emit(bad, "2"))


def test_strict_mode_round_trip_keeps_required_nullables() -> None:
    # no free-form objects exist in schema 2; strict_schema must not
    # introduce the string bridge, and normalize_emit must keep required
    # nullable fields (e.g. statement.importance = null) intact
    strict = strict_schema(emit_schema("2"))
    assert '"anyOf": [{"type": "string"}' not in str(strict).replace("'", '"')
    emit = {**MINIMAL_EMIT, "statements": [_stmt("s1")]}
    out = normalize_emit(emit, "2")
    assert out["statements"][0]["importance"] is None


def _stmt(sid: str) -> dict[str, object]:
    return {
        "id": sid, "kind": "employer_context", "subject": "employer",
        "topic": "About", "evidence": [{"block_id": "b000001", "text": None,
                                         "occurrence": None}],
        "importance": None, "importance_evidence": None,
        "polarity": "positive", "polarity_evidence": None,
        "proficiency": None, "proficiency_evidence": None,
        "condition_ids": [], "fact_ids": [], "unresolved": [],
    }
```

- [ ] **Step 4: Run to verify failure**

Run: `uv run pytest tests/l2/v2/test_schemas2.py -v`
Expected: FAIL with `KeyError: unknown schema version: 2` before the files exist; PASS after Steps 1–2 land.

- [ ] **Step 5: Verify pass, gates, commit**

Run: `uv run pytest tests/l2/ -v && uv run ruff check . && uv run mypy`
```bash
git add src/jobhunter/l2/schemas_data/2/ tests/l2/v2/
git commit -m "feat(l2/v2): schema bundle 2 — emit and record contracts"
```

---

### Task 3: `v2/types.py` — closed enums and typed results

One module every other v2 module imports; no runtime services.

**Files:**
- Create: `src/jobhunter/l2/v2/__init__.py` (empty docstring module)
- Create: `src/jobhunter/l2/v2/types.py`
- Test: `tests/l2/v2/test_types.py`

**Interfaces:**
- Produces (used by Tasks 4–10): the constants and dataclasses below, exactly as named.

- [ ] **Step 1: Write the failing test** — `tests/l2/v2/test_types.py`:

```python
from jobhunter.l2.schemas import emit_schema
from jobhunter.l2.v2 import types


def test_enums_match_schema_2() -> None:
    defs = emit_schema("2")["$defs"]
    assert set(types.STATEMENT_KINDS) == set(defs["statement"]["properties"]["kind"]["enum"])
    assert set(types.IMPORTANCE) == set(defs["importance"]["enum"])
    assert set(types.OPERATORS) == set(defs["group"]["properties"]["operator"]["enum"])
    assert set(types.FAMILIES) == set(defs["fact_entry"]["properties"]["family"]["enum"])
    assert set(types.MENTION_ROLES) == set(defs["mention"]["properties"]["role"]["enum"])
    assert None not in types.PROFICIENCY  # the null lives at the field, not the enum


def test_block_is_frozen() -> None:
    b = types.Block(id="b000001", text="hello", span=(0, 5))
    assert b.span == (0, 5)
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/l2/v2/test_types.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement** — `src/jobhunter/l2/v2/types.py`:

```python
"""Closed enums and typed results shared by every v2 module. No runtime services.

The tuples below are the single Python-side source for the schema-2 enums; a
test asserts they match the packaged JSON so the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass

STATEMENT_KINDS = ("qualification", "responsibility", "employment_constraint",
                   "compensation_statement", "hiring_policy", "employer_context")
SUBJECTS = ("candidate", "employer", "role", "unstated")
IMPORTANCE = ("required", "preferred", "not_required", "unstated", "ambiguous")
POLARITY = ("positive", "negative", "ambiguous")
PROFICIENCY = ("expert", "proficient", "working", "exposure")
OPERATORS = ("all_of", "any_of", "unresolved")
CONDITION_KINDS = ("qualification_route", "role_level", "geography",
                   "employment_type", "schedule", "other")
FAMILIES = ("experience", "compensation", "quantity", "date")
MENTION_ROLES = ("direct", "example", "contextual")
USABILITY = ("usable", "partial", "placeholder", "empty", "unsupported")
EXCLUSION_REASONS = ("eeo", "benefits", "employer_description",
                     "contact_privacy_admin", "navigation")
DISPOSITIONS = ("statements", "facts", "context", "excluded", "unresolved")
PRESENCE_STATES = ("stated", "none_found", "explicitly_absent", "unresolved")
DERIVED_STATES = ("parsed", "present_unparsed", "ambiguous", "conflicting")
COMPARISONS = ("gte", "gt", "lte", "lt", "eq", "range", "unstated")
DIMENSIONS = ("duration", "count", "percentage", "frequency")
COMPONENTS = ("base", "total", "bonus", "equity", "unspecified")
PERIODS = ("year", "month", "week", "day", "hour")
DATE_KINDS = ("application_deadline", "interview_date", "other")

# statement kinds that carry a non-null importance (spec §3: qualifications and
# employment constraints; hiring policies impose applicant rules the same way)
IMPORTANCE_KINDS = frozenset({"qualification", "employment_constraint", "hiring_policy"})


@dataclass(frozen=True)
class Block:
    """One nonempty source line under blocks/1: id, exact text, codepoint span."""

    id: str
    text: str
    span: tuple[int, int]
```

`__init__.py`: `"""Parsing contract v2 (offline increment): pure, no I/O, no LLM."""`

- [ ] **Step 4: Verify pass** — `uv run pytest tests/l2/v2/test_types.py -v` → PASS.

- [ ] **Step 5: Gates and commit**

Run: `uv run ruff check . && uv run mypy`
```bash
git add src/jobhunter/l2/v2/ tests/l2/v2/test_types.py
git commit -m "feat(l2/v2): shared closed enums and Block type"
```

---

### Task 4: `v2/source.py` — block annotation `blocks/1` and reference binding

`annotate(markdown)` assigns `b000001, b000002, …` in source order to every
line containing a non-whitespace character; blank/whitespace-only lines keep
their offsets but get no id. `resolve(ref, blocks)` binds an emit reference to
the record's `bound_ref` shape. No fuzzy repair — unknown block, non-literal
substring, impossible occurrence, and half-null text/occurrence pairs all
raise.

**Files:**
- Create: `src/jobhunter/l2/v2/source.py`
- Test: `tests/l2/v2/test_source.py`

**Interfaces:**
- Consumes: `types.Block`.
- Produces:
  - `ANNOTATION_VERSION: str = "blocks/1"`
  - `annotate(markdown: str) -> list[Block]`
  - `class RefBindError(Exception)` with `.message: str`
  - `resolve(ref: dict[str, Any], blocks: dict[str, Block]) -> dict[str, Any]` returning `{"block_id", "text", "span": [s, e], "occurrence"}` (block-local occurrence)
  - `blocks_by_id(blocks: list[Block]) -> dict[str, Block]`

- [ ] **Step 1: Write the failing tests** — `tests/l2/v2/test_source.py`:

```python
import pytest

from jobhunter.l2.v2.source import (
    ANNOTATION_VERSION,
    RefBindError,
    annotate,
    blocks_by_id,
    resolve,
)

MD = "# Title\n\nPython and SQL. Python daily.\n   \n最低8年の経験。\n"


def test_annotation_version() -> None:
    assert ANNOTATION_VERSION == "blocks/1"


def test_annotate_ids_spans_and_blanks() -> None:
    blocks = annotate(MD)
    assert [b.id for b in blocks] == ["b000001", "b000002", "b000003"]
    assert blocks[0].text == "# Title" and blocks[0].span == (0, 7)
    # blank and whitespace-only lines get no block; offsets stay codepoint-true
    assert blocks[1].text == "Python and SQL. Python daily."
    assert MD[blocks[1].span[0] : blocks[1].span[1]] == blocks[1].text
    assert MD[blocks[2].span[0] : blocks[2].span[1]] == "最低8年の経験。"  # CJK codepoints


def test_annotate_empty_document() -> None:
    assert annotate("") == []
    assert annotate("\n \n") == []


def test_resolve_whole_block() -> None:
    blocks = blocks_by_id(annotate(MD))
    q = resolve({"block_id": "b000001", "text": None, "occurrence": None}, blocks)
    assert q == {"block_id": "b000001", "text": "# Title", "span": [0, 7], "occurrence": 0}


def test_resolve_substring_occurrence() -> None:
    blocks = blocks_by_id(annotate(MD))
    q = resolve({"block_id": "b000002", "text": "Python", "occurrence": 1}, blocks)
    s, e = q["span"]
    assert MD[s:e] == "Python" and q["occurrence"] == 1


@pytest.mark.parametrize(
    "ref",
    [
        {"block_id": "b000099", "text": None, "occurrence": None},   # unknown block
        {"block_id": "b000002", "text": "Ruby", "occurrence": 0},    # not a substring
        {"block_id": "b000002", "text": "Python", "occurrence": 5},  # impossible index
        {"block_id": "b000002", "text": "Python", "occurrence": None},  # half-null pair
        {"block_id": "b000002", "text": None, "occurrence": 0},         # half-null pair
    ],
)
def test_resolve_rejects(ref: dict[str, object]) -> None:
    blocks = blocks_by_id(annotate(MD))
    with pytest.raises(RefBindError):
        resolve(ref, blocks)
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/l2/v2/test_source.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement** — `src/jobhunter/l2/v2/source.py`:

```python
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
        raise RefBindError(f"{block.id}: not a literal substring: {text[:80]!r}")
    if not 0 <= occurrence < len(starts):
        raise RefBindError(
            f"{block.id}: occurrence {occurrence} of {text[:80]!r}; block has {len(starts)}"
        )
    s = block.span[0] + starts[occurrence]
    return {"block_id": block.id, "text": text, "span": [s, s + len(text)],
            "occurrence": occurrence}
```

- [ ] **Step 4: Verify pass** — `uv run pytest tests/l2/v2/test_source.py -v` → PASS.

- [ ] **Step 5: Gates and commit**

Run: `uv run ruff check . && uv run mypy`
```bash
git add src/jobhunter/l2/v2/source.py tests/l2/v2/test_source.py
git commit -m "feat(l2/v2): blocks/1 source annotation and exact reference binding"
```

---

### Task 5: `v2/facts.py` — quantity derivation (validator/10, part 1)

Code derives every normalized number from cited wording; the model never
supplies a value. Inputs are the concatenated texts of the aspect evidence
(assembly joins multiple refs with a single space, source order).

Grammar contract (spec §3 "Facts and missing information"):
- comparison text `at least / a minimum of / minimum (of) / no less than` → `gte`; `more than / over / greater than` → `gt`; `up to / at most / no more than / maximum (of)` → `lte`; `less than / fewer than / under` → `lt`; absent → operator from the value text (`8+` → gte, `5-7` → range) else `unstated`.
- value text: `N <unit>`, `N-M <unit>`, `N+ <unit>`; units `years→month×12`, `months→month`, `%→percent`, `times per week/month/day→per_*`; bare integer with no unit → dimension `count`, unit null.
- inclusivity: `gte/lte` inclusive true on the bounded side; `gt/lt` false; `range/eq` both true; `unstated` → null.
- anything else → `None` (assembly stores `present_unparsed`; a grammar gap is never a value).

**Files:**
- Create: `src/jobhunter/l2/v2/facts.py`
- Test: `tests/l2/v2/test_facts_quantity.py`

**Interfaces:**
- Consumes: `types` enums.
- Produces:
  - `VALIDATOR_VERSION: str = "10"`
  - `derive_quantity(value_text: str, comparison_text: str | None) -> dict[str, Any] | None` — the record's `derived_quantity` shape.

- [ ] **Step 1: Write the failing tests** — `tests/l2/v2/test_facts_quantity.py`:

```python
import pytest

from jobhunter.l2.v2.facts import VALIDATOR_VERSION, derive_quantity


def q(dimension, comparison, lo, hi, inc_lo, inc_hi, unit):
    return {"dimension": dimension, "comparison": comparison, "min_value": lo,
            "max_value": hi, "inclusive_min": inc_lo, "inclusive_max": inc_hi,
            "unit": unit}


def test_validator_version_is_10() -> None:
    assert VALIDATOR_VERSION == "10"


@pytest.mark.parametrize(
    ("value", "comparison", "expected"),
    [
        # C01: inclusive floor, no invented ceiling
        ("8 years", "a minimum of", q("duration", "gte", 96, None, True, None, "month")),
        ("8 years", "at least", q("duration", "gte", 96, None, True, None, "month")),
        # spec §3: strict floor, never converted to nine years
        ("8 years", "more than", q("duration", "gt", 96, None, False, None, "month")),
        ("8 years", "over", q("duration", "gt", 96, None, False, None, "month")),
        # bare quantity: stated, not an eligibility band
        ("5 years", None, q("duration", "unstated", 60, 60, None, None, "month")),
        ("5-7 years", None, q("duration", "range", 60, 84, True, True, "month")),
        ("8+ years", None, q("duration", "gte", 96, None, True, None, "month")),
        # C06-adjacent shapes
        ("20%", "up to", q("percentage", "lte", None, 20, None, True, "percent")),
        ("2-3 times per week", None, q("frequency", "range", 2, 3, True, True, "per_week")),
        ("6 months", None, q("duration", "unstated", 6, 6, None, None, "month")),
        ("3", "at least", q("count", "gte", 3, None, True, None, None)),
    ],
)
def test_derivations(value, comparison, expected) -> None:
    assert derive_quantity(value, comparison) == expected


@pytest.mark.parametrize(
    ("value", "comparison"),
    [
        ("several years", None),          # no number
        ("8 years and 3 years", None),    # two tokens, no range syntax
        ("7-5 years", None),              # descending range
        ("acht Jahre", "mindestens"),     # unknown language: unparsed, not guessed
    ],
)
def test_unparseable_is_none(value, comparison) -> None:
    assert derive_quantity(value, comparison) is None
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/l2/v2/test_facts_quantity.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/jobhunter/l2/v2/facts.py`:

```python
"""Versioned v2 derivation grammars: the model cites spans, code computes values.

VALIDATOR_VERSION freezes this module together with v2/verify.py's checks; any
grammar or threshold change bumps it (identifier 9 was claimed by the v1 floor
repair, spec [A3]). Null-over-guess governs every branch.
"""

from __future__ import annotations

import re
from typing import Any

VALIDATOR_VERSION = "10"

_CMP_PHRASES: list[tuple[str, str]] = [
    (r"at\s+least|a\s+minimum\s+of|minimum\s+of|minimum|no\s+less\s+than", "gte"),
    (r"more\s+than|over|greater\s+than", "gt"),
    (r"up\s+to|at\s+most|no\s+more\s+than|maximum\s+of|maximum", "lte"),
    (r"less\s+than|fewer\s+than|under", "lt"),
]

_NUM = r"(\d+(?:\.\d+)?)"
_RANGE = re.compile(_NUM + r"\s*(?:-|–|—|to)\s*" + _NUM)
_PLUS = re.compile(_NUM + r"\s*\+")
_SINGLE = re.compile(_NUM)
_UNIT = re.compile(
    r"(?P<years>years?|yrs?|yoe)|(?P<months>months?|mos?)|(?P<pct>%|percent)"
    r"|(?:times?\s+per\s+(?P<per>week|month|day))",
    re.IGNORECASE,
)


def _comparison(comparison_text: str | None) -> str | None:
    if comparison_text is None:
        return None
    for pattern, op in _CMP_PHRASES:
        if re.search(r"(?i)\b(?:" + pattern + r")\b", comparison_text):
            return op
    return "?"  # comparison evidence present but not in the grammar: unparsed


def derive_quantity(value_text: str, comparison_text: str | None) -> dict[str, Any] | None:
    op = _comparison(comparison_text)
    if op == "?":
        return None
    unit_m = _UNIT.search(value_text)
    if unit_m and unit_m.group("years"):
        dimension, unit, scale = "duration", "month", 12.0
    elif unit_m and unit_m.group("months"):
        dimension, unit, scale = "duration", "month", 1.0
    elif unit_m and unit_m.group("pct"):
        dimension, unit, scale = "percentage", "percent", 1.0
    elif unit_m and unit_m.group("per"):
        dimension, unit, scale = "frequency", f"per_{unit_m.group('per').lower()}", 1.0
    else:
        dimension, unit, scale = "count", None, 1.0

    def num(raw: str) -> float | int:
        v = float(raw) * scale
        return int(v) if v == int(v) else v

    lo: float | int | None
    hi: float | int | None
    if m := _RANGE.search(value_text):
        lo, hi = num(m.group(1)), num(m.group(2))
        if op is None:
            if lo > hi:
                return None  # descending: ambiguous
            return _q(dimension, "range", lo, hi, True, True, unit)
        return None  # a comparison phrase over a range: ambiguous, unparsed
    if m := _PLUS.search(value_text):
        if op is None:
            return _q(dimension, "gte", num(m.group(1)), None, True, None, unit)
        return None
    singles = _SINGLE.findall(value_text)
    if len(singles) != 1:
        return None  # zero numbers, or several without range syntax
    v = num(singles[0])
    if op is None:
        return _q(dimension, "unstated", v, v, None, None, unit)
    if op in ("gte", "gt"):
        return _q(dimension, op, v, None, op == "gte", None, unit)
    return _q(dimension, op, None, v, None, op == "lte", unit)


def _q(dimension: str, comparison: str, lo: float | int | None, hi: float | int | None,
       inc_lo: bool | None, inc_hi: bool | None, unit: str | None) -> dict[str, Any]:
    return {"dimension": dimension, "comparison": comparison, "min_value": lo,
            "max_value": hi, "inclusive_min": inc_lo, "inclusive_max": inc_hi,
            "unit": unit}
```

- [ ] **Step 4: Verify pass** — `uv run pytest tests/l2/v2/test_facts_quantity.py -v` → PASS.

- [ ] **Step 5: Gates and commit**

Run: `uv run ruff check . && uv run mypy`
```bash
git add src/jobhunter/l2/v2/facts.py tests/l2/v2/test_facts_quantity.py
git commit -m "feat(l2/v2): quantity derivation with explicit comparison operators (validator/10)"
```

---

### Task 6: `v2/facts.py` — money and date derivation (validator/10, part 2)

Money: decimal-string amounts (cents never discarded), currency only from an
explicit code or a single-currency symbol (£/€ — never `$`/`¥`), period from
unit evidence, malformed source amounts → `None` (kept as `present_unparsed`
with the evidence — the C06 rule). Dates: month-name grammar unambiguous;
numeric `a/b/year` with both `a, b ≤ 12` and `a != b` returns the ambiguous
candidates; never resolved by machine locale.

**Files:**
- Modify: `src/jobhunter/l2/v2/facts.py`
- Test: `tests/l2/v2/test_facts_money_date.py`

**Interfaces:**
- Produces:
  - `derive_money(value_text: str, comparison_text: str | None, currency_text: str | None, period_text: str | None) -> dict[str, Any] | None` — the record's `derived_money` shape.
  - `derive_date(value_text: str) -> dict[str, Any] | None` — the record's `derived_date` shape (`{"date": iso|None, "candidates": [...]|None}`; ambiguity is a result, not a failure).

- [ ] **Step 1: Write the failing tests** — `tests/l2/v2/test_facts_money_date.py`:

```python
import pytest

from jobhunter.l2.v2.facts import derive_date, derive_money


def m(cmp, lo, hi, cur, period):
    return {"comparison": cmp, "min_amount": lo, "max_amount": hi,
            "currency": cur, "period": period}


@pytest.mark.parametrize(
    ("value", "cmp_text", "cur_text", "period_text", "expected"),
    [
        # C02: the period lives in its own evidence; $ alone is no currency
        ("$210,300 - $273,400", None, None, "annually",
         m("range", "210300", "273400", None, "year")),
        ("$210,300 - $273,400", None, "USD", "Gross pay annually",
         m("range", "210300", "273400", "USD", "year")),
        # cents survive as decimal strings
        ("$169,100.00 - $233,200.00", None, "USD", None,
         m("range", "169100.00", "233200.00", "USD", None)),
        ("€71.000 to €95.000", None, None, "per annum",
         m("range", "71000", "95000", "EUR", "year")),
        ("£55,000", None, None, "per hour", m("unstated", "55000", "55000", "GBP", "hour")),
        ("$150,000", "up to", None, None, m("lte", None, "150000", None, None)),
        ("$130 - $150K", None, None, None, m("range", "130000", "150000", None, None)),
    ],
)
def test_money(value, cmp_text, cur_text, period_text, expected) -> None:
    assert derive_money(value, cmp_text, cur_text, period_text) == expected


@pytest.mark.parametrize(
    ("value", "cmp_text"),
    [
        ("$106,147 - $228, 781", None),   # C06's malformed upper bound: unparsed, kept
        ("£100,000 - €120,000", None),    # mixed currencies is not a range
        ("$150,000 - $120,000", None),    # inverted
        ("competitive salary", None),     # no amount
    ],
)
def test_money_unparseable_is_none(value, cmp_text) -> None:
    assert derive_money(value, cmp_text, None, None) is None


def test_dates() -> None:
    assert derive_date("September 11, 2026") == {"date": "2026-09-11", "candidates": None}
    assert derive_date("09/25/2026") == {"date": "2026-09-25", "candidates": None}  # day > 12
    # locale-ambiguous: both readings valid → candidates, no winner
    assert derive_date("03/04/2026") == {
        "date": None, "candidates": ["2026-03-04", "2026-04-03"]}
    assert derive_date("02/02/2026") == {"date": "2026-02-02", "candidates": None}
    assert derive_date("sometime soon") is None
    assert derive_date("02/30/2026") is None  # impossible calendar date
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/l2/v2/test_facts_money_date.py -v` → FAIL.

- [ ] **Step 3: Implement** — append to `src/jobhunter/l2/v2/facts.py`:

```python
_SYMBOL_CURRENCY = {"£": "GBP", "€": "EUR"}  # single-currency symbols only; $ and ¥ stay null
_CODE = re.compile(
    r"\b(USD|CAD|AUD|NZD|SGD|HKD|EUR|GBP|JPY|CNY|CHF|SEK|INR|TWD|KRW)\b", re.IGNORECASE
)
_AMT = r"([$£€¥]?)\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{1,3}(?:\.\d{3})+|\d+(?:\.\d{1,2})?)\s*([kK])?"
_MONEY_RANGE = re.compile(_AMT + r"\s*(?:--?|–|—|to)\s*" + _AMT)
_MONEY_ONE = re.compile(_AMT)
_PERIOD = re.compile(
    r"(?P<hour>/\s*(?:hr|hour)|per\s+hour|hourly)|(?P<year>/\s*(?:yr|year)|per\s+(?:year|annum)"
    r"|annually|annual|yearly)|(?P<month>per\s+month|monthly)|(?P<week>per\s+week|weekly)"
    r"|(?P<day>per\s+day|daily)",
    re.IGNORECASE,
)


def _decimal(sym: str, digits: str, k: str | None) -> str | None:
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", digits):
        digits = digits.replace(".", "")  # European thousands separator
    digits = digits.replace(",", "")
    if k:
        if "." in digits:
            return None  # "1.5K" cents-vs-thousands: ambiguous
        digits = str(int(digits) * 1000)
    return digits


def _period(period_text: str | None) -> str | None:
    if period_text is None:
        return None
    m = _PERIOD.search(period_text)
    if m is None:
        return None
    return next(name for name in ("hour", "year", "month", "week", "day") if m.group(name))


def derive_money(value_text: str, comparison_text: str | None,
                 currency_text: str | None, period_text: str | None) -> dict[str, Any] | None:
    op = _comparison(comparison_text)
    if op == "?":
        return None
    currency: str | None = None
    code = _CODE.search(currency_text or "") or _CODE.search(value_text)
    if code:
        currency = code.group(1).upper()
    lo: str | None
    hi: str | None
    if m := _MONEY_RANGE.fullmatch(value_text.strip()):
        s1, d1, k1, s2, d2, k2 = m.groups()
        if s1 and s2 and s1 != s2:
            return None  # mixed symbols is not a range
        lo, hi = _decimal(s1, d1, k1), _decimal(s2, d2, k2)
        if lo is None or hi is None:
            return None
        if k2 and not k1 and float(lo) < 1000:
            lo = str(int(float(lo) * 1000))  # "$130 - $150K": trailing K covers both
        if float(lo) > float(hi):
            return None  # inverted: ambiguous
        if op is None:
            op = "range"
        else:
            return None  # comparison phrase over a range: ambiguous
        sym = s1 or s2
    elif m := _MONEY_ONE.fullmatch(value_text.strip()):
        sym, digits, k = m.groups()
        v = _decimal(sym, digits, k)
        if v is None:
            return None
        if op is None:
            op, lo, hi = "unstated", v, v
        elif op in ("gte", "gt"):
            lo, hi = v, None
        else:
            lo, hi = None, v
    else:
        return None  # malformed ("$228, 781") or absent amount: unparsed, kept
    if currency is None and sym:
        currency = _SYMBOL_CURRENCY.get(sym)
    return {"comparison": op, "min_amount": lo, "max_amount": hi,
            "currency": currency, "period": _period(period_text)}
```

and the date grammar (reusing v1's month table verbatim — copy `_MONTH_NAMES`/`_MONTHS` and `_DATE` from `transforms.py`, they are four lines):

```python
from datetime import date as _date

_MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december"]
_MONTHS = {name: i + 1 for i, name in enumerate(_MONTH_NAMES)}
_MONTHS.update({name[:3]: i + 1 for i, name in enumerate(_MONTH_NAMES)})
_DATE_NAMED = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})")
_DATE_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}(?:\d{2})?)\b")


def derive_date(value_text: str) -> dict[str, Any] | None:
    for m in _DATE_NAMED.finditer(value_text):
        month = _MONTHS.get(m.group(1).lower())
        if month is None:
            continue
        try:
            return {"date": _date(int(m.group(3)), month, int(m.group(2))).isoformat(),
                    "candidates": None}
        except ValueError:
            return None
    if m := _DATE_NUMERIC.search(value_text):
        a, b, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = yy + 2000 if yy < 100 else yy
        readings: list[str] = []
        for mm, dd in ((a, b), (b, a)):
            try:
                iso = _date(year, mm, dd).isoformat()
            except ValueError:
                continue
            if iso not in readings:
                readings.append(iso)
        if not readings:
            return None
        if len(readings) == 1:
            return {"date": readings[0], "candidates": None}
        return {"date": None, "candidates": sorted(readings)}  # locale-ambiguous
    return None
```

- [ ] **Step 4: Verify pass** — `uv run pytest tests/l2/v2/test_facts_money_date.py -v` → PASS.

- [ ] **Step 5: Gates and commit**

Run: `uv run pytest tests/l2/v2/ -v && uv run ruff check . && uv run mypy`
```bash
git add src/jobhunter/l2/v2/facts.py tests/l2/v2/test_facts_money_date.py
git commit -m "feat(l2/v2): money and date derivation — decimal amounts, ambiguous locales kept"
```

---

### Task 7: `v2/assemble.py` — emit → record

Bind every reference, derive every fact, attach code-owned blocks, compute the
candidate hash. All binding errors collect before raising (one reprompt in
increment 2 carries the complete list — the v1 pattern).

Derivation wiring per family: `experience`/`quantity` → `derive_quantity`
(value + comparison evidence texts), `compensation` → `derive_money` (value,
comparison, currency, unit evidence), `date` → `derive_date` (value evidence).
Aspect evidence texts are the bound refs' texts joined with one space in
source order. `None` from a grammar → `derived.state = "present_unparsed"`,
payloads null. An ambiguous date result → `state = "ambiguous"`. The
`conflicting` state is reserved for the increment-2 auditor — assembly never
emits it.

**Files:**
- Create: `src/jobhunter/l2/v2/assemble.py`
- Test: `tests/l2/v2/test_assemble.py`

**Interfaces:**
- Consumes: `source.annotate/blocks_by_id/resolve/RefBindError/ANNOTATION_VERSION`, `facts.derive_quantity/derive_money/derive_date/VALIDATOR_VERSION`, `hashing.canonical_json/sha256_hex`, `quality.assess` (Task 9 — until then assembly sets `"quality": None` placeholder? No: to keep tasks independently testable, assembly OWNS a minimal quality stub inline and Task 9 replaces it — see Step 3).
- Produces:
  - `class AssembleError(Exception)` with `.errors: list[str]`
  - `assemble(emit: dict, markdown: str, *, document_hash: str, observed_model: str, at: str, normalizer_version: str = NORMALIZER_VERSION, prompt_version: str = "demand-profile/v6", parent_candidate_hash: str | None = None) -> dict` — a schema-2 record; `extraction.rules_version = "parsing-rules/2"`, `extraction.schema_version = "2"`, `extraction.validator_version = facts.VALIDATOR_VERSION`.
  - `candidate_hash(record: dict) -> str` — sha256 over `canonical_json` of the record with `extraction.candidate_hash` set to `""` and `quality` set to `None` (so repairs re-hash deterministically and quality changes never move identity).
  - `normalize_key(surface: str) -> str` — `surface.casefold().strip()` (alias policy `aliases/1`; richer aliasing is deferred with concept linking).

- [ ] **Step 1: Write the failing tests** — `tests/l2/v2/test_assemble.py`. Build a small emit over a two-line document and assert: bound spans, derived facts, code-owned fields, error collection.

```python
import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.schemas import validate_record
from jobhunter.l2.v2.assemble import AssembleError, assemble, candidate_hash

MD = "Requirements\nA minimum of 8 years of experience in sales.\n"
DOC_HASH = sha256_hex(MD.encode("utf-8"))


def _emit() -> dict:
    ref_cmp = {"block_id": "b000002", "text": "A minimum of", "occurrence": 0}
    ref_val = {"block_id": "b000002", "text": "8 years", "occurrence": 0}
    whole = {"block_id": "b000002", "text": None, "occurrence": None}
    return {
        "source_assessment": {"usability": "usable", "evidence": None, "note": None},
        "statements": [{
            "id": "s1", "kind": "qualification", "subject": "candidate",
            "topic": "Sales experience", "evidence": [whole],
            "importance": "required",
            "importance_evidence": [{"block_id": "b000001", "text": None, "occurrence": None}],
            "polarity": "positive", "polarity_evidence": None,
            "proficiency": None, "proficiency_evidence": None,
            "condition_ids": [], "fact_ids": ["f1"], "unresolved": [],
        }],
        "relations": {"groups": [], "conditions": [], "example_sets": []},
        "facts": {
            "presence": {
                "experience": {"state": "stated", "evidence": [ref_val]},
                "compensation": {"state": "none_found", "evidence": None},
                "quantities": {"state": "none_found", "evidence": None},
                "dates": {"state": "none_found", "evidence": None},
            },
            "entries": [{
                "id": "f1", "family": "experience", "statement_ids": ["s1"],
                "condition_ids": [],
                "scope": {"kind": "overall", "evidence": None},
                "date_kind": None, "component": None,
                "evidence": {"value": [ref_val], "comparison": [ref_cmp],
                              "unit": None, "currency": None, "component": None,
                              "applicability": None},
            }],
        },
        "mentions": [],
        "areas": [{"id": "a1", "name": "Experience", "kind": "capability",
                    "statement_ids": ["s1"], "evidence": None}],
        "block_accounting": [
            {"block_id": "b000001", "disposition": "context", "ref_ids": ["s1"],
             "exclusion_reason": None, "evidence": None},
            {"block_id": "b000002", "disposition": "statements", "ref_ids": ["s1"],
             "exclusion_reason": None, "evidence": None},
        ],
    }


def test_assemble_binds_derives_and_validates() -> None:
    record = assemble(_emit(), MD, document_hash=DOC_HASH,
                      observed_model="gpt-5.6-luna", at="2026-09-07T00:00:00+00:00")
    assert validate_record(record, "2") == []
    stmt = record["statements"][0]
    assert MD[slice(*stmt["evidence"][0]["span"])] == stmt["evidence"][0]["text"]
    derived = record["facts"]["entries"][0]["derived"]
    assert derived["state"] == "parsed"
    assert derived["quantity"] == {"dimension": "duration", "comparison": "gte",
                                   "min_value": 96, "max_value": None,
                                   "inclusive_min": True, "inclusive_max": None,
                                   "unit": "month"}
    assert record["extraction"]["schema_version"] == "2"
    assert record["extraction"]["validator_version"] == "10"
    assert record["document"]["annotation_version"] == "blocks/1"
    assert record["extraction"]["candidate_hash"] == candidate_hash(record)


def test_candidate_hash_ignores_quality_and_itself() -> None:
    record = assemble(_emit(), MD, document_hash=DOC_HASH,
                      observed_model="m", at="2026-09-07T00:00:00+00:00")
    h = candidate_hash(record)
    record["quality"]["search_eligible"] = True  # quality never moves identity
    assert candidate_hash(record) == h


def test_unparseable_fact_is_kept_not_dropped() -> None:
    # C06's rule: stated-but-unparsed pay is distinguishable from unstated pay
    emit = _emit()
    emit["facts"]["entries"][0]["evidence"]["comparison"] = None
    emit["facts"]["entries"][0]["evidence"]["value"] = [
        {"block_id": "b000002", "text": "minimum of", "occurrence": 0}]  # no number
    record = assemble(emit, MD, document_hash=DOC_HASH,
                      observed_model="m", at="2026-09-07T00:00:00+00:00")
    derived = record["facts"]["entries"][0]["derived"]
    assert derived["state"] == "present_unparsed"
    assert derived["quantity"] is None


def test_all_binding_errors_collected() -> None:
    emit = _emit()
    emit["statements"][0]["evidence"] = [
        {"block_id": "b000099", "text": None, "occurrence": None},
        {"block_id": "b000002", "text": "Ruby", "occurrence": 0},
    ]
    with pytest.raises(AssembleError) as exc:
        assemble(emit, MD, document_hash=DOC_HASH,
                 observed_model="m", at="2026-09-07T00:00:00+00:00")
    assert len(exc.value.errors) == 2
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/l2/v2/test_assemble.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/jobhunter/l2/v2/assemble.py`. Structure (write it in full; the walk is mechanical):

```python
"""Emit JSON → v2 record: bind every reference, derive every fact, attach the
code-owned document/extraction/quality blocks, hash the candidate. All binding
failures collect before raising so one reprompt carries the whole list."""

from __future__ import annotations

import copy
from typing import Any

from jobhunter.hashing import canonical_json, sha256_hex
from jobhunter.markdown import NORMALIZER_VERSION
from jobhunter.l2.v2.facts import (
    VALIDATOR_VERSION,
    derive_date,
    derive_money,
    derive_quantity,
)
from jobhunter.l2.v2.source import (
    ANNOTATION_VERSION,
    RefBindError,
    annotate,
    blocks_by_id,
    resolve,
)

RULES_VERSION = "parsing-rules/2"


class AssembleError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"{len(errors)} resolution error(s)")
        self.errors = errors


def normalize_key(surface: str) -> str:
    return surface.casefold().strip()


def candidate_hash(record: dict[str, Any]) -> str:
    shadow = copy.deepcopy(record)
    shadow["extraction"]["candidate_hash"] = ""
    shadow["quality"] = None
    return sha256_hex(canonical_json(shadow))
```

The body: a `_Binder` class holding `blocks`, `errors: list[str]`, with
`refs(path, value)` → list of bound refs or None (appends the path-prefixed
`RefBindError.message` on failure), and `opt_refs` for nullable evidence.
`assemble()` walks: source_assessment, each statement (all evidence fields),
relations (groups/conditions/example_sets), presence, each fact entry
(each aspect of `evidence`, plus `scope.evidence`), each mention
(single ref + `normalized_key`), areas, block_accounting. For fact entries,
after binding compute `texts(aspect) = " ".join(r["text"] for r in bound)` and:

```python
if entry["family"] in ("experience", "quantity"):
    q = derive_quantity(texts("value"), texts_or_none("comparison"))
    derived = {"state": "parsed" if q else "present_unparsed",
               "quantity": q, "money": None, "date": None}
elif entry["family"] == "compensation":
    money = derive_money(texts("value"), texts_or_none("comparison"),
                          texts_or_none("currency"), texts_or_none("unit"))
    derived = {"state": "parsed" if money else "present_unparsed",
               "quantity": None, "money": money, "date": None}
else:  # date
    d = derive_date(texts("value"))
    state = ("present_unparsed" if d is None
             else "ambiguous" if d["date"] is None else "parsed")
    derived = {"state": state, "quantity": None, "money": None, "date": d}
```

Then raise `AssembleError(self.errors)` if any; else build the record dict in
schema-2 property order, with `"quality"` set by a local stub
`_initial_quality(usability, evidence="pass")` returning
`{"source": usability, "evidence": "pass", "semantics": "not_checked",
"completeness": "not_checked", "sampling": "not_requested",
"human_review": "none", "search_eligible": False}` (Task 9 replaces the stub
with `quality.assess` and re-points this call — one-line change, noted there),
set `extraction.candidate_hash = ""`, compute `candidate_hash(record)`, store
it, return.

- [ ] **Step 4: Verify pass** — `uv run pytest tests/l2/v2/test_assemble.py -v` → PASS.

- [ ] **Step 5: Gates and commit**

Run: `uv run pytest tests/l2/v2/ -v && uv run ruff check . && uv run mypy`
```bash
git add src/jobhunter/l2/v2/assemble.py tests/l2/v2/test_assemble.py
git commit -m "feat(l2/v2): emit-to-record assembly with collected binding errors"
```

---

### Task 8: `v2/verify.py` — deterministic checks

Pure `verify(record, markdown) -> Report` (reuses `l2.report.Report`,
`validator_version = "10"`). Checks and finding codes:

| check | code | severity | rule |
| --- | --- | --- | --- |
| doc_binding | `hash_mismatch` | error | sha256 of markdown == `document.document_hash` (fail-fast) |
| annotation | `annotation_version` | error | `document.annotation_version == "blocks/1"` |
| schema | `invalid` | error | `validate_record(record, "2")` (fail-fast after) |
| attribution | `text_mismatch` / `span_bounds` / `occurrence_mismatch` / `outside_block` | error | every bound_ref: `md[s:e] == text`, span within the named block's span, block-local occurrence correct |
| references | `unknown_reference` | error | every id in `condition_ids`/`fact_ids`/`statement_ids`/`members`/`mention_ids`/`parent_statement_id`/`ref_ids` resolves to an object of the right type |
| references | `duplicate_id` | error | statement/group/condition/fact/mention/area/example-set ids unique across their namespaces |
| references | `reference_cycle` / `depth_exceeded` | error | group membership acyclic, nesting ≤ 5 |
| relations | `connective_evidence_missing` | error | `all_of`/`any_of` groups carry non-null evidence; `unresolved` may be bare |
| statements | `importance_missing` | error | kinds in `types.IMPORTANCE_KINDS` have non-null importance |
| statements | `importance_unexpected` | error | other kinds have null importance |
| statements | `evidence_missing` | error | non-null proficiency ⇒ non-null proficiency_evidence; importance `required`/`preferred`/`not_required`/`ambiguous` ⇒ non-null importance_evidence (`unstated` may be bare) |
| facts | `fact_mismatch` | error | re-derive each entry's `derived` from its bound evidence texts; must equal stored |
| facts | `fact_family_shape` | error | `date_kind` only on family date (and required there); `component` only on compensation; `scope` only on experience/quantity |
| facts | `presence_mismatch` | error | family has entries ⇔ its presence state is `stated`; `explicitly_absent`/`unresolved` presence requires evidence |
| mentions | `mention_ungrounded` | error | mention surface is a substring of its bound evidence text; `normalized_key == normalize_key(surface)` |
| accounting | `block_unaccounted` | error | every annotated block id appears in ≥1 accounting entry |
| accounting | `unknown_block` | error | accounting entries name only annotated blocks |
| accounting | `exclusion_reason_missing` | error | disposition `excluded` ⇒ non-null reason; other dispositions ⇒ null reason |
| accounting | `refs_missing` | error | dispositions `statements`/`facts` ⇒ non-empty `ref_ids` |
| accounting | `exclusion_requirement_language` | warning | excluded block text matches `(?i)\b(must|required?|minimum|at least|only candidates|need to|proficien\w*|fluen\w*)\b` — the deterministic tripwire for the C02/C07 English-footer class |
| usability | `usability_conflict` | error | model says `empty` but annotated blocks exist; or blocks are absent and usability != `empty`; nonempty non-usable classifications (`partial`/`placeholder`/`unsupported`) require evidence |
| usability | `empty_with_content` | error | usability `empty`/`placeholder` with statements or fact entries present — insufficiency and extraction output contradict |

Metrics: `n_statements`, `n_mentions`, `n_fact_entries`, `n_blocks`,
`blocks_accounted` (count), `excluded_blocks`.

**Files:**
- Create: `src/jobhunter/l2/v2/verify.py`
- Test: `tests/l2/v2/test_verify2.py`

**Interfaces:**
- Consumes: `source.annotate`, `facts` derivations, `assemble.normalize_key`, `schemas.validate_record`, `report.Report`, `hashing.sha256_hex`.
- Produces: `verify(record: dict, markdown: str) -> Report`.

- [ ] **Step 1: Write the failing tests** — `tests/l2/v2/test_verify2.py`. Use Task 7's `_emit()`/`assemble()` to get a passing record, then one test per finding code corrupting a deep-copied record:

```python
import copy

from jobhunter.l2.v2.verify import verify
# _record() helper: assemble(_emit(), MD, ...) — import the emit builder from
# tests.l2.v2.test_assemble or duplicate the 40 lines into a conftest fixture
# `v2_record` (preferred: move _emit()/MD/DOC_HASH into tests/l2/v2/conftest.py
# in this task and update test_assemble.py imports).


def test_clean_record_passes(v2_record) -> None:
    report = verify(v2_record, MD)
    assert report.status == "pass" and report.validator_version == "10"


def test_fact_mismatch(v2_record) -> None:
    bad = copy.deepcopy(v2_record)
    bad["facts"]["entries"][0]["derived"]["quantity"]["max_value"] = 96  # C01's legacy shape
    report = verify(bad, MD)
    assert any(f.code == "fact_mismatch" for f in report.findings)
    assert report.status == "fail"


def test_block_unaccounted(v2_record) -> None:
    bad = copy.deepcopy(v2_record)
    bad["block_accounting"] = bad["block_accounting"][:1]
    assert any(f.code == "block_unaccounted" for f in verify(bad, MD).findings)


def test_exclusion_requirement_language_warns(v2_record) -> None:
    bad = copy.deepcopy(v2_record)
    bad["block_accounting"][1] = {"block_id": "b000002", "disposition": "excluded",
                                   "ref_ids": [], "exclusion_reason": "eeo",
                                   "evidence": None}
    report = verify(bad, MD)  # b000002 contains "A minimum of 8 years..."
    codes = {f.code: f.severity for f in report.findings}
    assert codes.get("exclusion_requirement_language") == "warning"
```

…plus one corruption test for each remaining code in the table (same pattern:
deep-copy, break one thing, assert the code appears). Every code in the table
gets exactly one test; name them `test_<code>`.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/l2/v2/test_verify2.py -v` → FAIL.

- [ ] **Step 3: Implement** `src/jobhunter/l2/v2/verify.py` — same skeleton as v1's `verify()` (fail-fast hash → schema → check functions appending to one `Report`), one private `_check_*` function per table row group, iterating bound refs via a local `iter_bound_refs(record)` generator that yields `(path, ref)` for every `bound_ref` position in schema 2 (statements' five evidence fields, unresolved issues, relations, presence, fact-entry aspects and scope, mentions, areas, accounting).

- [ ] **Step 4: Verify pass** — `uv run pytest tests/l2/v2/test_verify2.py -v` → PASS.

- [ ] **Step 5: Gates and commit**

Run: `uv run pytest tests/l2/ -v && uv run ruff check . && uv run mypy`
```bash
git add src/jobhunter/l2/v2/verify.py tests/l2/v2/test_verify2.py tests/l2/v2/conftest.py tests/l2/v2/test_assemble.py
git commit -m "feat(l2/v2): deterministic verifier — references, facts, accounting, usability"
```

---

### Task 9: `v2/quality.py` + `v2/project.py` — eligibility policy and claim-level projection

Quality (spec §6): seven code-owned dimensions; `search_eligible` derived by
policy, never model output. Projection (spec §3): one row per
mention/statement link carrying the statement's own importance — the C04/C12
fix in code form. Both pure; the store wires them in increment 3.

**Files:**
- Create: `src/jobhunter/l2/v2/quality.py`
- Create: `src/jobhunter/l2/v2/project.py`
- Modify: `src/jobhunter/l2/v2/assemble.py` (replace `_initial_quality` stub with `quality.assess`)
- Test: `tests/l2/v2/test_quality.py`, `tests/l2/v2/test_project.py`

**Interfaces:**
- Produces:
  - `quality.assess(*, source: str, evidence: str, semantics: str = "not_checked", completeness: str = "not_checked", sampling: str = "not_requested", human_review: str = "none", blocking_findings: int = 0, blocking_unresolved: int = 0) -> dict[str, Any]` — the record's `quality` object.
  - Eligibility rule: `search_eligible` is True iff `source == "usable"` and `evidence == "pass"` and `semantics == "no_findings"` and `completeness == "no_findings"` and `sampling in ("not_requested", "complete")` and `human_review != "rejected"` and both blocking counts are 0. (In increment 1 nothing can reach `no_findings` — no auditor exists — so every offline record is ineligible by construction; the tests assert this.)
  - `project.mention_rows(record: dict) -> list[dict[str, Any]]` — one row per (mention, statement) pair: `{"mention_id", "statement_id", "surface", "normalized_key", "role", "kind", "subject", "importance", "polarity", "condition_ids", "group_ids"}` where `group_ids` lists every relation group the statement is a member of (direct membership only). Rows are emitted only when `record["quality"]["search_eligible"]` is True unless `include_ineligible=True` is passed (inspection surface, spec §7).

- [ ] **Step 1: Write the failing tests.** `test_quality.py`:

```python
from jobhunter.l2.v2.quality import assess


def test_offline_records_are_never_eligible() -> None:
    q = assess(source="usable", evidence="pass")
    assert q["search_eligible"] is False  # semantics/completeness not_checked


def test_full_gate_eligible() -> None:
    q = assess(source="usable", evidence="pass", semantics="no_findings",
               completeness="no_findings", sampling="complete")
    assert q["search_eligible"] is True


def test_human_rejection_is_final() -> None:
    q = assess(source="usable", evidence="pass", semantics="no_findings",
               completeness="no_findings", human_review="rejected")
    assert q["search_eligible"] is False


def test_insufficient_source_is_never_eligible() -> None:
    for usability in ("partial", "placeholder", "empty", "unsupported"):
        q = assess(source=usability, evidence="pass", semantics="no_findings",
                   completeness="no_findings")
        assert q["search_eligible"] is False, usability
```

`test_project.py` (uses the `v2_record` fixture; add a mention + a preferred
statement inside a group to the fixture emit):

```python
from jobhunter.l2.v2.project import mention_rows


def test_rows_carry_statement_importance_not_area(v2_record_with_mentions) -> None:
    rows = mention_rows(v2_record_with_mentions, include_ineligible=True)
    by_key = {r["normalized_key"]: r for r in rows}
    # the C04 shape: a preferred certification next to a required degree in the
    # same presentation area must project as preferred
    assert by_key["cpa"]["importance"] == "preferred"
    assert by_key["cpa"]["statement_id"] == "s_cert"


def test_ineligible_records_project_nothing_by_default(v2_record_with_mentions) -> None:
    assert mention_rows(v2_record_with_mentions) == []
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/l2/v2/test_quality.py tests/l2/v2/test_project.py -v` → FAIL.

- [ ] **Step 3: Implement both modules** (each under 60 lines: `assess` builds the dict and computes the boolean; `mention_rows` indexes statements and groups, then loops mentions × statement_ids). Replace the Task-7 stub: `assemble.py` imports `from jobhunter.l2.v2.quality import assess` and `_initial_quality` becomes `assess(source=usability, evidence="pass")`.

- [ ] **Step 4: Verify pass** — `uv run pytest tests/l2/v2/ -v` → PASS.

- [ ] **Step 5: Gates and commit**

```bash
git add src/jobhunter/l2/v2/quality.py src/jobhunter/l2/v2/project.py \
        src/jobhunter/l2/v2/assemble.py tests/l2/v2/
git commit -m "feat(l2/v2): quality gate policy and claim-level mention projection"
```

---

### Task 10: The twelve case contracts

Encode spec §9's mandatory contracts as regression tests. Fixtures are checked
in as minimal source excerpts + hand-authored v2 emits under
`tests/l2/v2/cases/`; each fixture JSON carries `original_document_hash` and
`case` fields for provenance. Excerpt hashes are new hashes — the tests hash
the excerpt itself and never claim the original hash. The excerpts come from
the local snapshot: extract with

```bash
.venv/bin/python - 'C01' <<'PY'
import json, sys
case = sys.argv[1]
book = json.load(open("data/l2-audit-2026-09-06/casebook.json"))
entry = next(c for c in book if c["case"] == case)
print(entry["postings_and_sources"][0]["markdown"])
PY
```

then copy the minimal lines named below into
`tests/l2/v2/cases/<case>.source.md` (keep whole lines — blocks are lines).
If `data/` is absent on the executing machine, the key sentences below are
sufficient; they are quoted verbatim from the audit evidence.

Contract table — each becomes `tests/l2/v2/test_cases.py::test_<case>`:

| case | fixture source lines (verbatim anchors) | assertions |
| --- | --- | --- |
| C01 Zendesk | "…with a minimum of 8 years of experience and a proven track record…" | emit cites value "8 years" + comparison "a minimum of"; record derives `{gte, 96, None, inclusive_min: True, unit: month}`; a corrupted record storing `max_value: 96` (the v1 legacy shape) fails `fact_mismatch` |
| C02 Unity | "**Gross pay annually : $210,300 - $273,400**" and the footer line containing "sufficient knowledge of English to have professional verbal and written exchanges" | money derives `period: "year"` (unit evidence cites "annually") with `currency: None`; the English line is represented as a `hiring_policy`/`qualification` statement — and an alternative emit marking that block `excluded/eeo` earns the `exclusion_requirement_language` warning |
| C03 Zendesk ML | "Strong SQL skills and experience with cloud data warehouses (Snowflake preferred)" | two statements: required SQL (subject candidate) and preferred Snowflake, sharing evidence spans (shared evidence is legal); mentions "SQL" and "Snowflake" each link to their own statement; no `any_of` group exists between a required degree statement and a `not_required` advanced-degree statement (the invented-alternative shape is asserted absent; a group joining them with `any_of` and no evidence fails `connective_evidence_missing`) |
| C04 Palantir | "CPA or ACCA/ACA (preferred but not required)" | statement importance `preferred`, polarity `positive`; mentions CPA/ACCA/ACA link to it; `project.mention_rows(..., include_ineligible=True)` rows all carry `importance: "preferred"`; schema 2 rejects any area `importance` key (already covered in Task 2, re-asserted here on the C04 fixture) |
| C05 Adobe | one line naming "Java, Spring Boot" and one naming "Docker and Kubernetes" | every named technology in the fixture emit has a mention with `statement_ids`; `normalized_key` preserves nothing beyond casefold ("Spring Boot" → "spring boot" — no splitting); an offline record is `search_eligible: False` (normalization cannot manufacture searchability without the audit gate) |
| C06 Spotify | the line asking in-person attendance "2-3 times per week" and "…base range for this position is $106,147 - $228, 781, plus equity…" | attendance is an `employment_constraint` statement with a frequency fact deriving `{range, 2, 3, per_week}`; the malformed salary keeps a compensation entry with `derived.state: "present_unparsed"` and its evidence intact — distinguishable from an absent compensation (presence `stated`) |
| C07 Unity recruiter | the placeholder first line (from the casebook: the internal add-the-job-profile sentence) plus the English footer line | `source_assessment.usability: "placeholder"` with evidence; `quality.search_eligible` False; the English statement is still present for inspection (partial extraction from an insufficient source is visible, not hidden) |
| C08 empty | the empty string | `annotate("") == []`; `sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"`; an emit claiming any statement over the empty document fails verify (`usability_conflict`/`empty_with_content`); the only passing record has usability `empty`, zero statements, and is ineligible |
| C09 Visa | the sentence offering the degree level "or equivalent experience (minimum … years…)" — pull the exact line from the casebook | an `any_of` group over the degree-route and experience-route statements with connective evidence citing "or"; the experience fact's `condition_ids` names a `qualification_route` condition scoping it to the equivalent-experience route; a corrupted variant flattening both under `all_of` with no evidence fails `connective_evidence_missing` |
| C10 Zillow | (no source fixture) | offline half only: `json.loads` of a truncated emit string raises before assembly — assert `assemble` is never reached and nothing in the contract path can turn malformed output into a record with empty statements; the full attempt-outcome contract lands in increment 2 |
| C11 Workday | "$163,800 USD - $245,800 USD" | distinctness: `derive_money` parses it (grammar OK, per [A4]); a bad quote in the same emit still collects a binding error — one failure class never masks the other (`AssembleError.errors` carries only the quote error; the fact derives cleanly) |
| C12 NVIDIA | a line naming NVIDIA frameworks as preferred examples ("such as …" — pull the exact line from the casebook) | mentions carry `role: "example"` inside an `example_set` with `exhaustive: False`; projected rows keep `importance: "preferred"` and `role: "example"` |

Synthetic minimal pairs (same file, `test_minimal_pairs`): parametrized
`derive_quantity`/`derive_money` cases for at-least/more-than (96-inclusive vs
96-exclusive), and/or (connective evidence required), same-number different
units ("$5,000 per month" vs "per year"), not-required vs prohibited (two
statements differing only in importance `not_required` + polarity `positive`
vs `required` + polarity `negative` — both schema-valid, distinct), CJK
source lines (annotation + binding), duplicate text occurrences (occurrence
index binding), and a prompt-injection line ("Ignore previous instructions…")
asserted to bind and verify like any other text (block ids are metadata, spec
§3).

**Files:**
- Create: `tests/l2/v2/cases/*.source.md` (C01–C09, C11, C12), `tests/l2/v2/cases/*.emit.json`
- Create: `tests/l2/v2/test_cases.py`

**Interfaces:** consumes everything from Tasks 2–9; produces nothing new.

- [ ] **Step 1:** Extract/author the fixture sources and emits (C01 first). Each emit is hand-written against the excerpt, using the Task 7 emit shape.
- [ ] **Step 2:** Write `test_cases.py` with one test per case per the table, loading fixtures relative to `__file__`, assembling, verifying, and asserting the contract plus the corrupted-variant rejection where the table names one.
- [ ] **Step 3:** Run — `uv run pytest tests/l2/v2/test_cases.py -v`. Every contract must fail before its supporting behavior existed (they pass now only because Tasks 2–9 landed; any failure here is a real contract gap — fix the module, never the contract).
- [ ] **Step 4:** Full gates: `uv run pytest tests/ -v && uv run ruff check . && uv run mypy` (store-backed tests may skip without Postgres; that is their normal offline behavior).
- [ ] **Step 5: Commit**

```bash
git add tests/l2/v2/cases/ tests/l2/v2/test_cases.py
git commit -m "test(l2/v2): the twelve audit case contracts and synthetic minimal pairs"
```

---

### Task 11: Documentation and freeze

**Files:**
- Modify: `src/jobhunter/CLAUDE.md` (the `l2/` bullet: add the `l2/v2/` modules, `blocks/1`, schema `2`, validator `9` v1 / `10` v2)
- Modify: `docs/README.md` (mark this plan executed for increment 1)
- Modify: `tests/CLAUDE.md` (mention `tests/l2/v2/` and the case fixtures)

- [ ] **Step 1:** Write the doc updates (plain sentences, no hype).
- [ ] **Step 2:** Final full run: `uv run pytest tests/ -v && uv run ruff check . && uv run mypy` — paste the summary lines into the PR/merge description as evidence.
- [ ] **Step 3: Commit**

```bash
git add src/jobhunter/CLAUDE.md docs/README.md tests/CLAUDE.md
git commit -m "docs: record parsing v2 increment 1 — offline contract built and frozen"
```

- [ ] **Step 4:** Merge decision via superpowers:finishing-a-development-branch. After merge, identifiers `blocks/1`, schema `2`, validator `9`/`10`, `parsing-rules/2`, `aliases/1` are frozen — any change bumps.

---

## Self-review checklist (run after writing, before execution)

- Spec coverage: §3 record contract → Tasks 2–7; §3 grammars → Tasks 5–6; §6 quality → Task 9; §3 projection semantics → Task 9; §9 case contracts + minimal pairs → Task 10; [A3] identifier allocation → Tasks 1/5. Not covered here by design: §4 prompts, §5 flow, [A1]/[A2] runner work (increment 2); §7 persistence/reads (increment 3).
- Known deferred decisions (do not silently resolve during execution — flag if hit): cross-block multi-line evidence rendering in reprompts; `conflicting` state producer (auditor only); alias policy beyond casefold.
