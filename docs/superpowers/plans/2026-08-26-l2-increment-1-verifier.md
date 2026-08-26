# L2 Increment 1 — Verifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the machine-verification layer of the L2 spec — quote objects, span
resolution, versioned fact transforms, the JSON schemas, the pure `verify()` check
suite, and the file-based `job-hunter verify` CLI — ending with `validator/1` frozen.

**Architecture:** A new pure sub-package `src/jobhunter/l2/` with zero I/O and no LLM
anywhere: `quotes.py` (span resolution — code computes offsets, never the model),
`transforms.py` (versioned L1 fact parsers), `schemas.py` + packaged JSON Schemas,
`report.py` (findings), `verify.py` (the check table from spec §3.3). One addition to
`markdown.py` (`block_intervals`). CLI gets a file-based `verify` command; archive-
and store-addressed verification arrives in increment 2.

**Tech Stack:** Python 3.12, uv, jsonschema (new runtime dep), pytest, mypy --strict,
ruff (line 100).

**Spec:** `docs/2026-08-26-l2-extraction-harness.md` (normative; §3 is this
increment). Record format: `docs/2026-08-17-parsing-direction.md`.

## Roadmap (spec §13)

| milestone | scope | plan |
| --- | --- | --- |
| **M1 (this plan)** | verifier: quotes/spans, transforms, schemas, `verify()`, file CLI, goldens incl. CJK; `validator/1` freezes at the end | this document |
| M2 | harness: attempt archive objects, engines (openai-compat + claude-cli), state machine, queue, locks, caps, `extract`/`rebuild`/`review` verbs, `status` extension, fake-server integration tests, canary, first supervised backfill | own plan when M1 merges |
| M3 | quality loop: 5% k=3 audit, agreement metrics, weekly `consolidate` (drift report, audit queue, boilerplate cross-check, memos), refuter, attention alerts | own plan |
| M4 | agent access verbs (`q`/`propose`/`curate`); MCP wrapper after first real use | own plan |

## Global Constraints

- Python ≥ 3.12; uv only; ruff line-length 100 (`E,F,I,UP,B,SIM`); `mypy --strict`.
- `l2/` is pure: no I/O, no network, no env reads, no LLM. Only the CLI task touches files.
- Offsets are Unicode codepoints, half-open `[start, end)`; `markdown[s:e] == text` is the stored representation. No fuzzy repair anywhere (spec §3.1–3.2).
- Hashing only via `jobhunter.hashing.sha256_hex`; never `hashlib` directly.
- `VALIDATOR_VERSION = "1"` names the whole suite incl. thresholds: claim quote error <5 or >600 codepoints, warn <15 or >280; fact anchors ≥2; structure ops `{AND, OR}`, arity ≥2, depth ≤5 (spec §12, ruled).
- Every command accepts `--json`; exit 0 normal, 2 systemic; `verify` adds exit 1 = "ran fine, findings failed" (spec §3.3, ruled).
- Work on branch `l2/increment-1-verifier`; never commit to `main` directly.

## File Structure

- Create `src/jobhunter/l2/__init__.py` — exports `verify`, `Report`, `VALIDATOR_VERSION`.
- Create `src/jobhunter/l2/quotes.py` — quote resolution + line:col derivation.
- Create `src/jobhunter/l2/transforms.py` — `TRANSFORMS` registry, v1 parsers.
- Create `src/jobhunter/l2/report.py` — `Finding`, `Report`.
- Create `src/jobhunter/l2/verify.py` — `verify(extraction, markdown) -> Report`.
- Create `src/jobhunter/l2/schemas.py` — loader over packaged JSON files.
- Create `src/jobhunter/l2/schemas_data/1/record.schema.json`, `emit.schema.json`.
- Modify `src/jobhunter/markdown.py` — add `block_intervals(md) -> list[tuple[int, int]]`.
- Modify `src/jobhunter/cli.py` — add `verify` command.
- Create `tests/l2/__init__.py`, `tests/l2/test_quotes.py`, `test_transforms.py`,
  `test_schemas.py`, `test_verify.py`, `test_verify_golden.py`; fixtures under
  `tests/l2/fixtures/` (incl. a synthetic CJK pair); extend `tests/test_markdown.py`
  and `tests/test_cli.py`.

---

### Task 1: Branch + `block_intervals` in markdown.py

**Files:**
- Modify: `src/jobhunter/markdown.py` (append after `strip_markdown`)
- Test: `tests/test_markdown.py` (append)

**Interfaces:**
- Produces: `block_intervals(md: str) -> list[tuple[int, int]]` — codepoint intervals
  `[start, end)` of maximal runs of non-blank lines; blank line = `""` after `strip()`.
  Interval end excludes the trailing newline. Used by Task 5's `block_bounds` check.

- [ ] **Step 1: Create the branch**

```bash
git checkout -b l2/increment-1-verifier
```

- [ ] **Step 2: Write the failing tests** (append to `tests/test_markdown.py`)

```python
from jobhunter.markdown import block_intervals


def test_block_intervals_basic() -> None:
    md = "## Head\n\n- a\n- b\n\npara"
    assert block_intervals(md) == [(0, 7), (9, 17), (19, 23)]


def test_block_intervals_empty_and_trailing() -> None:
    assert block_intervals("") == []
    assert block_intervals("\n\n") == []
    md = "one\n"
    assert block_intervals(md) == [(0, 3)]


def test_block_intervals_cover_all_nonblank() -> None:
    md = "a\nb\n\n\nc"
    ivs = block_intervals(md)
    assert ivs == [(0, 3), (5, 6)]
    for s, e in ivs:
        assert md[s:e].strip() == md[s:e]
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_markdown.py -k block_intervals -v`
Expected: FAIL — `ImportError: cannot import name 'block_intervals'`

- [ ] **Step 4: Implement** (append to `src/jobhunter/markdown.py`)

```python
def block_intervals(md: str) -> list[tuple[int, int]]:
    """Codepoint intervals [start, end) of maximal runs of non-blank lines.

    Derived on demand from canonical markdown (parsing-direction: blocks(markdown)
    is a function, not a stored table). End excludes the newline separator.
    """
    intervals: list[tuple[int, int]] = []
    pos = 0
    start: int | None = None
    end = 0
    for line in md.split("\n"):
        if line.strip():
            if start is None:
                start = pos
            end = pos + len(line)
        elif start is not None:
            intervals.append((start, end))
            start = None
        pos += len(line) + 1
    if start is not None:
        intervals.append((start, end))
    return intervals
```

- [ ] **Step 5: Run tests, lint, typecheck; commit**

Run: `uv run pytest tests/test_markdown.py -q && uv run ruff check . && uv run mypy`
Expected: all pass.

```bash
git add src/jobhunter/markdown.py tests/test_markdown.py
git commit -m "feat(l2): block_intervals over canonical markdown"
```

---

### Task 2: `l2/quotes.py` — span resolution

**Files:**
- Create: `src/jobhunter/l2/__init__.py` (empty for now)
- Create: `src/jobhunter/l2/quotes.py`
- Test: `tests/l2/__init__.py` (empty), `tests/l2/test_quotes.py`

**Interfaces:**
- Produces (Tasks 5–9 consume):
  - `@dataclass(frozen=True) Quote(text: str, span: tuple[int, int], occurrence: int)`
  - `find_occurrences(md: str, text: str) -> list[int]` — all match starts, ascending.
  - `longest_matching_prefix(md: str, text: str) -> int` — largest k with `text[:k] in md`.
  - `resolve_quote(md: str, text: str, occurrence: int | None = None) -> Quote` —
    raises `QuoteNotFound(text, longest_prefix)` on zero matches,
    `AmbiguousQuote(text, starts)` on ≥2 matches with no/invalid occurrence.
  - `occurrence_index(md: str, text: str, start: int) -> int` — which instance a span
    points at; `-1` if `start` is not a match start.
  - `line_col(md: str, offset: int) -> tuple[int, int]` — 1-based, codepoints.

- [ ] **Step 1: Write the failing tests** (`tests/l2/test_quotes.py`)

```python
import pytest

from jobhunter.l2.quotes import (
    AmbiguousQuote,
    QuoteNotFound,
    find_occurrences,
    line_col,
    longest_matching_prefix,
    occurrence_index,
    resolve_quote,
)

MD = "## Skills\n\n- **Python** and Go\n- Python for scripting\n\n5年以上の経験 🎯 required"


def test_find_occurrences() -> None:
    assert find_occurrences(MD, "Python") == [14, 33]
    assert find_occurrences(MD, "absent") == []


def test_resolve_unique() -> None:
    q = resolve_quote(MD, "**Python** and Go")
    assert q.span == (12, 29)
    assert q.occurrence == 0
    assert MD[q.span[0] : q.span[1]] == q.text


def test_resolve_ambiguous_needs_occurrence() -> None:
    with pytest.raises(AmbiguousQuote) as exc:
        resolve_quote(MD, "Python")
    assert exc.value.starts == [14, 33]
    q = resolve_quote(MD, "Python", occurrence=1)
    assert q.span == (33, 39)
    with pytest.raises(AmbiguousQuote):
        resolve_quote(MD, "Python", occurrence=2)


def test_resolve_not_found_prefix_diagnostic() -> None:
    with pytest.raises(QuoteNotFound) as exc:
        resolve_quote(MD, "Python for scripts")
    assert exc.value.longest_prefix == len("Python for script")


def test_cjk_and_astral_are_codepoints() -> None:
    q = resolve_quote(MD, "5年以上の経験")
    assert q.span[1] - q.span[0] == 7
    assert MD[q.span[0] : q.span[1]] == "5年以上の経験"
    emoji = resolve_quote(MD, "🎯")
    assert emoji.span[1] - emoji.span[0] == 1  # one codepoint, not UTF-16 pair


def test_occurrence_index() -> None:
    assert occurrence_index(MD, "Python", 14) == 0
    assert occurrence_index(MD, "Python", 33) == 1
    assert occurrence_index(MD, "Python", 15) == -1


def test_line_col() -> None:
    assert line_col(MD, 0) == (1, 1)
    assert line_col(MD, 12) == (3, 3)  # "- **Python**..." line, after "- "
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/l2/test_quotes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobhunter.l2'`

- [ ] **Step 3: Implement** (`src/jobhunter/l2/quotes.py`)

```python
"""Quote objects and deterministic span resolution.

The LLM emits verbatim text (+ optional occurrence); code computes offsets
(spec §3.2). Offsets are Unicode codepoints, half-open [start, end). No fuzzy
repair, ever — a quote that does not appear exactly is a fabrication signal.
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
```

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `uv run pytest tests/l2 -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/l2 tests/l2
git commit -m "feat(l2): quote objects and deterministic span resolution"
```

---

### Task 3: `l2/transforms.py` — versioned fact parsers

**Files:**
- Create: `src/jobhunter/l2/transforms.py`
- Test: `tests/l2/test_transforms.py`

**Interfaces:**
- Produces (Task 7 consumes):
  - `VALIDATOR_VERSION: str = "1"`
  - `parse_experience_months(text: str) -> dict[str, object] | None` → `{"min": int, "max": int | None}` in months.
  - `parse_compensation(text: str) -> dict[str, object] | None` → `{"min": int, "max": int, "currency": "USD", "period": "year" | "hour"}`.
  - `parse_deadline(text: str) -> dict[str, object] | None` → `{"date": "YYYY-MM-DD"}`.
  - `TRANSFORMS: dict[str, dict[str, Callable[[str], dict[str, object] | None]]]` —
    `TRANSFORMS["1"] = {"experience_months": ..., "compensation": ..., "deadline": ...}`.
- v1 grammar (frozen with the validator): ranges `0-2 YOE` / `3 – 5 years`; floors
  `5+ years`; exact `2 years`; `$130,000 - $150,000` / `$130K–$150K` (+ `/hour`,
  `per hour` → hour, else year; `$` only → USD); `July 17, 2026` English dates.
  Anything else returns `None` (null-over-guess: never guess a parse).

- [ ] **Step 1: Write the failing tests** (`tests/l2/test_transforms.py`)

```python
import pytest

from jobhunter.l2.transforms import (
    TRANSFORMS,
    VALIDATOR_VERSION,
    parse_compensation,
    parse_deadline,
    parse_experience_months,
)


def test_registry_shape() -> None:
    assert VALIDATOR_VERSION == "1"
    assert set(TRANSFORMS["1"]) == {"experience_months", "compensation", "deadline"}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0-2 YOE", {"min": 0, "max": 24}),
        ("3 – 5 years", {"min": 36, "max": 60}),
        ("5+ years", {"min": 60, "max": None}),
        ("2 years", {"min": 24, "max": 24}),
        ("12 yrs", {"min": 144, "max": 144}),
        ("many years", None),
        ("", None),
    ],
)
def test_experience(text: str, expected: dict[str, object] | None) -> None:
    assert parse_experience_months(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "$130,000 - $150,000",
            {"min": 130000, "max": 150000, "currency": "USD", "period": "year"},
        ),
        ("$130K–$150K", {"min": 130000, "max": 150000, "currency": "USD", "period": "year"}),
        ("$45 - $55 per hour", {"min": 45, "max": 55, "currency": "USD", "period": "hour"}),
        ("competitive salary", None),
    ],
)
def test_compensation(text: str, expected: dict[str, object] | None) -> None:
    assert parse_compensation(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("July 17, 2026", {"date": "2026-07-17"}),
        ("until March 3, 2027", {"date": "2027-03-03"}),
        ("soon", None),
    ],
)
def test_deadline(text: str, expected: dict[str, object] | None) -> None:
    assert parse_deadline(text) == expected
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/l2/test_transforms.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement** (`src/jobhunter/l2/transforms.py`)

```python
"""Versioned L1 fact transforms: the LLM points at a span, code computes the value.

Facts are re-derived from anchor text and compared structurally — never checked as
literal numbers (spec §3.3 facts_rederive; parsing-direction review finding 6).
The grammar below is frozen as part of VALIDATOR_VERSION.
"""

from __future__ import annotations

import re
from collections.abc import Callable

VALIDATOR_VERSION = "1"

_RANGE = re.compile(r"(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)
_FLOOR = re.compile(r"(\d+)\s*\+\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)
_EXACT = re.compile(r"(\d+)\s*(?:years?|yrs?|yoe)\b", re.IGNORECASE)

_MONEY = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})*|\d+)\s*(k)?\s*(?:-|–|—|to)\s*"
    r"\$\s*(\d{1,3}(?:,\d{3})*|\d+)\s*(k)?",
    re.IGNORECASE,
)
_HOURLY = re.compile(r"(?:/\s*(?:hr|hour)|per\s+hour)\b", re.IGNORECASE)

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "january february march april may june july august september october "
        "november december".split()
    )
}
_DATE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")


def parse_experience_months(text: str) -> dict[str, object] | None:
    if m := _RANGE.search(text):
        return {"min": int(m.group(1)) * 12, "max": int(m.group(2)) * 12}
    if m := _FLOOR.search(text):
        return {"min": int(m.group(1)) * 12, "max": None}
    if m := _EXACT.search(text):
        months = int(m.group(1)) * 12
        return {"min": months, "max": months}
    return None


def _amount(digits: str, k_suffix: str | None) -> int:
    value = int(digits.replace(",", ""))
    return value * 1000 if k_suffix else value


def parse_compensation(text: str) -> dict[str, object] | None:
    m = _MONEY.search(text)
    if not m:
        return None
    period = "hour" if _HOURLY.search(text) else "year"
    return {
        "min": _amount(m.group(1), m.group(2)),
        "max": _amount(m.group(3), m.group(4)),
        "currency": "USD",
        "period": period,
    }


def parse_deadline(text: str) -> dict[str, object] | None:
    m = _DATE.search(text)
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if month is None:
        return None
    return {"date": f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"}


TRANSFORMS: dict[str, dict[str, Callable[[str], dict[str, object] | None]]] = {
    "1": {
        "experience_months": parse_experience_months,
        "compensation": parse_compensation,
        "deadline": parse_deadline,
    }
}
```

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `uv run pytest tests/l2 -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/l2/transforms.py tests/l2/test_transforms.py
git commit -m "feat(l2): versioned fact transforms (validator/1 grammar)"
```

---

### Task 4: JSON Schemas + loader

**Files:**
- Create: `src/jobhunter/l2/schemas_data/1/record.schema.json`
- Create: `src/jobhunter/l2/schemas_data/1/emit.schema.json`
- Create: `src/jobhunter/l2/schemas.py`
- Modify: `pyproject.toml` (deps)
- Test: `tests/l2/test_schemas.py`

**Interfaces:**
- Produces (Task 5 consumes): `record_schema(version: str) -> dict[str, Any]`,
  `emit_schema(version: str) -> dict[str, Any]`; both raise `KeyError` on unknown
  versions. `validate_record(extraction: dict, version: str) -> list[str]` returns
  human-readable error strings (empty = valid) using `jsonschema.Draft202012Validator`.

- [ ] **Step 1: Add the dependency**

```bash
uv add "jsonschema>=4.23" && uv add --group dev types-jsonschema
```

- [ ] **Step 2: Write the failing tests** (`tests/l2/test_schemas.py`)

```python
import pytest

from jobhunter.l2.schemas import emit_schema, record_schema, validate_record
from tests.l2.conftest import minimal_record


def test_schemas_load() -> None:
    assert record_schema("1")["$defs"]["quote"]["required"] == ["text", "span", "occurrence"]
    assert "span" not in emit_schema("1")["$defs"]["quote"]["properties"]
    with pytest.raises(KeyError):
        record_schema("99")


def test_minimal_record_validates() -> None:
    assert validate_record(minimal_record(), "1") == []


def test_extra_property_rejected() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["claims"][0]["quote"]["extra"] = 1
    errors = validate_record(rec, "1")
    assert errors and "extra" in errors[0]
```

Also create `tests/l2/conftest.py` with the shared fixture builder used by every
verify test (documents the canonical shapes once):

```python
"""Shared builders: a small canonical document + a valid extraction record."""

from __future__ import annotations

import copy
from typing import Any

from jobhunter.hashing import sha256_hex

DOC_MD = (
    "## Requirements\n\n"
    "- **Python** and distributed systems\n"
    "- 0-2 YOE preferred\n\n"
    "## About\n\n"
    "Equal opportunity employer."
)

_RECORD: dict[str, Any] = {
    "document": {
        "document_hash": sha256_hex(DOC_MD.encode("utf-8")),
        "normalizer_version": "md/1",
    },
    "facts": {
        "experience_months": {
            "min": 0,
            "max": 24,
            "scope": "total",
            "anchor": {"text": "0-2 YOE", "span": [55, 62], "occurrence": 0},
        },
        "compensation": [],
        "deadline": None,
        "boilerplate_spans": [
            {"text": "Equal opportunity employer.", "span": [84, 111], "occurrence": 0}
        ],
    },
    "demand_profile": {
        "areas": [
            {
                "id": "a1",
                "name": "Backend engineering",
                "kind": "technical",
                "importance": "required",
                "level": None,
                "claims": [
                    {
                        "id": "c1",
                        "quote": {
                            "text": "**Python** and distributed systems",
                            "span": [18, 52],
                            "occurrence": 0,
                        },
                        "importance": "required",
                        "level": None,
                        "level_evidence": None,
                        "negated": False,
                        "threshold": None,
                        "qualifiers": [],
                        "evidence_sources": [],
                    },
                    {
                        "id": "c2",
                        "quote": {"text": "0-2 YOE preferred", "span": [55, 72], "occurrence": 0},
                        "importance": "preferred",
                        "level": None,
                        "level_evidence": None,
                        "negated": False,
                        "threshold": None,
                        "qualifiers": [],
                        "evidence_sources": [],
                    },
                ],
                "context": [],
                "structure": {"op": "AND", "of": ["c1", "c2"]},
                "mentions": ["Python"],
                "description": {"text": None, "synthesis": "none", "run": None},
            }
        ],
        "interview_evaluated": [],
    },
    "extraction": {
        "model": "test-model",
        "prompt_version": "demand-profile/v1",
        "schema_version": "1",
        "validator_version": "1",
        "at": "2026-08-26T00:00:00Z",
    },
}


def minimal_record() -> dict[str, Any]:
    return copy.deepcopy(_RECORD)
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/l2/test_schemas.py -v`
Expected: FAIL — module not found.

- [ ] **Step 4: Write the schema files and loader**

`src/jobhunter/l2/schemas_data/1/record.schema.json` (complete file):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "job-hunter L2 extraction record, schema_version 1",
  "type": "object",
  "additionalProperties": false,
  "required": ["document", "facts", "demand_profile", "extraction"],
  "$defs": {
    "span": {
      "type": "array",
      "minItems": 2,
      "maxItems": 2,
      "prefixItems": [
        { "type": "integer", "minimum": 0 },
        { "type": "integer", "minimum": 1 }
      ],
      "items": false,
      "description": "Half-open [start,end) in Unicode CODEPOINTS (not bytes, not UTF-16 units) into the canonical markdown whose UTF-8 encoding hashes to document_hash."
    },
    "quote": {
      "type": "object",
      "additionalProperties": false,
      "required": ["text", "span", "occurrence"],
      "properties": {
        "text": { "type": "string", "minLength": 1 },
        "span": { "$ref": "#/$defs/span" },
        "occurrence": { "type": "integer", "minimum": 0 }
      }
    },
    "importance": { "enum": ["required", "preferred", "contextual"] },
    "level": { "enum": ["expert", "proficient", "working", "exposure", null] },
    "structure_node": {
      "type": "object",
      "additionalProperties": false,
      "required": ["op", "of"],
      "properties": {
        "op": { "enum": ["AND", "OR"] },
        "of": {
          "type": "array",
          "minItems": 2,
          "items": {
            "anyOf": [{ "type": "string" }, { "$ref": "#/$defs/structure_node" }]
          }
        }
      }
    },
    "claim": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "id", "quote", "importance", "level", "level_evidence",
        "negated", "threshold", "qualifiers", "evidence_sources"
      ],
      "properties": {
        "id": { "type": "string", "minLength": 1 },
        "quote": { "$ref": "#/$defs/quote" },
        "importance": { "$ref": "#/$defs/importance" },
        "level": { "$ref": "#/$defs/level" },
        "level_evidence": { "type": ["string", "null"] },
        "negated": { "type": "boolean" },
        "threshold": { "type": ["object", "null"] },
        "qualifiers": { "type": "array", "items": { "type": "string" } },
        "evidence_sources": { "type": "array", "items": { "type": "string" } }
      }
    },
    "area": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "id", "name", "kind", "importance", "level", "claims",
        "context", "structure", "mentions", "description"
      ],
      "properties": {
        "id": { "type": "string", "minLength": 1 },
        "name": { "type": "string", "minLength": 1 },
        "kind": { "enum": ["technical", "capability", "trait", "credential", "constraint"] },
        "importance": { "$ref": "#/$defs/importance" },
        "level": { "$ref": "#/$defs/level" },
        "claims": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/claim" } },
        "context": { "type": "array", "items": { "$ref": "#/$defs/quote" } },
        "structure": {
          "anyOf": [{ "$ref": "#/$defs/structure_node" }, { "type": "null" }]
        },
        "mentions": { "type": "array", "items": { "type": "string" } },
        "description": {
          "anyOf": [
            {
              "type": "object",
              "additionalProperties": false,
              "required": ["text", "synthesis", "run"],
              "properties": {
                "text": { "type": ["string", "null"] },
                "synthesis": { "enum": ["llm", "template", "none"] },
                "run": { "type": ["string", "null"] }
              }
            },
            { "type": "null" }
          ]
        }
      }
    }
  },
  "properties": {
    "posting": { "type": "object" },
    "document": {
      "type": "object",
      "additionalProperties": false,
      "required": ["document_hash", "normalizer_version"],
      "properties": {
        "document_hash": { "type": "string", "pattern": "^[0-9a-f]{64}$" },
        "normalizer_version": { "type": "string" }
      }
    },
    "facts": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "experience_months": {
          "anyOf": [
            {
              "type": "object",
              "additionalProperties": false,
              "required": ["min", "max", "scope", "anchor"],
              "properties": {
                "min": { "type": "integer", "minimum": 0 },
                "max": { "type": ["integer", "null"], "minimum": 0 },
                "scope": { "type": ["string", "null"] },
                "anchor": { "$ref": "#/$defs/quote" }
              }
            },
            { "type": "null" }
          ]
        },
        "compensation": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["min", "max", "currency", "period", "anchor"],
            "properties": {
              "min": { "type": ["integer", "null"] },
              "max": { "type": ["integer", "null"] },
              "currency": { "type": ["string", "null"] },
              "period": { "type": ["string", "null"] },
              "condition": { "type": ["string", "null"] },
              "anchor": { "$ref": "#/$defs/quote" }
            }
          }
        },
        "deadline": {
          "anyOf": [
            {
              "type": "object",
              "additionalProperties": false,
              "required": ["date", "anchor"],
              "properties": {
                "date": { "type": "string", "format": "date" },
                "anchor": { "$ref": "#/$defs/quote" }
              }
            },
            { "type": "null" }
          ]
        },
        "boilerplate_spans": { "type": "array", "items": { "$ref": "#/$defs/quote" } }
      }
    },
    "demand_profile": {
      "type": "object",
      "additionalProperties": false,
      "required": ["areas", "interview_evaluated"],
      "properties": {
        "areas": { "type": "array", "items": { "$ref": "#/$defs/area" } },
        "interview_evaluated": { "type": "array", "items": { "type": "string" } }
      }
    },
    "links": { "type": "array" },
    "extraction": {
      "type": "object",
      "additionalProperties": false,
      "required": ["model", "prompt_version", "schema_version", "validator_version", "at"],
      "properties": {
        "model": { "type": "string" },
        "prompt_version": { "type": "string" },
        "schema_version": { "type": "string" },
        "validator_version": { "type": "string" },
        "at": { "type": "string" },
        "cost_usd": { "type": ["number", "null"] }
      }
    },
    "verification": { "type": "object" }
  }
}
```

`src/jobhunter/l2/schemas_data/1/emit.schema.json` — identical structure except the
`quote` def is what the model emits (code adds `span`/`occurrence` after resolution)
and there is no `document`/`extraction`/`verification` (the harness adds those).
Complete file:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "job-hunter L2 emit schema (what the LLM returns), schema_version 1",
  "type": "object",
  "additionalProperties": false,
  "required": ["facts", "demand_profile"],
  "$defs": {
    "quote": {
      "type": "object",
      "additionalProperties": false,
      "required": ["text"],
      "properties": {
        "text": { "type": "string", "minLength": 1 },
        "occurrence": { "type": "integer", "minimum": 0 },
        "hint": { "type": "string" }
      },
      "description": "Verbatim text from the canonical markdown, markup included, no newlines. Code locates the span; never emit offsets."
    },
    "importance": { "enum": ["required", "preferred", "contextual"] },
    "level": { "enum": ["expert", "proficient", "working", "exposure", null] },
    "structure_node": {
      "type": "object",
      "additionalProperties": false,
      "required": ["op", "of"],
      "properties": {
        "op": { "enum": ["AND", "OR"] },
        "of": {
          "type": "array",
          "minItems": 2,
          "items": {
            "anyOf": [{ "type": "string" }, { "$ref": "#/$defs/structure_node" }]
          }
        }
      }
    },
    "claim": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "quote", "importance", "level", "negated"],
      "properties": {
        "id": { "type": "string", "minLength": 1 },
        "quote": { "$ref": "#/$defs/quote" },
        "importance": { "$ref": "#/$defs/importance" },
        "level": { "$ref": "#/$defs/level" },
        "level_evidence": { "type": ["string", "null"] },
        "negated": { "type": "boolean" },
        "threshold": { "type": ["object", "null"] },
        "qualifiers": { "type": "array", "items": { "type": "string" } },
        "evidence_sources": { "type": "array", "items": { "type": "string" } }
      }
    },
    "area": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "name", "kind", "importance", "level", "claims"],
      "properties": {
        "id": { "type": "string", "minLength": 1 },
        "name": { "type": "string", "minLength": 1 },
        "kind": { "enum": ["technical", "capability", "trait", "credential", "constraint"] },
        "importance": { "$ref": "#/$defs/importance" },
        "level": { "$ref": "#/$defs/level" },
        "claims": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/claim" } },
        "context": { "type": "array", "items": { "$ref": "#/$defs/quote" } },
        "structure": {
          "anyOf": [{ "$ref": "#/$defs/structure_node" }, { "type": "null" }]
        },
        "mentions": { "type": "array", "items": { "type": "string" } }
      }
    }
  },
  "properties": {
    "facts": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "experience_months": {
          "anyOf": [
            {
              "type": "object",
              "additionalProperties": false,
              "required": ["anchor"],
              "properties": {
                "scope": { "type": ["string", "null"] },
                "anchor": { "$ref": "#/$defs/quote" }
              }
            },
            { "type": "null" }
          ]
        },
        "compensation": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["anchor"],
            "properties": {
              "condition": { "type": ["string", "null"] },
              "anchor": { "$ref": "#/$defs/quote" }
            }
          }
        },
        "deadline": {
          "anyOf": [
            {
              "type": "object",
              "additionalProperties": false,
              "required": ["anchor"],
              "properties": { "anchor": { "$ref": "#/$defs/quote" } }
            },
            { "type": "null" }
          ]
        },
        "boilerplate_spans": { "type": "array", "items": { "$ref": "#/$defs/quote" } }
      }
    },
    "demand_profile": {
      "type": "object",
      "additionalProperties": false,
      "required": ["areas", "interview_evaluated"],
      "properties": {
        "areas": { "type": "array", "items": { "$ref": "#/$defs/area" } },
        "interview_evaluated": { "type": "array", "items": { "type": "string" } }
      }
    }
  }
}
```

Note the emit-side facts carry only `anchor` (+`scope`/`condition` judgment fields):
the LLM points at spans; **code computes min/max/currency/date** via Task 3's
transforms at harness time (M2). The record schema stores both.

`src/jobhunter/l2/schemas.py`:

```python
"""Loader for packaged, versioned JSON Schemas (spec §3.3: archived, checked in)."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

import jsonschema


@cache
def _load(version: str, name: str) -> dict[str, Any]:
    root = resources.files("jobhunter.l2.schemas_data")
    path = root / version / f"{name}.schema.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KeyError(f"unknown schema version: {version}") from None
    data: dict[str, Any] = json.loads(raw)
    return data


def record_schema(version: str) -> dict[str, Any]:
    return _load(version, "record")


def emit_schema(version: str) -> dict[str, Any]:
    return _load(version, "emit")


def validate_record(extraction: dict[str, Any], version: str) -> list[str]:
    validator = jsonschema.Draft202012Validator(record_schema(version))
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(extraction), key=lambda e: list(e.absolute_path))
    ]
```

- [ ] **Step 5: Run tests, lint, typecheck; commit**

Run: `uv run pytest tests/l2 -q && uv run ruff check . && uv run mypy`
Expected: all pass (schema files ship via hatchling's default package-data inclusion).

```bash
git add pyproject.toml uv.lock src/jobhunter/l2/schemas.py src/jobhunter/l2/schemas_data tests/l2
git commit -m "feat(l2): record + emit JSON schemas v1 and loader"
```

---

### Task 5: `report.py` + `verify()` core checks

**Files:**
- Create: `src/jobhunter/l2/report.py`
- Create: `src/jobhunter/l2/verify.py`
- Modify: `src/jobhunter/l2/__init__.py`
- Test: `tests/l2/test_verify.py`

**Interfaces:**
- Produces:
  - `Finding(check: str, path: str, code: str, detail: dict[str, object], severity: str)`
    (frozen dataclass; severity `"error" | "warning"`).
  - `Report(validator_version: str)` with `.findings: list[Finding]`,
    `.metrics: dict[str, object]`, `.error(check, path, code, **detail)`,
    `.warn(check, path, code, **detail)`, `.status` property (`"pass"` iff no
    error-severity findings), `.to_json() -> dict[str, object]`.
  - `verify(extraction: dict[str, Any], markdown: str) -> Report` running (this task):
    `doc_binding` (hard fail-fast), `schema`, `attribution`, `block_bounds`.
  - `iter_quote_objects(extraction) -> Iterator[tuple[str, dict[str, Any]]]` over
    claim quotes (`areas[a1].claims[c1].quote`), context (`areas[a1].context[0]`),
    fact anchors (`facts.experience_months.anchor`, `facts.compensation[0].anchor`,
    `facts.deadline.anchor`), boilerplate (`facts.boilerplate_spans[0]`).
- Consumes: Task 1 `block_intervals`, Task 2 `occurrence_index`, Task 4 `validate_record`,
  `jobhunter.hashing.sha256_hex`.

- [ ] **Step 1: Write the failing tests** (`tests/l2/test_verify.py`)

```python
from typing import Any

from jobhunter.l2 import VALIDATOR_VERSION, verify
from tests.l2.conftest import DOC_MD, minimal_record


def codes(report: Any, check: str) -> list[str]:
    return [f.code for f in report.findings if f.check == check]


def test_valid_record_passes() -> None:
    report = verify(minimal_record(), DOC_MD)
    assert report.status == "pass"
    assert report.validator_version == VALIDATOR_VERSION


def test_doc_binding_hard_fail() -> None:
    report = verify(minimal_record(), DOC_MD + " tampered")
    assert report.status == "fail"
    assert codes(report, "doc_binding") == ["hash_mismatch"]
    assert len(report.findings) == 1  # fail-fast: nothing else ran


def test_schema_invalid() -> None:
    rec = minimal_record()
    del rec["demand_profile"]["areas"][0]["claims"][0]["negated"]
    report = verify(rec, DOC_MD)
    assert report.status == "fail"
    assert codes(report, "schema")


def test_attribution_text_mismatch_has_prefix() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["claims"][1]["quote"]["text"] = "0-2 YOE preferrd"
    report = verify(rec, DOC_MD)
    assert codes(report, "attribution") == ["text_mismatch"]
    finding = next(f for f in report.findings if f.check == "attribution")
    assert finding.detail["longest_prefix"] == len("0-2 YOE preferr")


def test_attribution_span_bounds_and_occurrence() -> None:
    rec = minimal_record()
    rec["facts"]["experience_months"]["anchor"]["span"] = [55, 9999]
    report = verify(rec, DOC_MD)
    assert codes(report, "attribution") == ["span_bounds"]

    rec2 = minimal_record()
    rec2["facts"]["experience_months"]["anchor"]["occurrence"] = 1
    report2 = verify(rec2, DOC_MD)
    assert codes(report2, "attribution") == ["occurrence_mismatch"]


def test_block_bounds_rejects_newline_quote() -> None:
    rec = minimal_record()
    claim = rec["demand_profile"]["areas"][0]["claims"][0]
    claim["quote"]["text"] = DOC_MD[18:55]  # spans two list lines
    claim["quote"]["span"] = [18, 55]
    report = verify(rec, DOC_MD)
    assert "newline_in_quote" in codes(report, "block_bounds")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/l2/test_verify.py -v`
Expected: FAIL — `verify` not importable.

- [ ] **Step 3: Implement**

`src/jobhunter/l2/report.py`:

```python
"""Verification findings. machine-verified != true: this reports attribution and
internal consistency only (spec §3.4)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Finding:
    check: str
    path: str
    code: str
    detail: dict[str, object]
    severity: str  # "error" | "warning"


@dataclass
class Report:
    validator_version: str
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, object] = field(default_factory=dict)

    def error(self, check: str, path: str, code: str, **detail: object) -> None:
        self.findings.append(Finding(check, path, code, dict(detail), "error"))

    def warn(self, check: str, path: str, code: str, **detail: object) -> None:
        self.findings.append(Finding(check, path, code, dict(detail), "warning"))

    @property
    def status(self) -> str:
        return "fail" if any(f.severity == "error" for f in self.findings) else "pass"

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status,
            "validator_version": self.validator_version,
            "findings": [
                {
                    "check": f.check,
                    "path": f.path,
                    "code": f.code,
                    "severity": f.severity,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
            "metrics": self.metrics,
        }
```

`src/jobhunter/l2/verify.py` (this task's slice; Tasks 6–7 extend it):

```python
"""The verifier: one pure function over (extraction JSON, canonical markdown).

Inline validator in the harness retry loop, standalone audit, and memo linter —
three call sites, one implementation (spec §3.3). Zero I/O; no LLM.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from jobhunter.hashing import sha256_hex
from jobhunter.l2.quotes import longest_matching_prefix, occurrence_index
from jobhunter.l2.report import Report
from jobhunter.l2.schemas import validate_record
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.markdown import block_intervals


def iter_quote_objects(extraction: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    facts = extraction.get("facts", {})
    for kind in ("experience_months", "deadline"):
        item = facts.get(kind)
        if item is not None:
            yield f"facts.{kind}.anchor", item["anchor"]
    for i, comp in enumerate(facts.get("compensation") or []):
        yield f"facts.compensation[{i}].anchor", comp["anchor"]
    for i, bp in enumerate(facts.get("boilerplate_spans") or []):
        yield f"facts.boilerplate_spans[{i}]", bp
    for area in extraction.get("demand_profile", {}).get("areas", []):
        aid = area.get("id", "?")
        for claim in area.get("claims", []):
            yield f"areas[{aid}].claims[{claim.get('id', '?')}].quote", claim["quote"]
        for i, ctx in enumerate(area.get("context") or []):
            yield f"areas[{aid}].context[{i}]", ctx


def _check_attribution(extraction: dict[str, Any], md: str, report: Report) -> None:
    n = len(md)
    for path, q in iter_quote_objects(extraction):
        s, e = q["span"]
        if not 0 <= s < e <= n:
            report.error("attribution", path, "span_bounds", span=[s, e], doc_len=n)
            continue
        if md[s:e] != q["text"]:
            report.error(
                "attribution",
                path,
                "text_mismatch",
                expected=q["text"],
                found=md[s:e],
                longest_prefix=longest_matching_prefix(md, q["text"]),
            )
            continue
        if occurrence_index(md, q["text"], s) != q["occurrence"]:
            report.error("attribution", path, "occurrence_mismatch", span=[s, e])


def _check_block_bounds(extraction: dict[str, Any], md: str, report: Report) -> None:
    blocks = block_intervals(md)
    for path, q in iter_quote_objects(extraction):
        if "\n" in q["text"]:
            report.error("block_bounds", path, "newline_in_quote")
            continue
        s, e = q["span"]
        if not any(bs <= s and e <= be for bs, be in blocks):
            report.error("block_bounds", path, "crosses_block_boundary", span=[s, e])


def verify(extraction: dict[str, Any], markdown: str) -> Report:
    report = Report(validator_version=VALIDATOR_VERSION)
    stored = extraction.get("document", {}).get("document_hash")
    if sha256_hex(markdown.encode("utf-8")) != stored:
        report.error("doc_binding", "document", "hash_mismatch", stored=stored)
        return report  # hard fail-fast: wrong document, nothing else is meaningful

    for message in validate_record(extraction, extraction["extraction"]["schema_version"]):
        report.error("schema", "<schema>", "invalid", message=message)
    if report.status == "fail":
        return report  # structure unknown; span checks would KeyError

    _check_attribution(extraction, markdown, report)
    _check_block_bounds(extraction, markdown, report)
    return report
```

`src/jobhunter/l2/__init__.py`:

```python
"""L2: demand-profile extraction. Increment 1 ships the verifier only."""

from jobhunter.l2.report import Finding, Report
from jobhunter.l2.transforms import VALIDATOR_VERSION
from jobhunter.l2.verify import verify

__all__ = ["VALIDATOR_VERSION", "Finding", "Report", "verify"]
```

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `uv run pytest tests/l2 -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/l2 tests/l2
git commit -m "feat(l2): verify() core — doc binding, schema, attribution, block bounds"
```

---

### Task 6: `verify()` — structure, evidence substrings, mentions

**Files:**
- Modify: `src/jobhunter/l2/verify.py`
- Test: `tests/l2/test_verify.py` (append)

**Interfaces:**
- Extends `verify()` with checks `structure`, `evidence_substrings`, `mentions_grounded`
  (spec §3.3 rows). Rules: claim ids unique per document; `structure` present iff the
  area has >1 claim; every leaf resolves to a claim id in the same area; each claim
  referenced exactly once; depth ≤ 5; `interview_evaluated[]` ids resolve to areas.
  `level_evidence`/`qualifiers[]`/`evidence_sources[]` must be substrings of the claim
  quote **or any of the area's context texts** (the ruled fix — spec §12).
  `mentions[]` must appear in some claim quote or context text of the area.

- [ ] **Step 1: Write the failing tests** (append to `tests/l2/test_verify.py`)

```python
def test_structure_missing_and_dangling() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["structure"] = None
    report = verify(rec, DOC_MD)
    assert "structure_missing" in codes(report, "structure")

    rec2 = minimal_record()
    rec2["demand_profile"]["areas"][0]["structure"] = {"op": "AND", "of": ["c1", "cX"]}
    report2 = verify(rec2, DOC_MD)
    assert "unknown_claim_id" in codes(report2, "structure")


def test_structure_each_claim_exactly_once() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["structure"] = {"op": "AND", "of": ["c1", "c1"]}
    report = verify(rec, DOC_MD)
    assert "claim_reference_count" in codes(report, "structure")


def test_structure_depth_cap() -> None:
    rec = minimal_record()
    node: dict[str, object] = {"op": "AND", "of": ["c1", "c2"]}
    for _ in range(6):
        node = {"op": "OR", "of": [node, "c1"]}  # deliberately broken refs; depth first
    rec["demand_profile"]["areas"][0]["structure"] = node
    report = verify(rec, DOC_MD)
    assert "depth_exceeded" in codes(report, "structure")


def test_interview_evaluated_resolves() -> None:
    rec = minimal_record()
    rec["demand_profile"]["interview_evaluated"] = ["a99"]
    report = verify(rec, DOC_MD)
    assert "unknown_area_id" in codes(report, "structure")


def test_evidence_substring_of_quote_or_context() -> None:
    rec = minimal_record()
    area = rec["demand_profile"]["areas"][0]
    area["claims"][1]["level_evidence"] = "preferred"  # in quote: ok
    area["claims"][0]["qualifiers"] = ["with guidance"]  # not in quote...
    area["context"] = [
        {"text": "0-2 YOE preferred", "span": [55, 72], "occurrence": 0}
    ]
    report = verify(rec, DOC_MD)
    assert "fragment_unanchored" in codes(report, "evidence_substrings")

    area["context"] = [
        {"text": "**Python** and distributed systems", "span": [18, 52], "occurrence": 0}
    ]
    area["claims"][0]["qualifiers"] = ["distributed"]  # substring of quote: ok
    report2 = verify(rec, DOC_MD)
    assert codes(report2, "evidence_substrings") == []


def test_mentions_grounded() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["mentions"] = ["Python", "Kubernetes"]
    report = verify(rec, DOC_MD)
    assert codes(report, "mentions_grounded") == ["mention_ungrounded"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/l2/test_verify.py -v`
Expected: new tests FAIL (checks not implemented; existing tests still pass).

- [ ] **Step 3: Implement** (add to `verify.py`; call the three new functions from
`verify()` after `_check_block_bounds`)

```python
_MAX_DEPTH = 5


def _walk_structure(
    node: object, depth: int, leaves: list[str], report: Report, path: str
) -> None:
    if isinstance(node, str):
        leaves.append(node)
        return
    assert isinstance(node, dict)  # schema-guaranteed past the schema check
    if depth > _MAX_DEPTH:
        report.error("structure", path, "depth_exceeded", max_depth=_MAX_DEPTH)
        return
    for child in node["of"]:
        _walk_structure(child, depth + 1, leaves, report, path)


def _check_structure(extraction: dict[str, Any], report: Report) -> None:
    profile = extraction["demand_profile"]
    area_ids = set()
    claim_ids: list[str] = []
    for area in profile["areas"]:
        aid = area["id"]
        area_ids.add(aid)
        path = f"areas[{aid}].structure"
        ids_here = [c["id"] for c in area["claims"]]
        claim_ids.extend(ids_here)
        structure = area["structure"]
        if len(area["claims"]) > 1 and structure is None:
            report.error("structure", path, "structure_missing")
            continue
        if len(area["claims"]) == 1 and structure is not None:
            report.error("structure", path, "structure_unexpected")
            continue
        if structure is None:
            continue
        leaves: list[str] = []
        _walk_structure(structure, 1, leaves, report, path)
        for leaf in leaves:
            if leaf not in ids_here:
                report.error("structure", path, "unknown_claim_id", claim_id=leaf)
        known = [leaf for leaf in leaves if leaf in ids_here]
        if sorted(known) != sorted(set(ids_here) & set(known)) or set(ids_here) - set(known):
            report.error(
                "structure", path, "claim_reference_count", expected=ids_here, got=leaves
            )
    dupes = {c for c in claim_ids if claim_ids.count(c) > 1}
    if dupes:
        report.error("structure", "<document>", "duplicate_claim_id", ids=sorted(dupes))
    for aid in profile["interview_evaluated"]:
        if aid not in area_ids:
            report.error("structure", "interview_evaluated", "unknown_area_id", area_id=aid)


def _check_evidence_fragments(extraction: dict[str, Any], report: Report) -> None:
    for area in extraction["demand_profile"]["areas"]:
        context_texts = [c["text"] for c in area["context"]]
        for claim in area["claims"]:
            hay = [claim["quote"]["text"], *context_texts]
            frags = [claim["level_evidence"], *claim["qualifiers"], *claim["evidence_sources"]]
            for frag in frags:
                if frag is not None and not any(frag in t for t in hay):
                    report.error(
                        "evidence_substrings",
                        f"areas[{area['id']}].claims[{claim['id']}]",
                        "fragment_unanchored",
                        fragment=frag,
                    )
        for mention in area["mentions"]:
            texts = [c["quote"]["text"] for c in area["claims"]] + context_texts
            if not any(mention in t for t in texts):
                report.error(
                    "mentions_grounded",
                    f"areas[{area['id']}]",
                    "mention_ungrounded",
                    mention=mention,
                )
```

(The claim-reference rule: every claim in a multi-claim area appears in `leaves`
exactly once — extra, missing, or repeated ids all land in `claim_reference_count`.)

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `uv run pytest tests/l2 -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/l2/verify.py tests/l2/test_verify.py
git commit -m "feat(l2): verify() structure, evidence fragments, mention grounding"
```

---

### Task 7: `verify()` — facts, overlap, quote shape, description, coverage

**Files:**
- Modify: `src/jobhunter/l2/verify.py`
- Test: `tests/l2/test_verify.py` (append)

**Interfaces:**
- Extends `verify()` with `facts_rederive` (re-run `TRANSFORMS[VALIDATOR_VERSION]` on
  anchor text; compare only the keys the transform returns; `None` →
  `fact_unanchored`), `overlap` (claim spans vs boilerplate → error
  `claim_in_boilerplate`; identical claim spans across areas → warning
  `duplicate_claim_span`), `quote_shape` (claim quotes: error <5 / >600, warn <15 /
  >280 codepoints; anchors ≥2), `template_description` (renderer, frozen v1:
  `f"{name}: " + " • ".join(claim quote texts in listed order)`; `synthesis
  "none"` → text must be null; `"llm"` → skipped), `coverage` (recompute `n_areas`,
  `n_claims`, `claim_char_coverage = |union(claim ∪ context spans)| / (len(md) −
  |union(boilerplate spans)|)`, denominator 0 → coverage 0.0; stored counters, if
  present under `extraction["coverage"]`... **no** — counters live nowhere in v1;
  the metrics are always recomputed into `Report.metrics`).
- Produces: `render_template_description(area: dict[str, Any]) -> str` (M2's template
  path reuses it).

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_facts_rederive_mismatch_and_unanchored() -> None:
    rec = minimal_record()
    rec["facts"]["experience_months"]["max"] = 36  # anchor says 0-2 YOE -> 24
    report = verify(rec, DOC_MD)
    assert "fact_mismatch" in codes(report, "facts_rederive")

    rec2 = minimal_record()
    anchor = {"text": "distributed systems", "span": [33, 52], "occurrence": 0}
    rec2["facts"]["experience_months"]["anchor"] = anchor
    report2 = verify(rec2, DOC_MD)
    assert "fact_unanchored" in codes(report2, "facts_rederive")


def test_overlap_claim_in_boilerplate() -> None:
    rec = minimal_record()
    rec["facts"]["boilerplate_spans"] = [
        {"text": "0-2 YOE preferred", "span": [55, 72], "occurrence": 0}
    ]
    report = verify(rec, DOC_MD)
    assert "claim_in_boilerplate" in codes(report, "overlap")


def test_quote_shape_bounds() -> None:
    rec = minimal_record()
    claim = rec["demand_profile"]["areas"][0]["claims"][1]
    claim["quote"] = {"text": "YOE", "span": [59, 62], "occurrence": 0}
    report = verify(rec, DOC_MD)
    assert "quote_too_short" in codes(report, "quote_shape")


def test_template_description() -> None:
    rec = minimal_record()
    area = rec["demand_profile"]["areas"][0]
    area["description"] = {"text": "wrong", "synthesis": "none", "run": None}
    report = verify(rec, DOC_MD)
    assert "description_text_unexpected" in codes(report, "template_description")

    area["description"] = {
        "text": "Backend engineering: **Python** and distributed systems • 0-2 YOE preferred",
        "synthesis": "template",
        "run": None,
    }
    report2 = verify(rec, DOC_MD)
    assert codes(report2, "template_description") == []


def test_coverage_metrics() -> None:
    report = verify(minimal_record(), DOC_MD)
    assert report.metrics["n_areas"] == 1
    assert report.metrics["n_claims"] == 2
    covered = (52 - 18) + (72 - 55)
    denominator = len(DOC_MD) - (111 - 84)
    assert report.metrics["claim_char_coverage"] == round(covered / denominator, 4)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/l2/test_verify.py -v`
Expected: new tests FAIL.

- [ ] **Step 3: Implement** (add to `verify.py`; call from `verify()` in this order
after the Task 6 checks; import `TRANSFORMS` from transforms)

```python
def _check_facts(extraction: dict[str, Any], report: Report) -> None:
    transforms = TRANSFORMS[VALIDATOR_VERSION]
    facts = extraction["facts"]
    items: list[tuple[str, str, dict[str, Any]]] = []
    if facts.get("experience_months"):
        items.append(("experience_months", "facts.experience_months", facts["experience_months"]))
    for i, comp in enumerate(facts.get("compensation") or []):
        items.append(("compensation", f"facts.compensation[{i}]", comp))
    if facts.get("deadline"):
        items.append(("deadline", "facts.deadline", facts["deadline"]))
    for kind, path, item in items:
        derived = transforms[kind](item["anchor"]["text"])
        if derived is None:
            report.error("facts_rederive", path, "fact_unanchored")
            continue
        stored = {k: item.get(k) for k in derived}
        if stored != derived:
            report.error("facts_rederive", path, "fact_mismatch", derived=derived, stored=stored)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _check_overlap(extraction: dict[str, Any], report: Report) -> None:
    boiler = [tuple(q["span"]) for q in extraction["facts"].get("boilerplate_spans") or []]
    seen: dict[tuple[int, int], str] = {}
    for area in extraction["demand_profile"]["areas"]:
        for claim in area["claims"]:
            span = tuple(claim["quote"]["span"])
            path = f"areas[{area['id']}].claims[{claim['id']}]"
            for b in boiler:
                if _overlaps(span, b):
                    report.error("overlap", path, "claim_in_boilerplate", boilerplate=list(b))
            if span in seen and seen[span] != area["id"]:
                report.warn("overlap", path, "duplicate_claim_span", also_in=seen[span])
            seen.setdefault(span, area["id"])


def _check_quote_shape(extraction: dict[str, Any], report: Report) -> None:
    for path, q in iter_quote_objects(extraction):
        length = len(q["text"])
        is_claim = ".claims[" in path
        is_anchor = path.endswith(".anchor")
        if is_claim:
            if length < 5:
                report.error("quote_shape", path, "quote_too_short", length=length)
            elif length < 15:
                report.warn("quote_shape", path, "quote_short", length=length)
            if length > 600:
                report.error("quote_shape", path, "quote_too_long", length=length)
            elif length > 280:
                report.warn("quote_shape", path, "quote_long", length=length)
        elif is_anchor and length < 2:
            report.error("quote_shape", path, "anchor_too_short", length=length)


def render_template_description(area: dict[str, Any]) -> str:
    quotes = " • ".join(c["quote"]["text"] for c in area["claims"])
    return f"{area['name']}: {quotes}"


def _check_descriptions(extraction: dict[str, Any], report: Report) -> None:
    for area in extraction["demand_profile"]["areas"]:
        desc = area["description"]
        if desc is None:
            continue
        path = f"areas[{area['id']}].description"
        if desc["synthesis"] == "none" and desc["text"] is not None:
            report.error("template_description", path, "description_text_unexpected")
        elif desc["synthesis"] == "template" and desc["text"] != render_template_description(area):
            report.error(
                "template_description",
                path,
                "template_mismatch",
                expected=render_template_description(area),
            )
        # synthesis == "llm": judged, not machine-checked (spec §3.4)


def _union_size(spans: list[tuple[int, int]]) -> int:
    total, prev_end = 0, -1
    for s, e in sorted(spans):
        s = max(s, prev_end)
        if e > s:
            total += e - s
            prev_end = e
        prev_end = max(prev_end, e)
    return total


def _compute_coverage(extraction: dict[str, Any], md: str, report: Report) -> None:
    areas = extraction["demand_profile"]["areas"]
    claim_spans = [
        (c["quote"]["span"][0], c["quote"]["span"][1]) for a in areas for c in a["claims"]
    ]
    ctx_spans = [(q["span"][0], q["span"][1]) for a in areas for q in a["context"]]
    boiler = [
        (q["span"][0], q["span"][1])
        for q in extraction["facts"].get("boilerplate_spans") or []
    ]
    denominator = len(md) - _union_size(boiler)
    coverage = _union_size(claim_spans + ctx_spans) / denominator if denominator else 0.0
    report.metrics.update(
        {
            "n_areas": len(areas),
            "n_claims": len(claim_spans),
            "claim_char_coverage": round(coverage, 4),
        }
    )
```

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `uv run pytest tests/l2 -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/l2/verify.py tests/l2/test_verify.py
git commit -m "feat(l2): verify() facts, overlap, quote shape, descriptions, coverage"
```

---

### Task 8: Golden end-to-end fixtures (incl. CJK)

**Files:**
- Create: `tests/l2/fixtures/anthropic.extraction.json` (against the existing golden
  `tests/fixtures/md/greenhouse_anthropic.md`)
- Create: `tests/l2/fixtures/cjk.md`, `tests/l2/fixtures/cjk.extraction.json`
- Test: `tests/l2/test_verify_golden.py`

**Interfaces:**
- Consumes: everything above. The CJK fixture is **required before `validator/1`
  freezes** (spec §11).

- [ ] **Step 1: Author the CJK document** (`tests/l2/fixtures/cjk.md` — synthetic
Japanese posting; NFKC applies at L0 so full-width digits are already folded)

```markdown
## 応募資格

- Pythonでの開発経験 3-5 years
- 分散システムの運用経験
- 日本語ビジネスレベル

## 歓迎

- Kubernetesの経験
```

- [ ] **Step 2: Author the extraction records**

For `cjk.extraction.json`: compute spans with a throwaway
`python -c` snippet using `resolve_quote` against the file content, then write the
record by hand following the `minimal_record()` shape in `tests/l2/conftest.py`:
document_hash = `sha256_hex(md.encode())`; one `technical` area (`a1`) with claims
`c1` = quote `Pythonでの開発経験 3-5 years` (importance `required`),
`c2` = quote `分散システムの運用経験` (required), `c3` = quote `Kubernetesの経験`
(preferred); `structure` `{"op": "AND", "of": ["c1", "c2", "c3"]}`; mentions
`["Python", "Kubernetes"]`; `facts.experience_months` anchored on `3-5 years`
(min 36, max 60); no compensation/deadline; empty boilerplate.

For `anthropic.extraction.json`: same procedure against
`tests/fixtures/md/greenhouse_anthropic.md` — pick two verbatim requirement bullets
as claims for one area, anchor one L1-parseable fact if the fixture contains one
(else `facts` holds only `boilerplate_spans: []`). Keep it small: this golden proves
the verifier passes on real converter output, not that the extraction is complete.

- [ ] **Step 3: Write the test** (`tests/l2/test_verify_golden.py`)

```python
import json
from pathlib import Path

from jobhunter.l2 import verify

HERE = Path(__file__).parent / "fixtures"
GOLDEN_MD = Path(__file__).resolve().parents[1] / "fixtures" / "md"


def _case(md_path: Path, extraction_path: Path) -> None:
    md = md_path.read_text(encoding="utf-8")
    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
    report = verify(extraction, md)
    assert report.status == "pass", [f"{f.check}:{f.code}@{f.path}" for f in report.findings]


def test_anthropic_golden() -> None:
    _case(GOLDEN_MD / "greenhouse_anthropic.md", HERE / "anthropic.extraction.json")


def test_cjk_golden() -> None:
    _case(HERE / "cjk.md", HERE / "cjk.extraction.json")


def test_cjk_spans_are_codepoints() -> None:
    md = (HERE / "cjk.md").read_text(encoding="utf-8")
    extraction = json.loads((HERE / "cjk.extraction.json").read_text(encoding="utf-8"))
    quote = extraction["demand_profile"]["areas"][0]["claims"][1]["quote"]
    s, e = quote["span"]
    assert md[s:e] == quote["text"] == "分散システムの運用経験"
    assert e - s == 11  # codepoints, not bytes (33 in UTF-8)
```

- [ ] **Step 4: Run; fix span arithmetic until green**

Run: `uv run pytest tests/l2/test_verify_golden.py -v`
Expected: PASS (iterate on hand-computed spans using the failure diagnostics — the
`text_mismatch` finding prints expected vs found, which is the verifier dogfooding
itself).

- [ ] **Step 5: Commit**

```bash
git add tests/l2/fixtures tests/l2/test_verify_golden.py
git commit -m "test(l2): golden end-to-end verification incl. CJK fixture"
```

---

### Task 9: CLI — `job-hunter verify`

**Files:**
- Modify: `src/jobhunter/cli.py`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Produces: `job-hunter verify EXTRACTION_FILE DOCUMENT_FILE [--json]`. Exit 0 all
  pass, **1 ran fine but findings failed** (ruled extension of the 0/2 convention),
  2 systemic (unreadable file, invalid JSON, unknown schema version). Human output
  one line per finding with derived line:col via `jobhunter.l2.quotes.line_col`.
  Archive-/store-addressed forms (`verify <document_hash>`, `--from-archive`,
  `--all/--since`) are increment 2, when extractions exist to look up.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli.py`, following
the file's existing `CliRunner` pattern)

```python
def test_verify_pass_and_fail(tmp_path: Path) -> None:
    from tests.l2.conftest import DOC_MD, minimal_record

    doc = tmp_path / "doc.md"
    doc.write_text(DOC_MD, encoding="utf-8")
    good = tmp_path / "good.json"
    good.write_text(json.dumps(minimal_record()), encoding="utf-8")
    result = runner.invoke(app, ["verify", str(good), str(doc), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "pass"

    bad_record = minimal_record()
    bad_record["demand_profile"]["areas"][0]["claims"][0]["quote"]["text"] = "fabricated"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_record), encoding="utf-8")
    result = runner.invoke(app, ["verify", str(bad), str(doc)])
    assert result.exit_code == 1
    assert "text_mismatch" in result.stdout


def test_verify_systemic(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["verify", str(tmp_path / "missing.json"), str(doc)])
    assert result.exit_code == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -k verify -v`
Expected: FAIL — no such command.

- [ ] **Step 3: Implement** (add to `cli.py`; follow the module's existing
error-handling style)

```python
@app.command()
def verify(
    extraction_file: str,
    document_file: str,
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Re-run every validator/1 check over an extraction against its document.

    Exit 0: all checks pass. Exit 1: ran fine, findings failed. Exit 2: systemic.
    """
    from jobhunter.l2 import verify as l2_verify
    from jobhunter.l2.quotes import line_col

    try:
        extraction = json.loads(Path(extraction_file).read_text(encoding="utf-8"))
        markdown = Path(document_file).read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_SYSTEMIC) from exc
    try:
        report = l2_verify(extraction, markdown)
    except KeyError as exc:  # unknown schema version, malformed top level
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_SYSTEMIC) from exc

    if json_out:
        typer.echo(json.dumps(report.to_json(), ensure_ascii=False))
    else:
        for f in report.findings:
            loc = ""
            span = f.detail.get("span")
            if isinstance(span, list):
                line, col = line_col(markdown, int(span[0]))
                loc = f"  line {line}:{col}"
            typer.echo(f"{f.severity.upper()} {f.check}:{f.code} {f.path}{loc}")
        typer.echo(f"{report.status}  ({len(report.findings)} findings)")
    if report.status == "fail":
        raise typer.Exit(1)
```

(`Path` is already imported in `cli.py` via its existing imports — if not, add
`from pathlib import Path` to the import block.)

- [ ] **Step 4: Run tests, lint, typecheck**

Run: `uv run pytest tests/test_cli.py tests/l2 -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/jobhunter/cli.py tests/test_cli.py
git commit -m "feat(cli): file-based verify command (exit 1 = findings failed)"
```

---

### Task 10: Docs, full gate, freeze

**Files:**
- Modify: `src/jobhunter/CLAUDE.md` (add `l2/` to Layout; drop "L2 not built" line)
- Modify: `CLAUDE.md` (root — add `verify` to the CLI command list)
- Modify: `docs/README.md` (Code section: L2 increment 1 shipped, link this plan)

**Interfaces:** none — documentation and the freeze declaration.

- [ ] **Step 1: Update the three docs**

In `src/jobhunter/CLAUDE.md` Layout, after the `store/` sub-package bullet, add:

```markdown
- [`l2/`] — demand-profile verification (increment 1): quote/span resolution,
  versioned fact transforms, JSON schemas v1, the pure `verify()` check suite.
  `VALIDATOR_VERSION = "1"` is frozen — any check or threshold change bumps it
  (see `docs/2026-08-26-l2-extraction-harness.md` §3).
```

and change the conventions line to "Not built yet: L2 extraction runner (M2+),
concept linker." Root `CLAUDE.md`: add `verify` to the CLI list. `docs/README.md`
Code section: add a line for `superpowers/plans/2026-08-26-l2-increment-1-verifier.md`.

- [ ] **Step 2: Run the full gate**

Run: `uv run pytest && uv run ruff check . && uv run mypy`
Expected: entire suite passes (store/integration tests may be skipped without
Postgres — unit suite must be green).

- [ ] **Step 3: Commit**

```bash
git add src/jobhunter/CLAUDE.md CLAUDE.md docs/README.md
git commit -m "docs: L2 increment 1 shipped; validator/1 frozen"
```

- [ ] **Step 4: Merge decision** — use superpowers:finishing-a-development-branch
(present the branch for review/merge; never force-push).

---

## Self-Review (performed at write time)

- **Spec coverage (§3):** quote objects/offsets → T2; resolution + no-repair → T2;
  verifier checks table → T5–T7 (all rows except `chain`, which requires archive
  access and is scoped to M2 with the store-addressed CLI); machine/judged facets →
  written by the M2 harness, not the pure verifier (the verifier *returns* the
  report; nothing in M1 persists it); gold migration tooling → M2+ (no gold rows
  exist yet); CJK fixture before freeze → T8; CLI + exit 1 → T9; freeze → T10.
- **Placeholder scan:** clean — every step carries code or exact content; T8's
  extraction JSONs are specified by construction procedure + shape reference to
  `minimal_record()` because their spans must be computed against fixture bytes.
- **Type consistency:** `Quote.span: tuple[int, int]` vs JSON `[int, int]` — JSON
  arrays enter `verify()` as lists; `verify()` reads `q["span"]` positionally and
  never constructs `Quote` from records (only `resolve_quote` builds `Quote`s, in
  the M2 harness emit path). `VALIDATOR_VERSION` lives in `transforms.py` and is
  re-exported by `__init__.py`; `verify.py` imports it from `transforms`.
