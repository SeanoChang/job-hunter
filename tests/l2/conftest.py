"""Shared builders: a small canonical document + a valid extraction record.

Spans are computed with resolve_quote rather than hand-typed offsets, so the
fixture cannot drift from the document text it quotes.
"""

from __future__ import annotations

import copy
from typing import Any

from jobhunter.hashing import sha256_hex
from jobhunter.l2.quotes import resolve_quote

DOC_MD = (
    "## Requirements\n\n"
    "- **Python** and distributed systems\n"
    "- 0-2 YOE preferred\n\n"
    "## About\n\n"
    "Equal opportunity employer."
)


def make_quote(text: str, occurrence: int | None = None) -> dict[str, Any]:
    quote = resolve_quote(DOC_MD, text, occurrence)
    return {"text": quote.text, "span": list(quote.span), "occurrence": quote.occurrence}


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
            "anchor": make_quote("0-2 YOE"),
        },
        "compensation": [],
        "deadline": None,
        "boilerplate_spans": [make_quote("Equal opportunity employer.")],
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
                        "quote": make_quote("**Python** and distributed systems"),
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
                        "quote": make_quote("0-2 YOE preferred"),
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
