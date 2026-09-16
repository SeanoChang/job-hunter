"""What a v2 record looks like once it leaves the archive: the slice Postgres
stores, the claim index the agreement gate compares, the rows the mention
aggregate asserts, and the digest every current renderer already knows how to
print.

Four adapters, one direction. Storage is not migrated by this module — the
`extractions.profile` JSONB blob and the three `profile_mentions` columns are
the same contract v1 writes into, so v2 has to say what it means inside them.
Where the legacy shape cannot carry a v2 distinction the projection loses it
rather than inventing a substitute, and the archived record keeps the whole
truth: the spec's richer per-claim table (statement, polarity, conditions,
relation ids) is an additive migration that needs its own approval.

Two rules run through all four:

- Importance belongs to a STATEMENT. The production defect this contract exists
  to kill (audit C04) was `profile_mentions` carrying the importance of the
  presentation area a mention happened to sit in, so a preferred certification
  next to a required degree read back as required. Nothing here reads `areas`.
- Nothing is derived here. Values were derived once, by `facts.py`, under a
  frozen validator version; a summary that re-parsed text would be a second,
  unversioned grammar.

The one exception to "nothing is derived here" is quality, and it is not an
exception at all: `quality_of` re-runs the frozen `quality.assess` policy over
the dimensions `runner.settle` attaches to the chosen candidate under
`SETTLEMENT`. Four of the seven dimensions only exist after a document's whole
event stream is folded, so the record reaching the projections carries them and
both projections read the same answer.

Pure like every other `l2/v2` module: no I/O, no environment, no store imports.
"""

from __future__ import annotations

from typing import Any

from jobhunter.l2.v2.project import mention_rows as _project_rows
from jobhunter.l2.v2.quality import assess
from jobhunter.l2.v2.types import PROFICIENCY

SCHEMA_VERSION = "2"

#: The reserved record key `runner.settle` attaches its verdict under, and the
#: only thing in this module that knows settlement happened (spec §6).
#: Assembly cannot fill the audit, sampling and review dimensions — they are
#: outputs of the fold over a document's whole event stream, which happens long
#: after a candidate is sealed — so the fold hands them to the bundle's
#: projections as data. A record without the key projects the quality it was
#: assembled with, which is what the archived candidate, the agreement gate and
#: a stored blob re-projected through `profile_of` all carry.
SETTLEMENT = "settlement"

#: `pulse.MAX_MENTIONS`, restated rather than imported: `pulse` reaches into the
#: store and importing it here would end this module's purity. The two are pinned
#: together by a test.
MAX_MENTIONS = 8

#: What a statement with no importance projects as in the legacy columns.
#: Responsibilities, compensation statements and employer context carry a null
#: importance by contract (spec §3), and the column is NOT NULL; `contextual` is
#: v1's existing word for "named by the posting, not demanded by it".
NO_IMPORTANCE = "contextual"


def quality_of(record: dict[str, Any]) -> dict[str, Any]:
    """The record's quality object as of settlement (spec §6).

    Four of the seven dimensions are not knowable at assembly: `semantics` and
    `completeness` are the `semantic-audit/v1` phase's answer, `sampling` is the
    cohort's, `human_review` is the review stream's, and `search_eligible` is
    derived from all of them together with the settled lifecycle. `assemble`
    therefore seals a candidate with the two it does know — the source
    assessment and the evidence verdict — and leaves the rest `not_checked`,
    which no offline phase can clear.

    `runner.settle` supplies the rest under `SETTLEMENT` once its fold has them,
    and this re-derives the object through the same frozen policy that wrote the
    first one. Nothing here decides anything: `blocking` is the audit's own
    count (blocking findings plus blocking unresolved questions, which gate
    identically), and every value is passed through to `quality.assess`.

    Defensive about the record it is given, like every other reader of a stored
    blob: an unrecognised value reaches `assess` unchanged and fails its
    whitelist there, never here.
    """
    quality = record.get("quality")
    quality = quality if isinstance(quality, dict) else {}
    settlement = record.get(SETTLEMENT)
    if not isinstance(settlement, dict):
        return quality
    blocking = settlement.get("blocking")
    return assess(
        source=str(quality.get("source")),
        evidence=str(quality.get("evidence")),
        semantics=str(settlement.get("semantics", "not_checked")),
        completeness=str(settlement.get("completeness", "not_checked")),
        sampling=str(settlement.get("sampling", "not_requested")),
        human_review=str(settlement.get("human_review", "none")),
        blocking_findings=blocking if isinstance(blocking, int) and blocking is not True else 0,
        lifecycle=str(settlement.get("lifecycle") or "pending"),
    )


def profile_of(record: dict[str, Any]) -> dict[str, Any]:
    """The stored profile blob for a v2 record: the served slice plus a shape marker.

    Deliberately not the whole record. `pulse` loads the blob of every profiled
    event on every call, so block accounting, presentation areas and the
    extraction envelope stay where they are already durable — in the archived
    attempt — and what remains is exactly what the read surface serves. The
    `schema` marker is what the two shape-aware readers dispatch on; without it
    a v2 blob is indistinguishable from a v1 one that lost its areas.

    Idempotent over its own output, so a caller holding only the stored blob
    re-derives the same slice a caller holding the record does.

    `demand_profile` is the claim index below, and it is here because this same
    function is what `runner.settle` feeds to the cross-sample agreement gate.

    The stored `quality` is the SETTLED one (`quality_of`): the blob is what the
    read surface answers from, and a blob claiming `not_checked` for a record an
    audit cleared would make every reader recompute the policy for itself. The
    reserved settlement key is consumed here and never stored — the blob's keys
    are these six.
    """
    return {
        "schema": SCHEMA_VERSION,
        "statements": record["statements"],
        "relations": record["relations"],
        "facts": record["facts"],
        "mentions": record["mentions"],
        "quality": quality_of(record),
        "demand_profile": claim_index(record),
    }


def claim_index(record: dict[str, Any]) -> dict[str, Any]:
    """The claim index the cross-sample agreement gate compares.

    `runner.settle` hands `bundle.profile_of(record)` to
    `agreement.cohort_hook`, and `agreement._claims` reads exactly one path out
    of it: `demand_profile.areas[].claims[]`, taking a span, an importance and a
    negation flag off each claim. A slice without that path is not compared
    leniently, it is not compared at all — empty claim sets divide by nothing
    and score `f1 = 1.0` — so the audit slot (5% of documents) and every
    reprompted document, the case v2 exists to absorb, would certify themselves
    however far apart their samples were. This is what makes the gate real for
    v2; `profile_of` therefore carries it.

    One claim per statement, per (mention, statement) link, and per fact entry:
    every assertion the record makes about the document, anchored at the span it
    cites and carrying its statement's importance and polarity. A claim's span
    is the envelope of the refs it cites, because the gate aligns claims by span
    overlap and a claim has one textual footprint.

    Validator/19 adds spec §6's remaining comparator dimensions to each claim —
    statement `kind`, the `polarity_target` a negation actually points at, the
    `values` its fact entries derived, the `alternatives` it participates in,
    and its `entity_links`. Until they were here, two samples that cited the
    same spans and disagreed about everything else scored F1 1.0 and certified
    each other: `all_of` against `any_of` over one pair of statements is a
    different job, and the gate could not see it (external review finding 6).
    Carrying them is this function's half; comparing them is `agreement`'s, and
    none of them touch F1 or a threshold.

    Two of the five are deliberately namespace-free — derived values come from
    `facts.py` under a frozen validator, and entity links are casefolded source
    surfaces — so both samples mean the same thing by them. Group MEMBER ids are
    not: ids are minted per sample (validator/18's refutation 7b354fd4), so they
    travel here in the claim's own namespace for naming and the comparator reads
    only what crosses samples. Topic, conditions and mention roles are still
    absent; they are display labels or a shape the eleven-case corpus does not
    yet produce.

    The shape is v1's `demand_profile` because that is the shape the gate reads,
    and — until the readers dispatch on `schema` — the only shape
    `pulse.profile_summary` and `extract show` can render. Area-level `mentions`
    are deliberately absent: `store.extraction.upsert_state` falls back to
    walking them when a caller passes no precomputed rows, and an area-level
    mention list carrying an area's importance is the exact C04 defect this
    contract kills. Without the key that fallback yields nothing, which is the
    honest answer for a bundle that projects its own rows.

    It is not free: over the eleven case fixtures the index costs about a third
    of the slice (30KB of blob becomes 41KB, ~1KB a document), nearly all of it
    the cited text a legacy renderer needs, and validator/19's five dimensions
    add ~650 bytes a document on top (41KB -> 48KB). That is the price of a gate
    that works, and it stays far short of the record — block accounting,
    presentation areas and the extraction envelope are still only in the
    archive.
    """
    statements = {s["id"]: s for s in record["statements"]}
    entries = record["facts"]["entries"]
    values = _values_by_statement(entries)
    links = _links_by_statement(record["mentions"])
    routes = _routes_by_member(record["relations"]["groups"])

    def semantics(statement_id: str) -> dict[str, Any]:
        return {"values": values.get(statement_id, ()), "links": links.get(statement_id, ()),
                "routes": routes.get(statement_id, ())}

    areas: dict[str, dict[str, Any]] = {
        statement["id"]: {
            "id": statement["id"],
            "name": statement["topic"],
            "kind": statement["kind"],
            "importance": statement["importance"] or NO_IMPORTANCE,
            "level": statement["proficiency"],
            "claims": [_claim(statement, statement["evidence"], **semantics(statement["id"]))],
        }
        for statement in record["statements"]
    }
    for mention in record["mentions"]:
        for statement_id in mention["statement_ids"]:
            if (area := areas.get(statement_id)) is not None:
                area["claims"].append(
                    _claim(statements[statement_id], [mention["evidence"]],
                           **semantics(statement_id))
                )
    unlinked: list[dict[str, Any]] = []
    for entry in entries:
        anchor = next((statements[i] for i in entry["statement_ids"] if i in statements), None)
        # a fact claim carries ITS OWN derived value, not its statement's whole
        # set: it is the one assertion the entry makes on its own span
        claim = _claim(anchor, entry["evidence"]["value"], values=(_value_signature(entry),),
                       links=links.get(anchor["id"], ()) if anchor else (),
                       routes=routes.get(anchor["id"], ()) if anchor else ())
        if anchor is None:
            # a fact no statement claims still says something about the document,
            # and a sample that omits it disagrees with one that reports it
            unlinked.append({"id": entry["id"], "name": entry["family"], "kind": "fact",
                             "importance": NO_IMPORTANCE, "level": None, "claims": [claim]})
        else:
            areas[anchor["id"]]["claims"].append(claim)
    return {"areas": [*areas.values(), *unlinked]}


def _claim(
    statement: dict[str, Any] | None,
    refs: list[dict[str, Any]],
    *,
    values: tuple[str, ...] = (),
    links: tuple[str, ...] = (),
    routes: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """One claim: the cited text and its span, under its statement's labels.

    `negated` is v1's boolean, so `negative` and `ambiguous` both read as "not
    plainly positive" — any split against `positive` escalates, which is what
    the gate asks of polarity. `polarity_target` is where the rest of polarity
    lives (validator/19): the pair a negation actually points at, so two samples
    that agree something is negated and disagree about WHAT ("No sponsorship
    available" as an employer constraint or as a candidate disqualification, the
    spec §3 case) split here, as does negative against ambiguous. It is null for
    a positive statement, which negates nothing and has no target.
    """
    ordered = sorted(refs, key=lambda r: (r["span"][0], r["span"][1]))
    polarity = statement["polarity"] if statement else None
    return {
        "quote": {
            "text": " ".join(str(ref["text"]) for ref in ordered),
            "span": [min(r["span"][0] for r in refs), max(r["span"][1] for r in refs)],
        },
        "importance": statement["importance"] if statement else None,
        "level": statement["proficiency"] if statement else None,
        "negated": statement is not None and statement["polarity"] != "positive",
        "kind": statement["kind"] if statement else None,
        "polarity_target": (
            f"{polarity}:{statement['subject']}"
            if statement is not None and polarity != "positive"
            else None
        ),
        "values": list(values),
        "alternatives": [dict(route) for route in routes],
        "entity_links": list(links),
    }


def _values_by_statement(entries: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """statement id -> the derived values its fact entries carry, sorted."""
    out: dict[str, list[str]] = {}
    for entry in entries:
        signature = _value_signature(entry)
        for statement_id in entry["statement_ids"]:
            out.setdefault(statement_id, []).append(signature)
    return {statement_id: tuple(sorted(v)) for statement_id, v in out.items()}


def _value_signature(entry: dict[str, Any]) -> str:
    """One fact entry's derived value, scope included, as one comparable string.

    Scope is part of the value, not context around it: spec §3's "5 years
    overall including 2 managing" is two entries whose numbers mean different
    things, and a sample that scopes 24 months to management where another
    scopes them to the whole role disagrees about the job.

    Nothing is re-derived here. These numbers came from `facts.py` under a
    frozen validator version, which is exactly why they compare across samples:
    unlike an id, a derived quantity means the same thing in both namespaces.
    The state leads, so `parsed` never silently compares equal to
    `present_unparsed` with the same empty payload.
    """
    derived = entry["derived"]
    scope = entry["scope"]["kind"] if entry["scope"] else ""
    parts = [entry["family"], scope, entry["date_kind"] or "", entry["component"] or "",
             derived["state"]]
    if (quantity := derived["quantity"]) is not None:
        parts += ["q", quantity["dimension"], quantity["comparison"],
                  _number(quantity["min_value"]), _number(quantity["max_value"]),
                  str(quantity["inclusive_min"]), str(quantity["inclusive_max"]),
                  str(quantity["unit"])]
    if (money := derived["money"]) is not None:
        parts += ["m", money["comparison"], str(money["min_amount"]), str(money["max_amount"]),
                  str(money["currency"]), str(money["period"])]
    if (date := derived["date"]) is not None:
        parts += ["d", str(date["date"]), ",".join(date["candidates"] or ())]
    return "|".join(parts)


def _number(value: Any) -> str:
    """A derived number as text, with 144 and 144.0 spelled the same — the JSON
    round trip through the blob decides which of the two a sample carries."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _links_by_statement(mentions: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """statement id -> the casefolded surfaces linked to it, sorted and unique.

    Casefolded because `aliases/1` is: two samples spelling one brand
    differently named the same entity, and a split there would be noise rather
    than a disagreement about the document.
    """
    out: dict[str, set[str]] = {}
    for mention in mentions:
        for statement_id in mention["statement_ids"]:
            out.setdefault(statement_id, set()).add(mention["surface"].casefold())
    return {statement_id: tuple(sorted(s)) for statement_id, s in out.items()}


def _routes_by_member(groups: list[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], ...]]:
    """member id -> the groups it belongs to DIRECTLY, in a stable order.

    Direct membership only: a statement inside a nested group belongs to that
    inner group, which is the operator actually governing it. Members travel as
    the sample's own ids (the comparator never matches them across samples) and
    the operator is what crosses.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        route = {"operator": group["operator"], "members": list(group["members"])}
        for member in group["members"]:
            out.setdefault(member, []).append(route)
    return {
        member: tuple(sorted(routes, key=lambda r: (r["operator"], tuple(r["members"]))))
        for member, routes in out.items()
    }


def mention_rows(record: dict[str, Any]) -> list[tuple[str, str, str]]:
    """`(mention, area_kind, importance)` rows for `profile_mentions`.

    Importance comes from the LINKED STATEMENT — the C04 fix at the write path —
    and `area_kind` carries that statement's kind verbatim, because the spec
    asks consumers to filter by kind before interpreting importance and the
    legacy column is the only place left to put it. Surfaces are written as the
    document spells them and are never re-split: v2 mentions are atomic by
    contract, so v1's `split_mention` decoration-stripping has nothing to do.

    A record that is not `search_eligible` yields no rows at all, and the
    eligibility read here is the SETTLED one (`quality_of`) — the same object
    `profile_of` stores, so the blob and the aggregate can never describe
    different records. An offline record is still ineligible: `assemble` leaves
    the two audit dimensions `not_checked`, and only a completed
    `semantic-audit/v1` phase, folded in by `runner.settle`, clears them. So v2
    populates the profile blob well before it populates the aggregate. That
    asymmetry is the policy, not an oversight: the blob describes one document
    and says how sure it is, while the aggregate is a corpus-wide assertion
    about who demands what.

    What the three columns cannot carry, they drop: a mention's ROLE, an
    alternative route, an applicability condition and a negative polarity all
    project as the plain statement label here. Role is the loss that ships
    today — C12's five preferred-framework examples index byte-identically to
    the direct mention beside them, against spec §9's "retain preferred/example
    semantics through indexing" — because the other three need a record shape
    the eleven-case corpus does not yet produce. The archived record and the
    spec's per-claim table keep all four; carrying them into the aggregate is
    the additive migration increment 3 owns, not a reshape of these columns.

    Importance is v2's vocabulary verbatim, including the three words v1 never
    had (`not_required`, `unstated`, `ambiguous`) — the column is TEXT with no
    CHECK. Flattening `not_required` into one of v1's three would make the
    aggregate assert the opposite of the document; stating it plainly leaves the
    reading to consumers, who filter by kind before importance either way.

    The read surface has not caught up, and that is a known consequence rather
    than an accident: `q claims --importance` (`cli_q.IMPORTANCES`) and the MCP
    `q_claims` tool both validate the argument against v1's closed three-word
    tuple, so a row written under one of the new three is returned by an
    unfiltered `q claims` but can never be selected BY that importance. Nothing
    is lost or mislabeled — the filter is simply narrower than the column.
    Widening it (and deciding what `--importance required` should mean when the
    corpus mixes vocabularies) is the read-surface half of increment 3, the same
    increment that owns the per-claim table; until then no offline v2 record is
    `search_eligible`, so the aggregate carries none of these words yet.
    """
    rows: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for projected in _project_rows({**record, "quality": quality_of(record)}):
        row = (
            projected["surface"],
            projected["kind"],
            projected["importance"] or NO_IMPORTANCE,
        )
        # two statements a mention supports can be indistinguishable in three
        # columns; the aggregate's primary key would reject the second anyway
        if row not in seen:
            seen.add(row)
            rows.append(row)
    return rows


def summary(profile: dict[str, Any]) -> dict[str, Any]:
    """The v2 counterpart of `pulse.profile_summary`: same keys, same value
    shapes, so every renderer that prints a v1 digest prints a v2 one.

    Reads defensively for the same reason its v1 twin does — a stored blob is
    only guaranteed to match the schema of the day it was written.
    """
    return {
        "areas": _areas(profile.get("statements") or []),
        "mentions": _mentions(profile.get("mentions") or []),
        "facts": _facts(profile.get("facts") or {}),
    }


def _areas(statements: list[Any]) -> list[dict[str, Any]]:
    """v1's `areas` list, synthesized from statements grouped by kind and
    importance — the two labels a v1 area carried authoritatively and the two v2
    moved onto the statement. Order is first appearance, so the digest reads in
    document order; the group's name is its statements' topics, and its level is
    the strongest proficiency any of them demands.
    """
    groups: dict[tuple[Any, str], dict[str, Any]] = {}
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        key = (statement.get("kind"), statement.get("importance") or NO_IMPORTANCE)
        group = groups.setdefault(key, {"topics": {}, "level": None})
        topic = statement.get("topic")
        if isinstance(topic, str) and topic:
            group["topics"].setdefault(topic, None)
        group["level"] = _stronger(group["level"], statement.get("proficiency"))
    return [
        {"name": ", ".join(group["topics"]), "kind": kind,
         "importance": importance, "level": group["level"]}
        for (kind, importance), group in groups.items()
    ]


def _stronger(current: str | None, candidate: Any) -> str | None:
    """The higher of two proficiencies, `PROFICIENCY` being strongest-first."""
    if not isinstance(candidate, str) or candidate not in PROFICIENCY:
        return current
    if current is None:
        return candidate
    return min(current, candidate, key=PROFICIENCY.index)


def _mentions(mentions: list[Any]) -> list[str]:
    """The mention surfaces in record order, deduped, bounded like v1's."""
    seen: dict[str, None] = {}
    for mention in mentions:
        surface = mention.get("surface") if isinstance(mention, dict) else None
        if isinstance(surface, str) and surface:
            seen.setdefault(surface, None)
    return list(seen)[:MAX_MENTIONS]


def _facts(facts: dict[str, Any]) -> dict[str, Any]:
    """v1's three headline facts, down-converted from v2's derived values.

    Only `parsed` derivations are reported, and the state is what decides it:
    `present_unparsed` means the document said something the grammar could not
    read, and `conflicting` — the increment-2 auditor's verdict — means it read
    a value the document contradicts. Both summarize honestly as silence, so a
    value carried alongside a non-`parsed` state is skipped rather than
    reported. Money keeps its exact decimal string — rounding a salary into v1's
    integer would be a loss no renderer asked for.
    """
    compensation: list[dict[str, Any]] = []
    experience: dict[str, Any] | None = None
    deadline: str | None = None
    for entry in facts.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        derived = entry.get("derived") or {}
        if derived.get("state") != "parsed":
            continue
        family = entry.get("family")
        if family == "compensation":
            money = derived.get("money")
            if money:
                compensation.append({
                    "min": money.get("min_amount"), "max": money.get("max_amount"),
                    "currency": money.get("currency"), "period": money.get("period"),
                })
        elif family == "experience" and experience is None:
            quantity = derived.get("quantity")
            # months is the only duration unit `facts.py` derives; anything else
            # is a different dimension wearing the experience family's name
            if quantity and quantity.get("unit") == "month":
                floor = quantity.get("min_value")
                experience = {"min": 0 if floor is None else floor,
                              "max": quantity.get("max_value")}
        elif family == "date" and deadline is None:
            date = derived.get("date")
            if entry.get("date_kind") == "application_deadline" and date:
                deadline = date.get("date")
    return {"compensation": compensation, "experience_months": experience,
            "deadline": deadline}
