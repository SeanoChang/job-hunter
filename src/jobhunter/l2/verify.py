"""The verifier: one pure function over (extraction JSON, canonical markdown).

Inline validator in the harness retry loop, standalone audit, and memo linter —
three call sites, one implementation (harness spec §3.3). Zero I/O; no LLM.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any

from jobhunter.hashing import sha256_hex
from jobhunter.l2.quotes import describe_not_found, longest_matching_prefix, occurrence_index
from jobhunter.l2.report import Report
from jobhunter.l2.schemas import validate_record
from jobhunter.l2.transforms import TRANSFORMS, VALIDATOR_VERSION
from jobhunter.markdown import block_intervals


def iter_quote_objects(extraction: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield (path, kind, quote) for every quote object; kind is structured, never
    re-parsed from the display path (model-supplied ids may contain anything)."""
    facts = extraction.get("facts", {})
    for fact_kind in ("experience_months", "deadline"):
        item = facts.get(fact_kind)
        if item is not None:
            yield f"facts.{fact_kind}.anchor", "anchor", item["anchor"]
    for i, comp in enumerate(facts.get("compensation") or []):
        yield f"facts.compensation[{i}].anchor", "anchor", comp["anchor"]
    for i, bp in enumerate(facts.get("boilerplate_spans") or []):
        yield f"facts.boilerplate_spans[{i}]", "boilerplate", bp
    for area in extraction.get("demand_profile", {}).get("areas", []):
        aid = area.get("id", "?")
        for claim in area.get("claims", []):
            yield f"areas[{aid}].claims[{claim.get('id', '?')}].quote", "claim", claim["quote"]
        for i, ctx in enumerate(area.get("context") or []):
            yield f"areas[{aid}].context[{i}]", "context", ctx


def _check_attribution(extraction: dict[str, Any], md: str, report: Report) -> None:
    n = len(md)
    for path, _kind, q in iter_quote_objects(extraction):
        s, e = q["span"]
        if type(s) is not int or type(e) is not int:
            # draft 2020-12 "integer" accepts 5.0; slicing with it would crash
            report.error("attribution", path, "span_type", span=[s, e])
            continue
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
                divergence=describe_not_found(md, q["text"]),
            )
            continue
        if occurrence_index(md, q["text"], s) != q["occurrence"]:
            report.error("attribution", path, "occurrence_mismatch", span=[s, e])


def _check_block_bounds(extraction: dict[str, Any], md: str, report: Report) -> None:
    blocks = block_intervals(md)
    for path, _kind, q in iter_quote_objects(extraction):
        if "\n" in q["text"]:
            report.error("block_bounds", path, "newline_in_quote")
            continue
        s, e = q["span"]
        if type(s) is not int or type(e) is not int:
            continue  # attribution already reported span_type
        if not any(bs <= s and e <= be for bs, be in blocks):
            report.error("block_bounds", path, "crosses_block_boundary", span=[s, e])


_MAX_DEPTH = 5


def _structure_depth_ok(extraction: dict[str, Any], report: Report) -> bool:
    """Iterative depth preflight, run BEFORE schema validation: the schema's
    recursive $ref would hit RecursionError on a few hundred nested operators,
    crashing before any check could report the configured cap. Defensive over
    raw JSON — nothing here assumes the record validated."""
    profile = extraction.get("demand_profile")
    areas = profile.get("areas") if isinstance(profile, dict) else None
    if not isinstance(areas, list):
        return True
    ok = True
    for area in areas:
        if not isinstance(area, dict):
            continue
        aid = area.get("id")
        label = aid if isinstance(aid, str) else "?"
        stack: list[tuple[object, int]] = [(area.get("structure"), 1)]
        while stack:
            node, depth = stack.pop()
            if not isinstance(node, dict):
                continue
            if depth > _MAX_DEPTH:
                report.error(
                    "structure", f"areas[{label}].structure", "depth_exceeded",
                    max_depth=_MAX_DEPTH,
                )
                ok = False
                break
            children = node.get("of")
            if isinstance(children, list):
                stack.extend((child, depth + 1) for child in children)
    return ok


def _walk_structure(
    node: object, depth: int, leaves: list[str], report: Report, path: str
) -> bool:
    """Collect leaf claim ids; True when truncated at the depth cap (leaf checks
    would then report phantom defects over the missing leaves)."""
    if isinstance(node, str):
        leaves.append(node)
        return False
    assert isinstance(node, dict)  # schema-guaranteed past the schema check
    if depth > _MAX_DEPTH:
        report.error("structure", path, "depth_exceeded", max_depth=_MAX_DEPTH)
        return True
    truncated = False
    for child in node["of"]:
        truncated = _walk_structure(child, depth + 1, leaves, report, path) or truncated
    return truncated


def _check_structure(extraction: dict[str, Any], report: Report) -> None:
    profile = extraction["demand_profile"]
    area_ids: set[str] = set()
    all_claim_ids: list[str] = []
    area_id_list = [a["id"] for a in profile["areas"]]
    dup_areas = sorted({a for a in area_id_list if area_id_list.count(a) > 1})
    if dup_areas:
        report.error("structure", "<document>", "duplicate_area_id", ids=dup_areas)
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
        truncated = _walk_structure(structure, 1, leaves, report, path)
        if truncated:
            continue  # leaf set incomplete: reference checks would report phantoms
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
            if claim["level"] is not None and claim["level_evidence"] is None:
                # ruled in the parsing direction (review disposition 7): a non-null
                # level carries an evidence phrase; null means unstated
                report.error(
                    "evidence_substrings",
                    f"areas[{area['id']}].claims[{claim['id']}]",
                    "level_evidence_missing",
                    level=claim["level"],
                )
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
    for path, kind, q in iter_quote_objects(extraction):
        length = len(q["text"])
        if kind == "claim":
            if length < 5:
                report.error("quote_shape", path, "quote_too_short", length=length)
            elif length < 15:
                report.warn("quote_shape", path, "quote_short", length=length)
            if length > 600:
                report.error("quote_shape", path, "quote_too_long", length=length)
            elif length > 280:
                report.warn("quote_shape", path, "quote_long", length=length)
        elif kind == "anchor" and length < 2:
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


def _merge_intervals(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _clamped_spans(quotes: list[dict[str, Any]], n: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for q in quotes:
        s, e = int(q["span"][0]), int(q["span"][1])
        s, e = max(0, min(s, n)), max(0, min(e, n))
        if e > s:
            out.append((s, e))
    return out


def _compute_coverage(extraction: dict[str, Any], md: str, report: Report) -> None:
    """claim_char_coverage stays in [0, 1]: spans are clamped to the document,
    boilerplate is excluded from the numerator, and an empty denominator is 0.0."""
    n = len(md)
    areas = extraction["demand_profile"]["areas"]
    claim_quotes = [c["quote"] for a in areas for c in a["claims"]]
    ctx_quotes = [q for a in areas for q in a["context"]]
    boiler = _merge_intervals(
        _clamped_spans(extraction["facts"].get("boilerplate_spans") or [], n)
    )
    covered = 0
    for s, e in _merge_intervals(_clamped_spans(claim_quotes + ctx_quotes, n)):
        segment = e - s
        for bs, be in boiler:
            lo, hi = max(s, bs), min(e, be)
            if hi > lo:
                segment -= hi - lo
        covered += segment
    denominator = n - sum(e - s for s, e in boiler)
    coverage = covered / denominator if denominator > 0 else 0.0
    report.metrics.update(
        {
            "n_areas": len(areas),
            "n_claims": len(claim_quotes),
            "claim_char_coverage": round(coverage, 4),
        }
    )


def verify(extraction: dict[str, Any], markdown: str) -> Report:
    report = Report(validator_version=VALIDATOR_VERSION)
    stored = extraction.get("document", {}).get("document_hash")
    if sha256_hex(markdown.encode("utf-8")) != stored:
        report.error("doc_binding", "document", "hash_mismatch", stored=stored)
        return report  # hard fail-fast: wrong document, nothing else is meaningful

    if not _structure_depth_ok(extraction, report):
        return report  # schema validation would recurse past the interpreter limit

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
