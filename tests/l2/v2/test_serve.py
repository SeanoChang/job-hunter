"""The three serving projections of a v2 record (`l2/v2/serve.py`).

`profile_of` decides what the `extractions.profile` blob holds, `mention_rows`
decides what the `profile_mentions` aggregate asserts, and `summary` is what
every current renderer reads. All three are pure, and all three are read by the
runner through the v2 bundle, so their contracts are storage contracts.

Records here are assembled from the real case fixtures. Where a test needs a
record an audit has cleared, `_audited` re-runs the frozen `quality.assess`
policy with the two audit dimensions satisfied — never a hand-set
`search_eligible`, which would make the test the policy's author.
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any

import pytest

from jobhunter.hashing import sha256_hex
from jobhunter.l2.agreement import agree
from jobhunter.l2.v2 import serve
from jobhunter.l2.v2.assemble import assemble
from jobhunter.l2.v2.quality import assess

CASES = pathlib.Path(__file__).parent / "cases"
AT = "2026-09-10T00:00:00+00:00"
MODEL = "fixture-hand-authored"


def case_emit(case: str) -> dict[str, Any]:
    """The FROZEN schema-2 emit of a case (`<case>.emit2.json`).

    The case corpus carries both shapes since the v20 bump: `<case>.emit.json`
    is the schema-3 derivation the active bundle runs on, and `.emit2.json` is
    the hand-authored schema-2 original it was derived from. Every case-driven
    test in this file is a pin on the SCHEMA-2 projection — the byte-identity
    guards, the demoted `importance` metric, the legacy `profile_mentions`
    columns — and schema 2 is a shipped corpus partition `rebuild` still
    replays, so those pins stay on the frozen emit. The schema-3 projection is
    covered from the `v3_record` fixtures (conftest) instead, which is where a
    record carrying no verdict at all comes from.
    """
    loaded: dict[str, Any] = json.loads(
        (CASES / f"{case}.emit2.json").read_text(encoding="utf-8")
    )
    body: dict[str, Any] = loaded["emit"]
    return body


def case_record(case: str, emit: dict[str, Any] | None = None) -> dict[str, Any]:
    markdown = (CASES / f"{case}.source.md").read_text(encoding="utf-8")
    return assemble(
        case_emit(case) if emit is None else emit, markdown,
        document_hash=sha256_hex(markdown.encode("utf-8")), observed_model=MODEL, at=AT,
        schema_version="2",
    )


def _audited(record: dict[str, Any]) -> dict[str, Any]:
    """The same record after the increment-2 audit phases complete cleanly.

    Offline assembly always leaves `semantics`/`completeness` at `not_checked`,
    so no assembled record is search-eligible until the auditor exists; this is
    the only thing that changes when it does.
    """
    out = copy.deepcopy(record)
    out["quality"] = assess(
        source=record["quality"]["source"], evidence=record["quality"]["evidence"],
        semantics="no_findings", completeness="no_findings",
    )
    assert out["quality"]["search_eligible"] is True
    return out


# --- profile_of: the stored slice -------------------------------------------


def test_profile_of_is_the_served_slice_under_a_shape_marker(v2_record: dict[str, Any]) -> None:
    profile = serve.profile_of(v2_record)
    assert profile == {
        "schema": "2",
        "statements": v2_record["statements"],
        "relations": v2_record["relations"],
        "facts": v2_record["facts"],
        "mentions": v2_record["mentions"],
        "quality": v2_record["quality"],
        "demand_profile": serve.claim_index(v2_record),
    }


def test_profile_of_leaves_the_bulk_of_the_record_in_the_archive(
    v2_record: dict[str, Any],
) -> None:
    """`pulse` loads every profiled event's blob, so block accounting and the
    extraction envelope stay in the archived attempt, not in Postgres."""
    profile = serve.profile_of(v2_record)
    for key in ("block_accounting", "areas", "extraction", "document", "source_assessment"):
        assert key not in profile
        assert key in v2_record  # ... and the record they were dropped from has them


def test_profile_of_is_idempotent_over_its_own_output(v2_record: dict[str, Any]) -> None:
    """`settle` folds archived records; a caller holding only the stored blob
    must get the same slice back, or a re-fold would change the row."""
    once = serve.profile_of(v2_record)
    assert serve.profile_of(once) == once


# --- the claim index: what the cross-sample agreement gate compares ----------
#
# `runner.settle` hands `bundle.profile_of(record)` to `agreement.cohort_hook`,
# and `agreement._claims` reads exactly one place: `demand_profile.areas[].
# claims[]`. A served slice without that key gives the gate nothing to align,
# so no aligned pair can split and every k-sample cohort certifies itself — the
# audit slot validating no matter how far apart its samples were. Under
# validator/20 the split that parks a document is a polarity flip or a numeric
# conflict on an aligned pair, and both are read out of this index; so are the
# medoid, the dispute set, and the metrics `quality.sample_notes` publishes.
# These tests are the gate's teeth.


def _c01_variant(**changes: Any) -> dict[str, Any]:
    """A second sample of the C01 document: the same emit, one field moved."""
    emit = copy.deepcopy(case_emit("C01"))
    emit["statements"][0].update(changes)
    return serve.profile_of(case_record("C01", emit))


#: the same requirement cited as a narrower substring — one sample reading the
#: whole bullet, the other only its tail (span Jaccard 0.30, below the gate's 0.5)
OTHER_SPAN = [{"block_id": "b000002", "occurrence": 0,
               "text": "a proven track record of exceeding sales targets"}]


def test_two_unrelated_documents_score_zero_f1() -> None:
    """The floor the missing index removed: without the claim index every
    cohort scored a perfect 1.0 on empty claim sets. The index is what makes
    the number mean something — under validator/20 it is a reported metric
    rather than a gate, and a metric that reads 1.0 for two different
    documents would still be a broken measurement."""
    result = agree([serve.profile_of(case_record("C01")),
                    serve.profile_of(case_record("C04"))])
    assert result.report["mean_f1"] == 0.0
    assert result.report["metrics"]["f1"] == 0.0
    assert result.report["failures"] == []  # F1 no longer parks a document


def test_a_sample_citing_different_text_is_an_f1_metric() -> None:
    """The realistic disagreement: two samples of ONE document that do not agree
    on which text carries the requirement. Thoroughness variance, reported."""
    result = agree([serve.profile_of(case_record("C01")), _c01_variant(evidence=OTHER_SPAN)])
    assert result.report["mean_f1"] < 0.8
    assert result.report["failures"] == [] and result.passed is True


def test_a_flipped_importance_no_longer_gates() -> None:
    """Same spans, different demand. Under 19 this parked the document; the
    2026-09-22 analysis found importance labels disagreeing across runs of one
    model and more across models, and schema 3 removed the field rather than
    calibrating it."""
    result = agree([serve.profile_of(case_record("C01")), _c01_variant(importance="preferred")])
    assert result.report["mean_f1"] == 1.0  # the claims align ...
    assert result.report["required_importance_agreement"] == 0.0  # ... and still disagree
    assert result.report["failures"] == [] and result.passed is True


def test_a_flipped_polarity_escalates() -> None:
    """Polarity is the attribution gate's documented blind spot, so a negation
    read one way and not the other escalates on a statement kind a reader acts
    on (`agreement` module docstring) — C01's is a qualification. C01's
    statement and the experience fact hanging off it both carry that polarity,
    so the flip shows up on both aligned pairs."""
    result = agree([serve.profile_of(case_record("C01")), _c01_variant(polarity="negative")])
    assert result.report["negation_disagreements"] == 2
    assert "negation" in result.report["failures"] and result.passed is False


def _variant(case: str, index: int, **changes: Any) -> dict[str, Any]:
    """A second sample of a case document: its schema-2 emit with statement
    `index` moved."""
    emit = copy.deepcopy(case_emit(case))
    emit["statements"][index].update(changes)
    return serve.profile_of(case_record(case, emit))


def test_a_hedged_polarity_is_a_metric_not_a_negation() -> None:
    """The 2026-09-28 amendment on the real projection: a hedge is not a
    denial. The stored claim still says `negated: true` for an `ambiguous`
    statement — that bit is v1's shape and stays — so the gate has to read
    polarity itself, and a positive-vs-ambiguous split lands in the polarity
    metric on both of C01's aligned pairs instead of parking the document."""
    result = agree([serve.profile_of(case_record("C01")), _c01_variant(polarity="ambiguous")])
    assert result.report["negation_disagreements"] == 0
    assert result.report["metrics"]["splits"]["polarity"] == 2
    assert result.report["failures"] == [] and result.passed is True


def test_a_negation_in_a_hiring_policy_is_a_polarity_split_not_a_gate() -> None:
    """C02's English-language requirement is a hiring policy: one sample
    reading it as a restriction on who is considered is a framing split in
    boilerplate, which the gate reports and does not park on."""
    hiring = serve.profile_of(case_record("C02"))
    assert hiring["statements"][1]["kind"] == "hiring_policy"
    result = agree([hiring, _variant("C02", 1, polarity="negative")])
    assert result.report["negation_disagreements"] == 0
    assert result.report["metrics"]["splits"]["polarity"] == 1
    assert result.report["failures"] == [] and result.passed is True


def test_the_gate_leaves_a_hedged_schema_2_claim_byte_identical() -> None:
    """The stored blob is not where the amendment lives: an ambiguous
    statement's claim keeps v1's collapsed bit and the polarity target it has
    carried since validator/19, key for key."""
    blob = _variant("C01", 0, polarity="ambiguous")
    claim = blob["demand_profile"]["areas"][0]["claims"][0]
    assert set(claim) == V2_CLAIM_KEYS
    assert claim["negated"] is True
    assert claim["polarity_target"] == "ambiguous:candidate"


def test_the_claim_index_covers_every_statement_mention_and_fact() -> None:
    """One claim per statement, per (mention, statement) link, and per fact
    entry — every assertion the record makes about the document, so a sample
    that drops one is a disagreement."""
    record = case_record("C04")
    claims = [c for a in serve.claim_index(record)["areas"] for c in a["claims"]]
    assert len(claims) == (
        len(record["statements"])
        + sum(len(m["statement_ids"]) for m in record["mentions"])
        + len(record["facts"]["entries"])
    )
    # ... and the index is derivable from the stored slice, not just the record
    assert serve.claim_index(serve.profile_of(record)) == serve.claim_index(record)


def test_the_claim_index_carries_no_area_level_mentions() -> None:
    """`upsert_state` walks `profile["demand_profile"]` for mentions when a
    caller passes no precomputed rows; an area-level mention list here would
    hand that path v1's area importance again — the exact C04 defect. The index
    carries none, so the fallback yields nothing rather than something wrong."""
    index = serve.claim_index(_audited(case_record("C04")))
    assert all("mentions" not in area for area in index["areas"])


def test_claims_carry_the_fields_a_v1_renderer_prints() -> None:
    """`extract show` prints `claim["quote"]["text"]` and locates it by
    `quote["span"][0]`; `pulse` prints each area's name/kind/importance/level.
    A v2 blob reaching either renderer must render, never raise."""
    for area in serve.claim_index(case_record("C04"))["areas"]:
        assert {"name", "kind", "importance", "level", "claims"} <= set(area)
        for claim in area["claims"]:
            assert isinstance(claim["quote"]["text"], str)
            assert isinstance(claim["quote"]["span"][0], int)
            assert "importance" in claim and "negated" in claim


#: Every key a schema-2 claim has carried since validator/19. Schema 2 is a
#: shipped corpus partition: adding a key here changes stored blob bytes for
#: every archived record, so the schema-3 fields are carried only by schema-3
#: claims and this is the pin that keeps it that way.
V2_CLAIM_KEYS = {"quote", "importance", "level", "negated", "kind",
                 "polarity_target", "values", "alternatives", "entity_links"}
V2_AREA_KEYS = {"id", "name", "kind", "importance", "level", "claims"}


def test_a_schema_2_claim_projects_byte_identically(v2_record: dict[str, Any]) -> None:
    """The regression guard on the schema-3 projection work."""
    for record in (case_record("C01"), case_record("C04"), v2_record):
        for area in serve.claim_index(record)["areas"]:
            assert set(area) == V2_AREA_KEYS
            for claim in area["claims"]:
                assert set(claim) == V2_CLAIM_KEYS


def test_the_c01_schema_2_claim_is_exactly_what_it_was() -> None:
    """The same guard spelled out once, value by value, so a silent change to a
    projected field is as visible as a changed key."""
    area = serve.claim_index(case_record("C01"))["areas"][0]
    assert area == {
        "id": "s_sales_experience",
        "name": "Cloud/software B2B sales or solution engineering experience",
        "kind": "qualification",
        "importance": "required",
        "level": None,
        "claims": [
            {
                "quote": {
                    "text": "- Experience in cloud/software B2B sales or solution "
                            "engineering, with a minimum of 8 years of experience and a "
                            "proven track record of exceeding sales targets.",
                    "span": [31, 190],
                },
                "importance": "required", "level": None, "negated": False,
                "kind": "qualification", "polarity_target": None,
                "values": ["experience|domain|||parsed|q|duration|gte|96|None|True|None|month"],
                "alternatives": [], "entity_links": [],
            },
            {
                "quote": {"text": "8 years", "span": [115, 122]},
                "importance": "required", "level": None, "negated": False,
                "kind": "qualification", "polarity_target": None,
                "values": ["experience|domain|||parsed|q|duration|gte|96|None|True|None|month"],
                "alternatives": [], "entity_links": [],
            },
        ],
    }


# --- schema 3: headings and quoted modality, no verdicts --------------------


def test_a_schema_3_record_projects_without_touching_a_verdict(
    v3_record: dict[str, Any],
) -> None:
    """The KeyError this fixes: `claim_index` indexed `statement["importance"]`
    and `statement["proficiency"]`, neither of which schema 3 has."""
    statements = v3_record["statements"]
    assert all("importance" not in s and "proficiency" not in s for s in statements)
    area = serve.claim_index(v3_record)["areas"][0]
    assert set(area) == V2_AREA_KEYS
    assert area["importance"] == serve.NO_IMPORTANCE and area["level"] is None
    statement_claim = area["claims"][0]
    assert set(statement_claim) == V2_CLAIM_KEYS | {"section_heading", "modality"}
    assert statement_claim["section_heading"] == "Requirements"
    assert statement_claim["modality"] == "A minimum of"
    assert statement_claim["importance"] == serve.NO_IMPORTANCE
    assert statement_claim["level"] is None


def test_a_schema_3_statement_with_no_modal_phrase_projects_null(
    v3_record_with_mentions: dict[str, Any],
) -> None:
    """The posting quotes "required" for the degree and nothing for the
    certification; `modality` is the bound quote's text or null, never an
    inference from the heading (parsing contract v3 §2.1)."""
    areas = {a["id"]: a for a in serve.claim_index(v3_record_with_mentions)["areas"]}
    degree = areas["s_degree"]["claims"][0]
    cert = areas["s_cert"]["claims"][0]
    assert degree["modality"] == "required" and cert["modality"] is None
    assert degree["section_heading"] == cert["section_heading"] == "Requirements"
    assert degree["importance"] == cert["importance"] == serve.NO_IMPORTANCE


def test_profile_of_serves_a_schema_3_record(v3_record: dict[str, Any]) -> None:
    profile = serve.profile_of(v3_record)
    assert profile["statements"] == v3_record["statements"]
    assert profile["demand_profile"] == serve.claim_index(v3_record)
    assert serve.profile_of(profile) == profile  # still idempotent over its own output


def test_the_blob_stamps_the_records_own_schema_version(
    v2_record: dict[str, Any], v3_record: dict[str, Any]
) -> None:
    """The marker is the contract the two shape-aware readers dispatch on, and
    `agreement._gates` reads it to pick a settlement policy — so it has to be
    the record's OWN shape, not the module's default. A v11 record stamped "2"
    would send schema-3 statements to every schema-2 reader there is.
    """
    assert serve.profile_of(v3_record)["schema"] == "3"
    assert serve.profile_of(v2_record)["schema"] == "2"  # the frozen replay path


def test_the_schema_stamp_survives_a_reprojection_of_the_stored_blob(
    v3_record: dict[str, Any],
) -> None:
    """A stored blob carries no `extraction` envelope (it stays in the archive),
    so re-projecting one has to read the marker it already declares — otherwise
    every re-projection of a schema-3 blob would relabel it."""
    blob = serve.profile_of(v3_record)
    assert "extraction" not in blob
    assert serve.profile_of(blob)["schema"] == "3"


def test_mention_rows_for_a_schema_3_record_carry_the_sentinel(
    v3_record_with_mentions: dict[str, Any],
) -> None:
    assert serve.mention_rows(v3_record_with_mentions) == [
        ("CPA", "qualification", serve.NO_IMPORTANCE)
    ]


def test_summary_of_a_schema_3_record_groups_under_the_sentinel(
    v3_record: dict[str, Any],
) -> None:
    """`summary` already read both verdicts defensively; this pins that a
    schema-3 blob summarizes rather than raising or inventing a level."""
    out = serve.summary(serve.profile_of(v3_record))
    assert out["areas"] == [{"name": "Sales experience", "kind": "qualification",
                             "importance": serve.NO_IMPORTANCE, "level": None}]


# --- the marker's readers ----------------------------------------------------


def test_the_stamp_readers_recognise_every_shape_this_module_serves(
    v2_record: dict[str, Any], v3_record: dict[str, Any]
) -> None:
    """The stamp is only worth writing if the readers recognise it.

    `profile_of` stamps the record's own version, so a reader testing the
    marker for equality with one version stops recognising the live shape the
    moment the contract bumps. Both shapes this module projects have to read as
    v2-family; a blob from before the marker existed still must not.
    """
    assert serve.reads_as_v2(serve.profile_of(v3_record))
    assert serve.reads_as_v2(serve.profile_of(v2_record))
    assert not serve.reads_as_v2({"demand_profile": {"areas": []}})  # a v1 blob
    assert sorted(serve.V2_SHAPES) == ["2", "3"]


def test_pulse_summarises_a_schema_3_blob_through_the_v2_projection(
    v3_record_with_mentions: dict[str, Any],
) -> None:
    """`pulse.profile_summary` dispatches on the stamp, so it has to follow it.

    Falling through to the v1 `demand_profile` walk is not a degraded answer,
    it is a wrong one: the walk drops `mentions` entirely and reports each
    statement as its own area carrying `NO_IMPORTANCE` as if that were a
    verdict the posting made — the exact fabrication parsing contract v3 exists
    to remove. Lives here rather than in `tests/test_pulse.py` because what is
    pinned is the stamp's contract with its reader.
    """
    from jobhunter.pulse import profile_summary

    blob = serve.profile_of(v3_record_with_mentions)
    assert blob["schema"] == "3"
    assert profile_summary(blob) == serve.summary(blob)
    assert profile_summary(blob)["mentions"] == ["CPA"]


# --- quality.sample_notes: what the cohort split on (spec §4) ----------------


def _settled(record: dict[str, Any], agreement: dict[str, Any] | None) -> dict[str, Any]:
    """The record as `runner._settled` hands it to the projections."""
    settlement: dict[str, Any] = {
        "lifecycle": "validated", "sampling": "complete", "semantics": "no_findings",
        "completeness": "no_findings", "blocking": 0, "human_review": "none",
    }
    if agreement is not None:
        settlement["agreement"] = agreement
    return {**record, serve.SETTLEMENT: settlement}


def test_sample_notes_name_the_dimensions_the_samples_split_on() -> None:
    """Spec §4: the blob's quality block gains `sample_notes` — which metric
    dimensions split and over how many aligned pairs — so a reading agent sees
    what the cohort disagreed on without re-deriving the gate."""
    report = agree([serve.profile_of(case_record("C01")),
                    _c01_variant(kind="responsibility")]).report
    profile = serve.profile_of(_settled(case_record("C01"), report))
    notes = profile["quality"]["sample_notes"]
    assert notes["k"] == 2
    assert notes["splits"] == {"kind": report["metrics"]["splits"]["kind"]}
    assert notes["aligned_pairs"] == report["metrics"]["aligned_pairs"]
    assert notes["f1"] == report["mean_f1"]


def test_sample_notes_name_a_polarity_split_the_gate_let_through() -> None:
    """What the negation gate stops parking on, the reader still hears about:
    a hedge against an assertion is published as a `polarity` split."""
    report = agree([serve.profile_of(case_record("C01")),
                    _c01_variant(polarity="ambiguous")]).report
    notes = serve.profile_of(_settled(case_record("C01"), report))["quality"]["sample_notes"]
    # a positive statement has no target, so only the polarity itself split
    assert notes["splits"] == {"polarity": 2}


def test_sample_notes_are_absent_when_no_cohort_ran() -> None:
    """An unsampled document (k=1) has no cohort and no notes; the blob keeps
    exactly the seven quality dimensions it had."""
    profile = serve.profile_of(_settled(case_record("C01"), None))
    assert "sample_notes" not in profile["quality"]


def test_sample_notes_are_empty_when_the_cohort_agreed() -> None:
    """A cohort that split on nothing says so: the key is present with no
    splits, which is a different statement from "never sampled"."""
    same = serve.profile_of(case_record("C01"))
    report = agree([same, same]).report
    profile = serve.profile_of(_settled(case_record("C01"), report))
    assert profile["quality"]["sample_notes"]["splits"] == {}


def test_sample_notes_count_the_samples_an_incomplete_cohort_lost() -> None:
    """Parsing contract v3 §5: an exhausted sample budget is monitoring
    information, so the cohort that could not be measured says how far it got.
    `requested` is the slots the sampler opened, `arrived` the records the gate
    actually had to compare."""
    report = agree([serve.profile_of(case_record("C01")),
                    _c01_variant(kind="responsibility")]).report
    report["k"], report["arrived"] = 3, 2  # what `cohort_hook` stamps
    notes = serve.profile_of(_settled(case_record("C01"), report))["quality"]["sample_notes"]
    assert (notes["requested"], notes["arrived"]) == (3, 2)
    assert notes["k"] == 3
    assert notes["splits"] == {"kind": report["metrics"]["splits"]["kind"]}


def test_sample_notes_of_a_complete_cohort_count_nothing() -> None:
    """A cohort that got everything it asked for has nothing out of the
    ordinary to report: `k` is already its arrived count, and the notes keep
    exactly the four keys every reader of them was written against."""
    same = serve.profile_of(case_record("C01"))
    report = agree([same, same]).report
    report["arrived"] = 2
    notes = serve.profile_of(_settled(case_record("C01"), report))["quality"]["sample_notes"]
    assert sorted(notes) == ["aligned_pairs", "f1", "k", "splits"]


def test_sample_notes_survive_a_re_projection_of_the_stored_blob() -> None:
    """`profile_of` is idempotent over its own output and the settlement key is
    consumed, so a caller holding only the blob keeps the notes."""
    report = agree([serve.profile_of(case_record("C01")),
                    _c01_variant(kind="responsibility")]).report
    once = serve.profile_of(_settled(case_record("C01"), report))
    assert serve.profile_of(once) == once


# --- mention_rows: the C04 fix at the write path ----------------------------


def test_mention_rows_take_importance_from_the_linked_statement() -> None:
    """C04: CPA/ACCA/ACA are `preferred` statements sitting in a `credential`
    area that also holds a `required` one. v1 wrote the area's importance; the
    row must read the statement's."""
    record = _audited(case_record("C04"))
    rows = serve.mention_rows(record)
    assert rows == [
        ("CPA", "qualification", "preferred"),
        ("ACCA", "qualification", "preferred"),
        ("ACA", "qualification", "preferred"),
    ]
    assert all(importance != "required" for _, _, importance in rows)


def test_mention_rows_serve_for_any_record_regardless_of_eligibility(
    v2_record_with_mentions: dict[str, Any],
) -> None:
    """Two-tier serving (2026-09-18 ruling): the skill listing serves for
    every record this function is handed — validated-status admission lives
    in the store — and eligibility no longer starves it. Audited and
    unaudited records project identical rows; `search_eligible` stays in the
    quality block for claim-tier consumers."""
    assert v2_record_with_mentions["quality"]["search_eligible"] is False
    assert serve.mention_rows(v2_record_with_mentions) == [
        ("CPA", "qualification", "preferred")
    ]
    assert serve.mention_rows(_audited(v2_record_with_mentions)) == [
        ("CPA", "qualification", "preferred")
    ]


def test_mention_rows_read_the_stored_blob_too() -> None:
    """`settle` holds the record; the blob is a superset of what the projection
    reads, so the aggregate and the profile can never disagree."""
    record = _audited(case_record("C04"))
    assert serve.mention_rows(serve.profile_of(record)) == serve.mention_rows(record)


def test_one_mention_supporting_two_equal_statements_is_one_row() -> None:
    """Two statements of the same kind and importance collapse into one row —
    the columns cannot tell them apart, and the PK would reject the duplicate."""
    emit = copy.deepcopy(case_emit("C04"))
    cpa = next(m for m in emit["mentions"] if m["id"] == "m_cpa")
    cpa["statement_ids"] = ["s_cpa", "s_netsuite"]  # both `qualification`/`preferred`
    rows = serve.mention_rows(_audited(case_record("C04", emit)))
    assert rows.count(("CPA", "qualification", "preferred")) == 1


def test_v2_importance_words_reach_the_column_verbatim() -> None:
    """A recorded deviation, pinned so the decision is made rather than found in
    production: `profile_mentions.importance` is TEXT with no CHECK and has only
    ever held v1's three words, and v2 writes its own five. `not_required` in
    the demand aggregate inverts the reading of any consumer that treats a row
    as "this employer wants X" — the column's shape is increment 3's call
    (spec §7's per-claim table), so v2 states its vocabulary here rather than
    flattening `not_required` into a word that means the opposite."""
    emit = copy.deepcopy(case_emit("C04"))
    statement = next(s for s in emit["statements"] if s["id"] == "s_cpa")
    statement["importance"] = "not_required"
    rows = serve.mention_rows(_audited(case_record("C04", emit)))
    assert ("CPA", "qualification", "not_required") in rows


def test_statements_without_importance_project_as_contextual() -> None:
    """Responsibilities, compensation statements and employer context carry a
    null importance (spec §3); the NOT NULL column takes v1's word for
    "named, not demanded"."""
    emit = copy.deepcopy(case_emit("C04"))
    statement = next(s for s in emit["statements"] if s["id"] == "s_cpa")
    statement["kind"] = "responsibility"
    statement["importance"] = None
    statement["importance_evidence"] = None
    rows = serve.mention_rows(_audited(case_record("C04", emit)))
    assert rows == [
        ("CPA", "responsibility", "contextual"),
        ("ACCA", "responsibility", "contextual"),
        ("ACA", "responsibility", "contextual"),
    ]


# --- summary: the v1 renderer contract --------------------------------------

V1_SUMMARY_KEYS = {"areas", "mentions", "facts"}
V1_FACT_KEYS = {"compensation", "experience_months", "deadline"}


def test_summary_speaks_the_v1_summary_shape(v2_record: dict[str, Any]) -> None:
    out = serve.summary(serve.profile_of(v2_record))
    assert set(out) == V1_SUMMARY_KEYS
    assert set(out["facts"]) == V1_FACT_KEYS
    assert set(out["areas"][0]) == {"name", "kind", "importance", "level"}


def test_summary_carries_the_c01_floor_not_a_bounded_range() -> None:
    """C01's "a minimum of 8 years" is 96 months with no ceiling — the audit
    defect was a 96..96 range, and the summary must not reintroduce it."""
    out = serve.summary(serve.profile_of(case_record("C01")))
    assert out["facts"]["experience_months"] == {"min": 96, "max": None}
    assert out["facts"]["compensation"] == [] and out["facts"]["deadline"] is None


def test_summary_areas_group_statement_topics_by_kind_and_importance() -> None:
    out = serve.summary(serve.profile_of(case_record("C04")))
    assert out["areas"] == [
        {
            "name": "NetSuite or other revenue recognition systems, "
                    "CPA or ACCA/ACA certification",
            "kind": "qualification", "importance": "preferred", "level": None,
        },
        {
            "name": "ASC 606 application experience",
            "kind": "qualification", "importance": "required", "level": None,
        },
    ]


def test_summary_mentions_are_the_surfaces_in_record_order() -> None:
    out = serve.summary(serve.profile_of(case_record("C04")))
    assert out["mentions"] == ["CPA", "ACCA", "ACA"]


def test_summary_mentions_are_bounded_like_the_v1_summary() -> None:
    blob = {"mentions": [{"surface": f"s{i}"} for i in range(serve.MAX_MENTIONS + 5)]}
    assert len(serve.summary(blob)["mentions"]) == serve.MAX_MENTIONS


def test_the_mention_bound_is_pulses_bound() -> None:
    """`serve` restates the constant instead of importing `pulse` (which reaches
    into the store); this is what keeps the two from drifting apart."""
    from jobhunter.pulse import MAX_MENTIONS

    assert serve.MAX_MENTIONS == MAX_MENTIONS


def test_summary_keeps_compensation_amounts_exactly() -> None:
    """C02's annual band: the period survives, "$" alone never implies USD, and
    the decimal strings are passed through rather than rounded into ints."""
    out = serve.summary(serve.profile_of(case_record("C02")))
    assert out["facts"]["compensation"] == [
        {"min": "210300", "max": "273400", "currency": None, "period": "year"}
    ]


def test_summary_reports_the_application_deadline() -> None:
    blob = {
        "facts": {
            "entries": [
                {"family": "date", "date_kind": "interview_date",
                 "derived": {"state": "parsed", "date": {"date": "2026-10-01"}}},
                {"family": "date", "date_kind": "application_deadline",
                 "derived": {"state": "parsed", "date": {"date": "2026-11-30"}}},
            ]
        }
    }
    assert serve.summary(blob)["facts"]["deadline"] == "2026-11-30"


def test_summary_ignores_facts_the_grammar_could_not_parse() -> None:
    """`present_unparsed` is stated-but-unparsed, never a value to invent."""
    blob = {
        "facts": {
            "entries": [
                {"family": "experience",
                 "derived": {"state": "present_unparsed", "quantity": None}},
                {"family": "compensation",
                 "derived": {"state": "present_unparsed", "money": None}},
            ]
        }
    }
    facts = serve.summary(blob)["facts"]
    assert facts == {"compensation": [], "experience_months": None, "deadline": None}


def test_summary_ignores_a_derivation_the_auditor_marked_conflicting() -> None:
    """`parsed` is the only state the summary reports. `conflicting` is the
    increment-2 auditor's verdict — the grammar read a value, and the document
    contradicts it — and a headline fact is the wrong place to launder that into
    a number (spec §6 lists it as a state requiring review)."""
    blob = {
        "facts": {
            "entries": [
                {"family": "compensation",
                 "derived": {"state": "conflicting",
                             "money": {"min_amount": "100", "max_amount": "200",
                                       "currency": "USD", "period": "year"}}},
                {"family": "experience",
                 "derived": {"state": "conflicting",
                             "quantity": {"unit": "month", "min_value": 12,
                                          "max_value": None}}},
                {"family": "date", "date_kind": "application_deadline",
                 "derived": {"state": "conflicting", "date": {"date": "2026-11-30"}}},
            ]
        }
    }
    assert serve.summary(blob)["facts"] == {
        "compensation": [], "experience_months": None, "deadline": None
    }


@pytest.mark.parametrize("blob", [{}, {"statements": None}, {"facts": {}}])
def test_summary_reads_defensively(blob: dict[str, Any]) -> None:
    """A blob written under an older shape must summarize, never raise."""
    out = serve.summary(blob)
    assert out == {"areas": [], "mentions": [],
                   "facts": {"compensation": [], "experience_months": None, "deadline": None}}
