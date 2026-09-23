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
from jobhunter.l2.v2.types import NO_IMPORTANCE, PROFICIENCY

#: The shape a blob declares when the record it was built from declares none.
#: Schema 2 is the oldest v2-family shape and the one every blob written before
#: the v20 bump carries, so it is what an un-named record reads back as. It is
#: NOT what `profile_of` stamps for a schema-3 record: the marker is the
#: record's own `extraction.schema_version` (`_schema_of`).
SCHEMA_VERSION = "2"

#: The blob key the shape marker is written under. `agreement._gates` keys the
#: settlement policy on it (its `_CONTRACT_KEY`), so the two are pinned by a
#: test rather than by coincidence.
_SCHEMA_KEY = "schema"

#: Every shape marker this module's projections read. It is a SET because a
#: reader that dispatches on one version stops seeing the shape the moment the
#: contract bumps — `profile_of` stamps the record's own version, so a blob of
#: the live shape carried a marker no reader recognised and fell through to the
#: v1 walk, which drops mentions and invents an importance the shape does not
#: have. The membership test is `reads_as_v2`, and both readers use it.
V2_SHAPES: frozenset[str] = frozenset({"2", "3"})

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

def _importance_of(statement: dict[str, Any] | None) -> str | None:
    """A statement's importance, where the shape it was written under has one.

    Schema 3 dropped the field entirely (parsing contract v3 §2.1), so this
    never INDEXES it: a statement with no `importance` key is not a statement
    whose importance is unknown, it is one the contract says carries no verdict,
    and `NO_IMPORTANCE` is that. A schema-2 statement reads exactly as before,
    null included, which is what keeps its projection byte-identical.
    """
    if statement is None:
        return None
    if "importance" not in statement:
        return NO_IMPORTANCE
    value: str | None = statement["importance"]
    return value


def _modality_of(statement: dict[str, Any]) -> str | None:
    """The posting's own modal phrase for this statement, as text.

    `modality_evidence` is zero or one bound reference (schema 3); the quote is
    what a reading agent needs, and the reference itself stays in the record.
    """
    refs = statement.get("modality_evidence")
    if not isinstance(refs, list) or not refs:
        return None
    text = refs[0].get("text")
    return text if isinstance(text, str) else None


def _sample_notes(agreement: Any) -> dict[str, Any] | None:
    """What the cohort's samples split on, for the agent reading the record
    (parsing contract v3 §4).

    The dimensions validator/20 demoted are reported in the agreement report's
    `metrics`; this carries the ones that actually split into the stored quality
    block, with the aligned-pair count they split out of, so the reader sees
    "the samples labelled this sentence two different kinds" without re-deriving
    the gate. `None` when no cohort ran — an unsampled document has nothing to
    say here, and saying nothing is not the same as saying the samples agreed.

    A cohort that could not spend its budget reports that too: `requested`
    against `arrived`, the slots the sampler opened against the records the
    gate actually had to compare. Under validator/20 an exhausted budget no
    longer parks the document (parsing contract v3 §5 — the 373-of-1,000
    "incomplete cohort" review class was exactly this), so the counts are how
    a reading agent learns that a served record was measured against fewer
    samples than the drain asked for. They appear only when something WAS
    lost: for a cohort that got everything, `k` is already its arrived count.

    Defensive like every reader of a stored blob: a report written under an
    older validator carries no `metrics` and yields no notes.
    """
    if not isinstance(agreement, dict):
        return None
    metrics = agreement.get("metrics")
    if not isinstance(metrics, dict):
        return None
    splits = metrics.get("splits")
    splits = splits if isinstance(splits, dict) else {}
    notes = {
        "k": agreement.get("k"),
        "f1": metrics.get("f1"),
        "aligned_pairs": metrics.get("aligned_pairs"),
        "splits": {
            dimension: count
            for dimension, count in sorted(splits.items())
            if isinstance(count, int) and not isinstance(count, bool) and count
        },
    }
    requested, arrived = _count(agreement.get("k")), _count(agreement.get("arrived"))
    if requested is not None and arrived is not None and arrived < requested:
        notes["requested"], notes["arrived"] = requested, arrived
    return notes


def _count(value: Any) -> int | None:
    """A stored count read as one: an int, and never a bool wearing one."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
    assessed = assess(
        source=str(quality.get("source")),
        evidence=str(quality.get("evidence")),
        semantics=str(settlement.get("semantics", "not_checked")),
        completeness=str(settlement.get("completeness", "not_checked")),
        sampling=str(settlement.get("sampling", "not_requested")),
        human_review=str(settlement.get("human_review", "none")),
        blocking_findings=blocking if isinstance(blocking, int) and blocking is not True else 0,
        lifecycle=str(settlement.get("lifecycle") or "pending"),
    )
    # ... plus the one thing the eligibility policy has no opinion about: what
    # the cohort's samples split on (parsing contract v3 §4). It is attached
    # here rather than inside `assess` because `assess` is the frozen policy
    # that decides `search_eligible`, and these notes decide nothing.
    notes = _sample_notes(settlement.get("agreement"))
    if notes is not None:
        assessed["sample_notes"] = notes
    return assessed


def reads_as_v2(profile: dict[str, Any]) -> bool:
    """True when this stored blob is one `summary` and the v2 renderers read.

    The single dispatch point between the two record families a profile blob
    can hold. Nothing sniffs structure: the marker `profile_of` stamped is the
    only signal, and a blob written before the marker existed has none, so it
    takes the v1 walk — byte-identical to what it has always returned.
    """
    return profile.get(_SCHEMA_KEY) in V2_SHAPES


def _schema_of(record: dict[str, Any]) -> str:
    """The shape marker for this record: the shape it declares, not this
    module's default.

    Two callers, two shapes of input. A RECORD carries the shape in its
    extraction envelope, and that is the authority — a v11 record stamped "2"
    would route schema-3 statements to every schema-2 reader there is, and
    `agreement._gates` would pick a settlement policy off a lie. A stored BLOB
    has no envelope (it stays in the archived attempt) but carries the marker
    it was written with, so re-projecting one keeps its own stamp, which is
    what `profile_of`'s idempotence promises. Neither present is schema 2: the
    default is the shape every blob written before the v20 bump holds.
    """
    extraction = record.get("extraction")
    if isinstance(extraction, dict):
        declared = extraction.get("schema_version")
        if isinstance(declared, str) and declared:
            return declared
    marker = record.get(_SCHEMA_KEY)
    return marker if isinstance(marker, str) and marker else SCHEMA_VERSION


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
        _SCHEMA_KEY: _schema_of(record),
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
    of it: `demand_profile.areas[].claims[]`. A slice without that path is not
    compared leniently, it is not compared at all — there is nothing to align,
    so no aligned pair can split, so the audit slot (5% of documents, the only
    cohorts validator/20 samples) would certify itself however far apart its
    samples were. Everything settlement does with a cohort reads through here:
    both gates — a polarity split and a numeric conflict, each asked of an
    ALIGNED pair — the medoid the audit then runs on, the dispute set findings
    are scoped against, and the demoted metrics `profile_of` publishes as
    `quality.sample_notes`. `profile_of` therefore carries it.

    One claim per statement, per (mention, statement) link, and per fact entry:
    every assertion the record makes about the document, anchored at the span it
    cites and carrying its statement's importance and polarity. A claim's span
    is the envelope of the refs it cites, because the gate aligns claims by span
    overlap and a claim has one textual footprint.

    Validator/19 added spec §6's remaining comparator dimensions to each claim —
    statement `kind`, the `polarity_target` a negation actually points at, the
    `values` its fact entries derived, the `alternatives` it participates in,
    and its `entity_links` — and validator/20 demoted all five to metrics
    (parsing contract v3 §3: 294 of 300 parked documents split on label variance
    over text both samples read the same way). They are still carried and still
    compared, because what they measure is what `quality.sample_notes` tells the
    reading agent; they simply park nothing. `values` is the exception that
    still gates, and only in part: `agreement._numbers` strips the family/scope
    tags off each signature, so two samples that both PARSED a number from one
    span and disagree about it fail `numeric_conflict`, while the same number
    under two scope tags stays a metric. Carrying all of it is this function's
    half; comparing it is `agreement`'s, and none of it touches F1.

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
            "importance": _importance_of(statement) or NO_IMPORTANCE,
            "level": statement.get("proficiency"),
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

    A SCHEMA-3 claim carries two more fields and a schema-2 one carries neither
    (parsing contract v3 §2.1): the code-derived `section_heading` and
    `modality`, the posting's own modal phrase as text or null. They are added
    only where the statement has them because schema 2 is a shipped corpus
    partition — a key added unconditionally would change the stored blob of
    every archived record for no reader's benefit.
    """
    ordered = sorted(refs, key=lambda r: (r["span"][0], r["span"][1]))
    polarity = statement["polarity"] if statement else None
    claim: dict[str, Any] = {
        "quote": {
            "text": " ".join(str(ref["text"]) for ref in ordered),
            "span": [min(r["span"][0] for r in refs), max(r["span"][1] for r in refs)],
        },
        "importance": _importance_of(statement),
        "level": statement.get("proficiency") if statement else None,
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
    if statement is not None and "section_heading" in statement:
        claim["section_heading"] = statement["section_heading"]
        claim["modality"] = _modality_of(statement)
    return claim


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

    Every record this function is handed yields rows (2026-09-18 two-tier
    ruling): the store's validated-status gate is the only admission test for
    the skill LISTING, because a validated record's mentions already passed
    the validator's grounding checks and an unaudited-but-validated record
    listing its skills is useful where a starved aggregate is not (133 of
    1,562 validated docs served rows under the old `search_eligible` gate).
    `search_eligible` keeps meaning what it says — the audited tier that backs
    claim-level assertions — and travels in the stored quality block
    (`quality_of`, the same settled object `profile_of` stores), so consumers
    that need the stronger tier still have it per document.

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
    # two-tier serving (2026-09-18 ruling): the skill LISTING serves for every
    # record the store accepts (validated status, gated there) — grounding is
    # validator-enforced and needs no audit. `search_eligible` stays the gate
    # for claim-level assertions, carried in the stored quality block; it no
    # longer starves the aggregate (133 of 1,562 validated docs served rows).
    for projected in _project_rows(
        {**record, "quality": quality_of(record)}, include_ineligible=True
    ):
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
