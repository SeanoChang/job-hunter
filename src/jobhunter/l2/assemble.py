"""Emit JSON → unified record: resolve every quote to a span, derive every fact
from its anchor, fill the record-side defaults. All resolution failures are
collected before raising so one reprompt carries the complete error list."""

from __future__ import annotations

from typing import Any

from jobhunter.l2.prompt import PROMPT_VERSION
from jobhunter.l2.quotes import (
    AmbiguousQuote,
    QuoteNotFound,
    describe_not_found,
    resolve_quote,
)
from jobhunter.l2.transforms import TRANSFORMS, VALIDATOR_VERSION


class AssembleError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"{len(errors)} resolution error(s)")
        self.errors = errors


class _Resolver:
    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.errors: list[str] = []

    def quote(self, emit_quote: dict[str, Any]) -> dict[str, Any] | None:
        text = emit_quote.get("text", "")
        occurrence = emit_quote.get("occurrence")
        try:
            q = resolve_quote(self.markdown, text, occurrence)
        except QuoteNotFound:
            self.errors.append(describe_not_found(self.markdown, text))
            return None
        except AmbiguousQuote as exc:
            self.errors.append(
                f"ambiguous quote, {len(exc.starts)} occurrences — "
                f'set "occurrence": {text[:80]!r}'
            )
            return None
        return {"text": q.text, "span": list(q.span), "occurrence": q.occurrence}


def _fact(resolver: _Resolver, kind: str, item: dict[str, Any] | None,
          passthrough: tuple[str, ...]) -> dict[str, Any] | None:
    if item is None:
        return None
    anchor = resolver.quote(item.get("anchor") or {})
    if anchor is None:
        return None
    derived = TRANSFORMS[VALIDATOR_VERSION][kind](anchor["text"])
    if derived is None:
        resolver.errors.append(
            f"fact anchor not parseable as {kind}: {anchor['text'][:80]!r} — "
            f"if the posting states no {kind}, set {kind} to null instead of "
            "anchoring on the sentence that says so; otherwise anchor on the "
            "exact phrase carrying the value"
        )
        return None
    out: dict[str, Any] = dict(derived)
    for key in passthrough:
        out[key] = item.get(key)
    out["anchor"] = anchor
    return out


def _claim(resolver: _Resolver, emit_claim: dict[str, Any]) -> dict[str, Any] | None:
    quote = resolver.quote(emit_claim.get("quote") or {})
    if quote is None:
        return None
    return {
        "id": emit_claim.get("id"),
        "quote": quote,
        "importance": emit_claim.get("importance"),
        "level": emit_claim.get("level"),
        "level_evidence": emit_claim.get("level_evidence"),
        "negated": emit_claim.get("negated", False),
        "threshold": emit_claim.get("threshold"),
        "qualifiers": emit_claim.get("qualifiers") or [],
        "evidence_sources": emit_claim.get("evidence_sources") or [],
    }


def assemble(
    emit: dict[str, Any],
    markdown: str,
    *,
    document_hash: str,
    normalizer_version: str,
    observed_model: str,
    at: str,
    prompt_version: str = PROMPT_VERSION,
    schema_version: str = "1",
) -> dict[str, Any]:
    resolver = _Resolver(markdown)
    emit_facts = emit.get("facts") or {}

    facts: dict[str, Any] = {
        "experience_months": _fact(
            resolver, "experience_months", emit_facts.get("experience_months"), ("scope",)
        ),
        "compensation": [
            comp
            for item in emit_facts.get("compensation") or []
            if (comp := _fact(resolver, "compensation", item, ("condition",))) is not None
        ],
        "deadline": _fact(resolver, "deadline", emit_facts.get("deadline"), ()),
        "boilerplate_spans": [
            q for bp in emit_facts.get("boilerplate_spans") or []
            if (q := resolver.quote(bp)) is not None
        ],
    }

    areas: list[dict[str, Any]] = []
    profile = emit.get("demand_profile") or {}
    for emit_area in profile.get("areas") or []:
        claims = [c for ec in emit_area.get("claims") or [] if (c := _claim(resolver, ec))]
        context = [q for ec in emit_area.get("context") or [] if (q := resolver.quote(ec))]
        structure = emit_area.get("structure") if len(claims) > 1 else None
        areas.append(
            {
                "id": emit_area.get("id"),
                "name": emit_area.get("name"),
                "kind": emit_area.get("kind"),
                "importance": emit_area.get("importance"),
                "level": emit_area.get("level"),
                "claims": claims,
                "context": context,
                "structure": structure,
                "mentions": emit_area.get("mentions") or [],
                "description": {"text": None, "synthesis": "none", "run": None},
            }
        )

    if resolver.errors:
        raise AssembleError(resolver.errors)

    return {
        "document": {"document_hash": document_hash, "normalizer_version": normalizer_version},
        "facts": facts,
        "demand_profile": {
            "areas": areas,
            "interview_evaluated": profile.get("interview_evaluated") or [],
        },
        "extraction": {
            "model": observed_model,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "validator_version": VALIDATOR_VERSION,
            "at": at,
        },
    }
