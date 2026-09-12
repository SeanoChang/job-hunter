"""Integration: the drain loop under the v2 bundle, over Postgres + LocalFS.

The loop itself is not re-tested here — `test_runner.py` owns ladder, breaker,
caps and catch-up. What this file proves is that selecting `v2` swaps every one
of the six engine-tuple pieces at once: the v2 prompt is what gets archived, the
schema-2 emit is what gets validated, `l2/v2/assemble` is what binds it,
`l2/v2/verify` is what judges it, and the two `l2/v2/serve` projections are what
reach `extractions.profile` and `profile_mentions`.

It also owns the `semantic-audit/v1` phase (spec §4/§5/§6): the audit runs after
sample collection and BEFORE settlement, its artifact is archived under the
candidate attempt it audited, and `settle` reads it back through the archive —
so live, catch-up and replay fold identically.

Recorded emits only — the same hand-authored case fixtures `tests/l2/v2/` runs
its contracts over. The auditor is scripted from the prompt it is handed. No
model is ever called.
"""

from __future__ import annotations

import copy
import gzip
import json
import pathlib
import re
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.hashing import sha256_hex
from jobhunter.l2.attempts import from_bytes, to_bytes
from jobhunter.l2.bundles import get_bundle
from jobhunter.l2.engines import EngineFatalError, EngineResult, EngineTransportError
from jobhunter.l2.runner import run, settle
from jobhunter.l2.v2.assemble import assemble
from jobhunter.l2.v2.audit import AUDIT_VERSION
from jobhunter.store import extraction
from tests.l2.test_attempts import _attempt
from tests.l2.test_runner import FakeEngine, _seed_doc, _settings, store  # noqa: F401

Conn = psycopg.Connection[dict[str, Any]]

CASES = pathlib.Path(__file__).parent / "v2" / "cases"
GLOBS = ("z-ai/*",)
MODEL = "z-ai/glm-5.2:free"
V2_TUPLE = ("demand-profile/v9", "2", "17")
# C04's three certifications, as `profile_mentions` rows once an audit clears
# the record: the importance is the linked STATEMENT's, not the area's.
C04_ROWS = [
    ("ACA", "qualification", "preferred"),
    ("ACCA", "qualification", "preferred"),
    ("CPA", "qualification", "preferred"),
]


def source(case: str) -> str:
    return (CASES / f"{case}.source.md").read_text(encoding="utf-8")


def emit_of(case: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((CASES / f"{case}.emit.json").read_text(encoding="utf-8"))
    body: dict[str, Any] = loaded["emit"]
    return body


def v2_settings(**env: str) -> Any:
    return _settings(JOB_HUNTER_L2_BUNDLE="v2", **env)


def seed_case(pg: Conn, case: str) -> str:
    markdown = source(case)
    dh = sha256_hex(markdown.encode("utf-8"))
    _seed_doc(pg, dh=dh, markdown=markdown, uid=f"gh:x:{case}")
    return dh


def result(emit: dict[str, Any]) -> EngineResult:
    return EngineResult(json.dumps(emit), MODEL, 40, 9, 0.0)


def attempts_in(archive: ArchiveStore) -> list[Any]:
    return [from_bytes(archive.get(k)) for k in sorted(archive.list(keys.X_ATTEMPTS_PREFIX))]


def audits_in(archive: ArchiveStore) -> dict[str, dict[str, Any]]:
    """Every archived audit artifact, by key: gzipped JSON at `x_audit_key`."""
    return {
        key: json.loads(gzip.decompress(archive.get(key)))
        for key in sorted(archive.list(keys.X_AUDITS_PREFIX))
    }


def archive_audit(
    archive: ArchiveStore,
    attempt_key: str,
    *,
    semantics: str = "no_findings",
    completeness: str = "no_findings",
    blocking: int = 0,
    outcome: str = "ok",
) -> None:
    """One audit artifact, written by hand — the read half of the contract.

    Settlement probes `x_audit_key(candidate)` and needs exactly three values
    out of it; writing them here (rather than through the runner) is what pins
    the format `settle` reads against the format the audit phase writes.
    """
    artifact = {
        "audit_version": AUDIT_VERSION, "outcome": outcome, "attempt_key": attempt_key,
        "semantics": semantics, "completeness": completeness, "blocking": blocking,
        "findings": [], "unresolved": [],
    }
    archive.put(
        keys.x_audit_key(attempt_key),
        gzip.compress(json.dumps(artifact, sort_keys=True).encode("utf-8"), mtime=0),
    )


def mention_rows_in(pg: Conn) -> list[tuple[str, str, str]]:
    return [
        (r["mention"], r["area_kind"], r["importance"])
        for r in pg.execute(
            "SELECT mention, area_kind, importance FROM profile_mentions"
            " ORDER BY mention, area_kind, importance"
        ).fetchall()
    ]


def settled_rows(pg: Conn) -> dict[str, Any]:
    """Every decision a fold reached, minus the timestamps a re-fold moves."""
    return {
        row["document_hash"]: (
            row["status"], row["k"], row["chosen_attempt"],
            (row["profile"] or {}).get("quality"),
        )
        for row in pg.execute("SELECT * FROM extractions").fetchall()
    }


# --- the scripted auditor --------------------------------------------------

_HASH_LINE = re.compile(r"CANDIDATE HASH: ([0-9a-f]{64})")


def candidate_hash_in(prompt: str) -> str:
    match = _HASH_LINE.search(prompt)
    assert match is not None, "the audit prompt carries the candidate hash"
    return match.group(1)


def candidate_in(prompt: str) -> dict[str, Any]:
    body = prompt.split("<<<CANDIDATE JSON\n", 1)[1].split("\nCANDIDATE JSON>>>", 1)[0]
    loaded: dict[str, Any] = json.loads(body)
    return loaded


def clean_audit(prompt: str) -> str:
    return json.dumps(
        {"candidate_hash": candidate_hash_in(prompt), "findings": [], "unresolved": []}
    )


def blocking_audit(prompt: str) -> str:
    """One `importance` finding against the candidate's first statement."""
    statement = candidate_in(prompt)["statements"][0]["id"]
    return json.dumps({
        "candidate_hash": candidate_hash_in(prompt),
        "findings": [{
            "code": "importance", "targets": [statement], "evidence": None,
            "explanation": "the posting words this as preferred, not required",
        }],
        "unresolved": [],
    })


def unparseable_audit(prompt: str) -> str:
    return "the candidate looks fine to me"  # not JSON: an audit error, never a pass


def foreign_audit(prompt: str) -> str:
    """A valid audit of somebody else's candidate — `judge` must refuse it."""
    return json.dumps({"candidate_hash": "f" * 64, "findings": [], "unresolved": []})


class AuditingEngine(FakeEngine):
    """`FakeEngine` plus a scripted auditor.

    Audit calls are told apart from extraction calls by the candidate-hash line
    the `semantic-audit/v1` template opens with, so a test never has to know
    where in the call order the audit lands — which is the point, since where it
    lands is what these tests are about. `audit` answers with the raw response
    text, or raises the exception it returns.
    """

    def __init__(self, script: list[Any], audit: Any) -> None:
        super().__init__(script)
        self._audit = audit
        self.audits: list[str] = []

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        if "CANDIDATE HASH:" not in prompt:
            return super().complete(prompt, schema, model)
        self.audits.append(prompt)
        answer = self._audit(prompt)
        if isinstance(answer, Exception):
            raise answer
        return EngineResult(answer, MODEL, 30, 5, 0.0)


def divergent_c01() -> dict[str, Any]:
    """A C01 emit citing a different span for the same requirement."""
    emit = copy.deepcopy(emit_of("C01"))
    emit["statements"][0]["evidence"] = [
        {"block_id": "b000002", "occurrence": 0,
         "text": "a proven track record of exceeding sales targets"}
    ]
    return emit


def polarity_split_c04() -> dict[str, Any]:
    """A C04 emit that reads the certification clause as a prohibition.

    Polarity is the agreement gate's zero-tolerance dimension, so a cohort
    carrying this disagrees however well its spans line up.
    """
    emit = copy.deepcopy(emit_of("C04"))
    emit["statements"][1]["polarity"] = "negative"
    emit["statements"][1]["polarity_evidence"] = [
        {"block_id": "b000003", "text": "not required", "occurrence": 0}
    ]
    return emit


def divergent_c09() -> dict[str, Any]:
    """A C09 emit citing the tail of the alternative route, not the whole clause."""
    emit = copy.deepcopy(emit_of("C09"))
    emit["statements"][1]["evidence"] = [
        {"block_id": "b000002", "occurrence": 0,
         "text": "incident management or operational resilience"}
    ]
    return emit


# --- the tuple, the prompt, the archive ------------------------------------


def test_a_v2_document_settles_validated_with_a_schema_2_blob(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """C01 end to end: the v2 prompt in, schema-2 emit back, v2 record assembled and
    verified, the served slice stored under the v9/2/16 configuration."""
    dh = seed_case(pg, "C01")
    engine = AuditingEngine([result(emit_of("C01"))], clean_audit)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1 and summary.docs_attempted == 1

    row = pg.execute("SELECT * FROM extractions").fetchone()
    assert row is not None
    assert row["status"] == "validated" and row["document_hash"] == dh
    assert (row["prompt_version"], row["schema_version"], row["validator_version"]) == V2_TUPLE

    profile = row["profile"]
    assert profile["schema"] == "2"
    assert [s["id"] for s in profile["statements"]] == ["s_sales_experience"]
    # the C01 contract survives the round trip: a floor, not a bounded range
    assert profile["facts"]["entries"][0]["derived"]["quantity"] == {
        "dimension": "duration", "comparison": "gte", "min_value": 96, "max_value": None,
        "inclusive_min": True, "inclusive_max": None, "unit": "month",
    }
    # ... and the bulk of the record stayed in the archive
    assert "block_accounting" not in profile and "extraction" not in profile


def test_the_v2_prompt_and_schema_are_archived_write_once(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    seed_case(pg, "C01")
    run(v2_settings(), pg, store, engine=AuditingEngine([result(emit_of("C01"))], clean_audit),
        max_docs=10, max_usd=5.0)
    assert store.exists(keys.x_prompt_key("demand-profile/v9"))
    assert store.exists(keys.x_schema_key("2"))
    attempt = attempts_in(store)[0]
    assert attempt.outcome == "ok"
    assert (attempt.prompt_version, attempt.schema_version, attempt.validator_version) == V2_TUPLE
    assert attempt.record is not None
    assert attempt.record["extraction"]["schema_version"] == "2"
    assert attempt.record["block_accounting"]  # the archive keeps what the blob drops


def test_an_unaudited_record_stores_its_profile_but_indexes_no_mentions(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The quality gate at the write path: C04 names three certifications, and
    none of them enters the aggregate until an audit clears the record. The
    profile blob still serves.

    This is replay's shape, not the drain's — an archived candidate with no
    audit artifact beside it, which is what every pre-`semantic-audit/v1`
    attempt in the archive looks like. Absent is `not_checked`, never a pass.
    """
    markdown = source("C04")
    dh = seed_case(pg, "C04")
    record = assemble(emit_of("C04"), markdown, document_hash=dh,
                      observed_model=MODEL, at="2026-09-10T00:00:00+00:00")
    started = datetime(2026, 9, 10, 6, 12, 4, tzinfo=UTC)
    attempt = _attempt(
        attempt_key=keys.x_attempt_key(started, dh, 1, 1), document_hash=dh,
        prompt_version=V2_TUPLE[0], schema_version=V2_TUPLE[1], validator_version=V2_TUPLE[2],
        requested_model=MODEL, observed_model=MODEL, record=record,
        started_at="2026-09-10T06:12:04Z", finished_at="2026-09-10T06:12:09Z",
    )
    store.put(attempt.attempt_key, to_bytes(attempt))
    extraction.record_attempt(pg, attempt, None)

    state = settle(pg, store, dh, GLOBS, "2026-09-10T06:13:00Z", bundle=get_bundle("v2"))
    assert state.status == "validated"
    row = pg.execute("SELECT profile FROM extractions").fetchone()
    assert row is not None
    quality = row["profile"]["quality"]
    assert (quality["semantics"], quality["completeness"]) == ("not_checked", "not_checked")
    assert quality["search_eligible"] is False
    assert [m["surface"] for m in row["profile"]["mentions"]] == ["CPA", "ACCA", "ACA"]
    assert mention_rows_in(pg) == []


def test_settle_writes_statement_derived_importance_into_the_aggregate(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The C04 fix where it lands: an audited record's mentions carry the
    importance of the statement each one supports, not of the `credential` area
    they share with a required qualification.

    The record is cleared the way settlement clears one — an archived audit
    artifact beside the candidate attempt, nothing written into the record.
    """
    markdown = source("C04")
    dh = seed_case(pg, "C04")
    record = assemble(emit_of("C04"), markdown, document_hash=dh,
                      observed_model=MODEL, at="2026-09-10T00:00:00+00:00")
    started = datetime(2026, 9, 10, 6, 12, 4, tzinfo=UTC)
    attempt = _attempt(
        attempt_key=keys.x_attempt_key(started, dh, 1, 1), document_hash=dh,
        prompt_version=V2_TUPLE[0], schema_version=V2_TUPLE[1], validator_version=V2_TUPLE[2],
        requested_model=MODEL, observed_model=MODEL, record=record,
        started_at="2026-09-10T06:12:04Z", finished_at="2026-09-10T06:12:09Z",
    )
    store.put(attempt.attempt_key, to_bytes(attempt))
    extraction.record_attempt(pg, attempt, None)
    archive_audit(store, attempt.attempt_key)

    state = settle(pg, store, dh, GLOBS, "2026-09-10T06:13:00Z", bundle=get_bundle("v2"))
    assert state.status == "validated"
    assert (state.semantics, state.completeness) == ("no_findings", "no_findings")
    assert mention_rows_in(pg) == C04_ROWS


# --- the audit phase (spec §4/§5/§6) ---------------------------------------


def test_an_agreeing_cohort_with_a_clean_audit_serves_mention_rows(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """(a) The first record the system has ever been allowed to index: three
    agreeing samples, a full-source audit that found nothing, and the aggregate
    finally carries rows."""
    seed_case(pg, "C04")
    engine = AuditingEngine([result(emit_of("C04"))] * 3, clean_audit)
    summary = run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1
    assert len(engine.audits) == 1  # one audit per document, never per sample

    row = pg.execute("SELECT * FROM extractions").fetchone()
    assert row is not None and row["status"] == "validated" and row["k"] == 3
    quality = row["profile"]["quality"]
    assert (quality["semantics"], quality["completeness"]) == ("no_findings", "no_findings")
    assert quality["sampling"] == "complete" and quality["search_eligible"] is True
    assert mention_rows_in(pg) == C04_ROWS

    # the audited candidate IS the settled one: the runner and settle share the
    # agreement gate, so the artifact can only ever be keyed by settle's choice
    artifacts = audits_in(store)
    assert list(artifacts) == [keys.x_audit_key(row["chosen_attempt"])]
    artifact = artifacts[keys.x_audit_key(row["chosen_attempt"])]
    assert artifact["outcome"] == "ok" and artifact["audit_version"] == AUDIT_VERSION
    assert artifact["attempt_key"] == row["chosen_attempt"]
    assert artifact["blocking"] == 0 and artifact["findings"] == []

    # spec §5: an audit is not an extraction sample and never becomes one
    assert len(attempts_in(store)) == 3
    assert not any(k.startswith(keys.X_ATTEMPTS_PREFIX) for k in artifacts)


def test_an_unsampled_document_is_audited_and_eligible(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """(g) Eligibility must not require sampling: a k=1 document is audited on
    the same path and indexes its mentions."""
    seed_case(pg, "C04")
    engine = AuditingEngine([result(emit_of("C04"))], clean_audit)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1 and len(engine.audits) == 1

    row = pg.execute("SELECT * FROM extractions").fetchone()
    assert row is not None and row["status"] == "validated" and row["k"] == 1
    quality = row["profile"]["quality"]
    assert quality["sampling"] == "not_requested" and quality["search_eligible"] is True
    assert mention_rows_in(pg) == C04_ROWS


def test_a_blocking_finding_leaves_an_agreeing_record_validated_but_ineligible(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """(b) Findings gate the aggregate; they never demote a structurally valid,
    agreeing cohort. The record still serves — with its verdict attached."""
    seed_case(pg, "C04")
    engine = AuditingEngine([result(emit_of("C04"))] * 3, blocking_audit)
    summary = run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1

    row = pg.execute("SELECT * FROM extractions").fetchone()
    assert row is not None and row["status"] == "validated"
    quality = row["profile"]["quality"]
    assert quality["semantics"] == "findings" and quality["completeness"] == "no_findings"
    assert quality["sampling"] == "complete" and quality["search_eligible"] is False
    assert mention_rows_in(pg) == []
    assert [m["surface"] for m in row["profile"]["mentions"]] == ["CPA", "ACCA", "ACA"]

    artifact = next(iter(audits_in(store).values()))
    assert artifact["outcome"] == "ok" and artifact["blocking"] == 1
    finding = artifact["findings"][0]
    # severity and dimension are code-owned; the model emitted neither
    assert finding["code"] == "importance" and finding["severity"] == "blocking"
    assert finding["dimension"] == "semantics"


def test_a_disagreeing_cohort_with_a_clean_audit_is_adjudicated(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """(c) validator/16: a full-source audit of the medoid outranks sampling
    variance, so the document settles — and indexes — instead of parking for a
    human. Two samples call the certification clause a prohibition; the audited
    medoid is fully source-supported, and variance alone is not a defect."""
    seed_case(pg, "C04")
    split = polarity_split_c04()
    engine = AuditingEngine(
        [result(emit_of("C04")), result(split), result(split)], clean_audit
    )
    summary = run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1

    row = pg.execute("SELECT * FROM extractions").fetchone()
    assert row is not None and row["status"] == "validated" and row["k"] == 3
    # the cohort really did disagree: polarity is the zero-tolerance dimension
    assert row["agreement"]["failures"] == ["negation"]
    quality = row["profile"]["quality"]
    assert quality["sampling"] == "adjudicated" and quality["search_eligible"] is True
    assert mention_rows_in(pg) == C04_ROWS  # an adjudicated record is indexed

    # exactly one audit, of the candidate settlement published
    assert list(audits_in(store)) == [keys.x_audit_key(row["chosen_attempt"])]


def test_an_accept_filed_before_the_audit_artifact_survives_it(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """A human disposition is not undone by an artifact that arrives later.

    The probe answers `x_audit_key(candidate)` for the whole event history, so
    an audit archived after an operator ruled re-reads the accept's point in the
    fold as the adjudicated `validated`. Sequence, all of it reachable: a run
    dies inside the audit call (attempts committed, no artifact), the next
    settlement parks the disagreement, the operator accepts it, and a later
    `--only-doc` run audits the same medoid. What must not happen is the accept
    quietly becoming a no-op — `human_review` is a spec §6 dimension, and a
    disposition the fold drops is one the next automated sample may overrule.
    """
    markdown = source("C04")
    dh = seed_case(pg, "C04")
    for slot, emit in enumerate((emit_of("C04"), polarity_split_c04()), start=1):
        record = assemble(emit, markdown, document_hash=dh, observed_model=MODEL,
                          at="2026-09-10T00:00:00+00:00")
        started = datetime(2026, 9, 10, 6, 12, 3 + slot, tzinfo=UTC)
        attempt = _attempt(
            attempt_key=keys.x_attempt_key(started, dh, slot, slot), document_hash=dh,
            prompt_version=V2_TUPLE[0], schema_version=V2_TUPLE[1],
            validator_version=V2_TUPLE[2], sample_slot=slot, attempt_no=slot,
            requested_model=MODEL, observed_model=MODEL, record=record,
            started_at=f"2026-09-10T06:12:0{3 + slot}Z",
            finished_at=f"2026-09-10T06:12:0{4 + slot}Z",
        )
        store.put(attempt.attempt_key, to_bytes(attempt))
        extraction.record_attempt(pg, attempt, None)

    # the audit call died: two committed samples that disagree, no artifact
    parked = settle(pg, store, dh, GLOBS, "2026-09-10T06:13:00Z", bundle=get_bundle("v2"))
    assert parked.status == "needs_review" and parked.sampling == "disagreement"
    assert audits_in(store) == {} and parked.chosen_attempt is not None

    at = datetime(2026, 9, 10, 6, 14, tzinfo=UTC)
    extraction.record_review(
        pg, review_key=keys.x_review_key(at, dh, "accept", 1), document_hash=dh,
        model=MODEL, prompt_version=V2_TUPLE[0], schema_version=V2_TUPLE[1],
        validator_version=V2_TUPLE[2], verb="accept", payload=None, actor="human",
        at=at.isoformat(),
    )
    accepted = settle(pg, store, dh, GLOBS, "2026-09-10T06:14:01Z", bundle=get_bundle("v2"))
    assert accepted.status == "validated" and accepted.human_review == "accepted"

    # the later run audits the same medoid; the artifact now answers the probe
    # for every point of the fold, the accept's included
    archive_audit(store, parked.chosen_attempt)
    audited = settle(pg, store, dh, GLOBS, "2026-09-10T06:15:00Z", bundle=get_bundle("v2"))
    assert audited.status == "validated" and audited.human_review == "accepted"
    assert audited.sampling == "adjudicated"  # the audit carries the cohort now
    assert audited.chosen_attempt == parked.chosen_attempt
    row = pg.execute("SELECT status, profile FROM extractions").fetchone()
    assert row is not None and row["status"] == "validated"
    quality = row["profile"]["quality"]
    assert quality["human_review"] == "accepted" and quality["search_eligible"] is True


def test_a_disagreeing_cohort_with_a_failed_audit_stays_for_review(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """(d) An audit that did not complete is never a pass: the disagreement
    stands and the document waits for a human."""
    seed_case(pg, "C01")
    divergent = divergent_c01()
    engine = AuditingEngine(
        [result(emit_of("C01")), result(divergent), result(divergent)], unparseable_audit
    )
    summary = run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 0

    row = pg.execute("SELECT * FROM extractions").fetchone()
    assert row is not None and row["status"] == "needs_review"
    quality = row["profile"]["quality"]
    assert (quality["semantics"], quality["completeness"]) == ("error", "error")
    assert quality["sampling"] == "disagreement" and quality["search_eligible"] is False
    assert mention_rows_in(pg) == []

    # the audited candidate is the settled one even when the medoid is not slot
    # 1: the runner and settle fold the same events through the same gate
    chosen = {a.attempt_key: a for a in attempts_in(store)}[row["chosen_attempt"]]
    assert chosen.sample_slot in (2, 3)
    assert list(audits_in(store)) == [keys.x_audit_key(row["chosen_attempt"])]

    artifact = next(iter(audits_in(store).values()))
    assert artifact["outcome"] == "audit_error"
    assert artifact["errors"] and "not valid JSON" in artifact["errors"][0]
    assert artifact["raw_response"] == "the candidate looks fine to me"
    assert artifact["attempt_key"] == row["chosen_attempt"]
    assert artifact["candidate_hash"] == chosen.record["extraction"]["candidate_hash"]


def test_an_audit_of_another_candidate_is_an_error_not_a_pass(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """`judge` binds findings to the exact candidate hash; a violation reaches
    the archive as an `audit_error`, with the defect list the judge raised."""
    seed_case(pg, "C04")
    engine = AuditingEngine([result(emit_of("C04"))], foreign_audit)
    run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)

    row = pg.execute("SELECT profile FROM extractions").fetchone()
    assert row is not None
    assert row["profile"]["quality"]["semantics"] == "error"
    assert mention_rows_in(pg) == []
    artifact = next(iter(audits_in(store).values()))
    assert artifact["outcome"] == "audit_error"
    assert any("candidate_hash" in e for e in artifact["errors"])


def test_a_transport_failure_is_retried_once_then_archived_as_an_error(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """One transport retry, mirroring the sample slots; a second failure leaves
    an `audit_error` and settlement still happens."""
    seed_case(pg, "C04")
    answers: list[Any] = [
        EngineTransportError("connection reset"),
        EngineTransportError("connection reset"),
    ]
    engine = AuditingEngine([result(emit_of("C04"))], lambda prompt: answers.pop(0))
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1 and len(engine.audits) == 2  # one call, one retry

    row = pg.execute("SELECT status, profile FROM extractions").fetchone()
    assert row is not None and row["status"] == "validated"
    assert row["profile"]["quality"]["search_eligible"] is False
    artifact = next(iter(audits_in(store).values()))
    assert artifact["outcome"] == "audit_error" and artifact["raw_response"] is None
    assert artifact["errors"] == ["connection reset", "connection reset"]


def test_a_refused_audit_call_does_not_abort_the_run(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """A provider that refuses the audit request is a quality failure, not a run
    failure: the document settles ineligible and the loop moves on. The
    extraction path still raises on the next document, which is where a dead
    provider belongs — one refused audit must not throw away a paid-for
    extraction."""
    seed_case(pg, "C04")
    engine = AuditingEngine(
        [result(emit_of("C04"))], lambda prompt: EngineFatalError("payment required (HTTP 402)")
    )
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1 and len(engine.audits) == 1  # not retried

    row = pg.execute("SELECT status, profile FROM extractions").fetchone()
    assert row is not None and row["status"] == "validated"
    assert row["profile"]["quality"]["search_eligible"] is False
    artifact = next(iter(audits_in(store).values()))
    assert artifact["outcome"] == "audit_error"
    assert artifact["errors"] == ["EngineFatalError: payment required (HTTP 402)"]


def test_a_transport_flake_is_retried_into_a_clean_audit(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    seed_case(pg, "C04")
    answers: list[Any] = [EngineTransportError("connection reset"), None]

    def audit(prompt: str) -> Any:
        answer = answers.pop(0)
        return answer if answer is not None else clean_audit(prompt)

    engine = AuditingEngine([result(emit_of("C04"))], audit)
    run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert len(engine.audits) == 2
    assert mention_rows_in(pg) == C04_ROWS


def test_a_broken_reference_quarantines_after_the_ladder_and_is_never_audited(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """A schema-valid emit citing a block that does not exist is an attribution
    failure, and the binding error is fed into the next attempt verbatim.

    No candidate, no audit: the phase costs nothing on the documents that never
    produced one, and it cannot manufacture a candidate for them either.
    """
    seed_case(pg, "C01")
    broken = copy.deepcopy(emit_of("C01"))
    broken["statements"][0]["evidence"][0]["block_id"] = "b000009"
    engine = AuditingEngine([result(broken)] * 3, clean_audit)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.quarantined == 1 and summary.validated == 0

    row = pg.execute("SELECT status FROM extractions").fetchone()
    assert row is not None and row["status"] == "quarantined"
    archived = attempts_in(store)
    assert [a.outcome for a in archived] == ["attribution_failed"] * 3
    assert archived[-1].ladder_exhausted is True
    assert archived[0].prior_errors == []
    assert any("b000009" in error for error in archived[1].prior_errors)
    assert mention_rows_in(pg) == []
    assert engine.audits == [] and audits_in(store) == {}


def test_the_audit_artifact_is_archived_before_the_extractions_row(
    pg: Conn, store: ArchiveStore, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """(e) [A1]/spec §5: every phase artifact is archived before settlement
    writes. A crash between the two replays the audit from the archive; the
    reverse order would publish a verdict nothing backs."""
    order: list[str] = []
    real_upsert = extraction.upsert_state

    def spy(*args: Any, **kwargs: Any) -> Any:
        order.append("extractions-row")
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(extraction, "upsert_state", spy)

    class Watched:
        def __init__(self, inner: ArchiveStore) -> None:
            self._inner = inner

        def put(self, key: str, data: bytes) -> bool:
            if key.startswith(keys.X_AUDITS_PREFIX):
                order.append("audit-artifact")
            return self._inner.put(key, data)

        def get(self, key: str) -> bytes:
            return self._inner.get(key)

        def exists(self, key: str) -> bool:
            return self._inner.exists(key)

        def list(self, prefix: str, start_after: str | None = None) -> Any:
            return self._inner.list(prefix, start_after)

    seed_case(pg, "C04")
    engine = AuditingEngine([result(emit_of("C04"))], clean_audit)
    run(v2_settings(), pg, Watched(store), engine=engine, max_docs=10, max_usd=5.0)
    assert order == ["audit-artifact", "extractions-row"]


def test_a_cold_catch_up_reproduces_every_decision_with_no_engine_calls(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """(f) The archive is truth: audits are keyed by the candidate attempt, so a
    replay that lost every derived row folds the same verdicts back — eligible,
    adjudicated and parked alike — without asking a model anything."""
    eligible = seed_case(pg, "C04")
    run(v2_settings(), pg, store, only_doc=eligible, max_docs=10, max_usd=5.0,
        engine=AuditingEngine([result(emit_of("C04"))], clean_audit))
    adjudicated = seed_case(pg, "C01")
    divergent = divergent_c01()
    run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, only_doc=adjudicated,
        max_docs=10, max_usd=5.0,
        engine=AuditingEngine(
            [result(emit_of("C01")), result(divergent), result(divergent)], clean_audit
        ))
    parked = seed_case(pg, "C09")
    split = divergent_c09()
    run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, only_doc=parked,
        max_docs=10, max_usd=5.0,
        engine=AuditingEngine(
            [result(emit_of("C09")), result(split), result(split)], unparseable_audit
        ))

    before = settled_rows(pg)
    assert {row[0] for row in before.values()} == {"validated", "needs_review"}
    assert sorted(q["sampling"] for _, _, _, q in before.values()) == [
        "adjudicated", "disagreement", "not_requested",
    ]
    mentions_before = mention_rows_in(pg)
    assert mentions_before  # the eligible document really did index something

    pg.execute("TRUNCATE extraction_attempts, extraction_reviews, extractions, profile_mentions")
    pg.commit()
    silent = AuditingEngine([], clean_audit)
    summary = run(v2_settings(), pg, store, engine=silent, max_docs=0, max_usd=0.0)
    assert silent.calls == [] and silent.audits == []
    assert summary.replayed > 0
    assert settled_rows(pg) == before
    assert mention_rows_in(pg) == mentions_before
