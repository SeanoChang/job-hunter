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

Pure like every other `l2/v2` module: no I/O, no environment, no store imports.
"""

from __future__ import annotations

from typing import Any

from jobhunter.l2.v2.project import mention_rows as _project_rows
from jobhunter.l2.v2.types import PROFICIENCY

SCHEMA_VERSION = "2"

#: `pulse.MAX_MENTIONS`, restated rather than imported: `pulse` reaches into the
#: store and importing it here would end this module's purity. The two are pinned
#: together by a test.
MAX_MENTIONS = 8

#: What a statement with no importance projects as in the legacy columns.
#: Responsibilities, compensation statements and employer context carry a null
#: importance by contract (spec §3), and the column is NOT NULL; `contextual` is
#: v1's existing word for "named by the posting, not demanded by it".
NO_IMPORTANCE = "contextual"


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
    """
    return {
        "schema": SCHEMA_VERSION,
        "statements": record["statements"],
        "relations": record["relations"],
        "facts": record["facts"],
        "mentions": record["mentions"],
        "quality": record["quality"],
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

    What the legacy claim cannot carry, this does not pretend to compare: kind,
    topic, scope, derived VALUES, conditions, alternatives and mention roles are
    all absent, so two samples that cite the same text and disagree about what
    it means still pass. Spec §6's v2 comparator (aligned statement kind,
    importance, polarity, scoped fact values, entity links, alternatives) is the
    successor and bumps `VALIDATOR_VERSION` when it lands; this is the floor
    beneath it, not that.

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
    the cited text a legacy renderer needs. That is the price of a gate that
    works, and it stays far short of the record — block accounting, presentation
    areas and the extraction envelope are still only in the archive.
    """
    statements = {s["id"]: s for s in record["statements"]}
    areas: dict[str, dict[str, Any]] = {
        statement["id"]: {
            "id": statement["id"],
            "name": statement["topic"],
            "kind": statement["kind"],
            "importance": statement["importance"] or NO_IMPORTANCE,
            "level": statement["proficiency"],
            "claims": [_claim(statement, statement["evidence"])],
        }
        for statement in record["statements"]
    }
    for mention in record["mentions"]:
        for statement_id in mention["statement_ids"]:
            if (area := areas.get(statement_id)) is not None:
                area["claims"].append(_claim(statements[statement_id], [mention["evidence"]]))
    unlinked: list[dict[str, Any]] = []
    for entry in record["facts"]["entries"]:
        anchor = next((statements[i] for i in entry["statement_ids"] if i in statements), None)
        claim = _claim(anchor, entry["evidence"]["value"])
        if anchor is None:
            # a fact no statement claims still says something about the document,
            # and a sample that omits it disagrees with one that reports it
            unlinked.append({"id": entry["id"], "name": entry["family"], "kind": "fact",
                             "importance": NO_IMPORTANCE, "level": None, "claims": [claim]})
        else:
            areas[anchor["id"]]["claims"].append(claim)
    return {"areas": [*areas.values(), *unlinked]}


def _claim(statement: dict[str, Any] | None, refs: list[dict[str, Any]]) -> dict[str, Any]:
    """One claim: the cited text and its span, under its statement's labels.

    `negated` is v1's boolean, so `negative` and `ambiguous` both read as "not
    plainly positive" — any split against `positive` escalates, which is what
    the gate asks of polarity, while a negative/ambiguous split is one of the
    distinctions the successor comparator has to make.
    """
    ordered = sorted(refs, key=lambda r: (r["span"][0], r["span"][1]))
    return {
        "quote": {
            "text": " ".join(str(ref["text"]) for ref in ordered),
            "span": [min(r["span"][0] for r in refs), max(r["span"][1] for r in refs)],
        },
        "importance": statement["importance"] if statement else None,
        "level": statement["proficiency"] if statement else None,
        "negated": statement is not None and statement["polarity"] != "positive",
    }


def mention_rows(record: dict[str, Any]) -> list[tuple[str, str, str]]:
    """`(mention, area_kind, importance)` rows for `profile_mentions`.

    Importance comes from the LINKED STATEMENT — the C04 fix at the write path —
    and `area_kind` carries that statement's kind verbatim, because the spec
    asks consumers to filter by kind before interpreting importance and the
    legacy column is the only place left to put it. Surfaces are written as the
    document spells them and are never re-split: v2 mentions are atomic by
    contract, so v1's `split_mention` decoration-stripping has nothing to do.

    A record that is not `search_eligible` yields no rows at all. Every offline
    record is ineligible today — `quality.assess` leaves the two audit
    dimensions `not_checked` until the auditor lands — so v2 populates the
    profile blob well before it populates the aggregate. That asymmetry is the
    policy, not an oversight: the blob describes one document and says how sure
    it is, while the aggregate is a corpus-wide assertion about who demands what.

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
    """
    rows: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for projected in _project_rows(record):
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
