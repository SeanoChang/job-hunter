"""The verifier: one pure function over (extraction JSON, canonical markdown).

Inline validator in the harness retry loop, standalone audit, and memo linter —
three call sites, one implementation (harness spec §3.3). Zero I/O; no LLM.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any

from jobhunter.hashing import sha256_hex
from jobhunter.l2.quotes import longest_matching_prefix, occurrence_index
from jobhunter.l2.report import Report
from jobhunter.l2.schemas import validate_record
from jobhunter.l2.transforms import TRANSFORMS, VALIDATOR_VERSION
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
    area_ids: set[str] = set()
    all_claim_ids: list[str] = []
    for area in profile["areas"]:
        aid = area["id"]
        area_ids.add(aid)
        path = f"areas[{aid}].structure"
        ids_here = [c["id"] for c in area["claims"]]
        all_claim_ids.extend(ids_here)
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
        known = Counter(leaf for leaf in leaves if leaf in ids_here)
        if known != Counter(ids_here):
            report.error("structure", path, "claim_reference_count", expected=ids_here, got=leaves)
    dupes = {c for c in all_claim_ids if all_claim_ids.count(c) > 1}
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
    boiler = [
        (int(q["span"][0]), int(q["span"][1]))
        for q in extraction["facts"].get("boilerplate_spans") or []
    ]
    seen: dict[tuple[int, int], str] = {}
    for area in extraction["demand_profile"]["areas"]:
        for claim in area["claims"]:
            span = (int(claim["quote"]["span"][0]), int(claim["quote"]["span"][1]))
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
        prev_end = max(prev_end, e)
    return total


def _compute_coverage(extraction: dict[str, Any], md: str, report: Report) -> None:
    areas = extraction["demand_profile"]["areas"]
    claim_spans = [
        (int(c["quote"]["span"][0]), int(c["quote"]["span"][1])) for a in areas for c in a["claims"]
    ]
    ctx_spans = [(int(q["span"][0]), int(q["span"][1])) for a in areas for q in a["context"]]
    boiler = [
        (int(q["span"][0]), int(q["span"][1]))
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
    _check_structure(extraction, report)
    _check_evidence_fragments(extraction, report)
    _check_facts(extraction, report)
    _check_overlap(extraction, report)
    _check_quote_shape(extraction, report)
    _check_descriptions(extraction, report)
    _compute_coverage(extraction, markdown, report)
    return report
