"""The verifier: one pure function over (extraction JSON, canonical markdown).

Inline validator in the harness retry loop, standalone audit, and memo linter —
three call sites, one implementation (harness spec §3.3). Zero I/O; no LLM.
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
                span=[s, e],
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
