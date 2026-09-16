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
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.hashing import sha256_hex
from jobhunter.l2 import runner
from jobhunter.l2.attempts import from_bytes, to_bytes
from jobhunter.l2.bundles import get_bundle
from jobhunter.l2.engines import (
    EngineFatalError,
    EngineResult,
    EngineThrottled,
    EngineTransportError,
)
from jobhunter.l2.runner import (
    ExtractSummary,
    _Journal,
    _reaudit_pass,
    _reaudit_queue,
    _Session,
    run,
    settle,
    unexplained_deletions,
)
from jobhunter.l2.state import globs_to_regex
from jobhunter.l2.v2.assemble import assemble
from jobhunter.l2.v2.audit import AUDIT_VERSION
from jobhunter.store import extraction
from jobhunter.timeutil import iso, utcnow_precise
from tests.l2.test_attempts import _attempt
from tests.l2.test_runner import FakeEngine, _seed_doc, _settings, store  # noqa: F401

Conn = psycopg.Connection[dict[str, Any]]

CASES = pathlib.Path(__file__).parent / "v2" / "cases"
GLOBS = ("z-ai/*",)
MODEL = "z-ai/glm-5.2:free"
V2_TUPLE = ("demand-profile/v10", "2", "19")
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


def repairs_in(archive: ArchiveStore) -> dict[str, dict[str, Any]]:
    """Every archived repair artifact, by key: gzipped JSON at `x_repair_key`."""
    return {
        key: json.loads(gzip.decompress(archive.get(key)))
        for key in sorted(archive.list(keys.X_REPAIRS_PREFIX))
    }


def archive_audit(
    archive: ArchiveStore,
    attempt_key: str,
    *,
    semantics: str = "no_findings",
    completeness: str = "no_findings",
    blocking: int = 0,
    outcome: str = "ok",
    audit_pass: int = 1,
    candidate_hash: str = "",
) -> None:
    """One audit artifact, written by hand — the read half of the contract.

    Settlement probes `x_audit_key(candidate)` and needs exactly three values
    out of it; writing them here (rather than through the runner) is what pins
    the format `settle` reads against the format the audit phase writes.
    `audit_pass` picks which of the candidate's two passes is being written,
    so a test can spend a budget without paying for two drains.
    """
    artifact = {
        "audit_version": AUDIT_VERSION, "outcome": outcome, "attempt_key": attempt_key,
        "audit_pass": audit_pass, "candidate_hash": candidate_hash,
        "semantics": semantics, "completeness": completeness, "blocking": blocking,
        "findings": [], "unresolved": [],
    }
    archive.put(
        keys.x_audit_key(attempt_key) if audit_pass == 1 else retry_key(attempt_key),
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


def row_of(pg: Conn) -> dict[str, Any]:
    row = pg.execute("SELECT * FROM extractions").fetchone()
    assert row is not None
    return row


def retry_key(attempt_key: str) -> str:
    """Where the ONE re-audit of a candidate lands: beside the failed artifact,
    in the same namespace, never over it (write-once)."""
    return keys.x_audit_key(attempt_key).removesuffix(".json.gz") + "-p2.json.gz"


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


def polarity_audit(prompt: str) -> str:
    """One `polarity_subject` finding — the `negation` gate's own dimension.

    Rule 3 of the validator/18 policy: whatever object it names, a finding whose
    code restates the failed gate lands ON the dispute, because a cohort that
    split over polarity and an auditor reporting a wrong polarity are the same
    disagreement.
    """
    statement = candidate_in(prompt)["statements"][1]["id"]
    return json.dumps({
        "candidate_hash": candidate_hash_in(prompt),
        "findings": [{
            "code": "polarity_subject", "targets": [statement], "evidence": None,
            "explanation": "the clause names who the certification applies to, not a prohibition",
        }],
        "unresolved": [],
    })


def scripted(*answers: Any) -> Any:
    """Answer each call with the next script, and every call after the last with
    the last — so "audit, then re-audit the repaired candidate" reads in order."""
    remaining = list(answers)

    def answer(prompt: str) -> Any:
        return (remaining.pop(0) if len(remaining) > 1 else remaining[0])(prompt)

    return answer


_BASE_HASH_LINE = re.compile(r"BASE CANDIDATE HASH: ([0-9a-f]{64})")


def base_hash_in(prompt: str) -> str:
    match = _BASE_HASH_LINE.search(prompt)
    assert match is not None, "the repair prompt carries the base candidate hash"
    return match.group(1)


def _polarity_op(prompt: str) -> dict[str, Any]:
    """One typed operation over the prompt's own candidate: the certification
    statement, read as a prohibition, put back the way the source words it.

    Everything it needs is printed in the prompt — the object to replace and the
    `object_hash` an operation must echo to be allowed to touch it."""
    shown = candidate_in(prompt)["statements"][1]
    fixed = {k: v for k, v in shown.items() if k != "object_hash"}
    fixed["polarity"] = "positive"
    fixed["polarity_evidence"] = None
    return {
        "op": "replace", "kind": "statement", "target_id": shown["id"],
        "object": fixed, "old_object_hash": shown["object_hash"], "finding_id": "f1",
        "evidence": [{"block_id": shown["evidence"][0]["block_id"],
                      "text": None, "occurrence": None}],
        "reason": "the source calls the certification preferred, it does not forbid it",
    }


def polarity_repair(prompt: str) -> str:
    return json.dumps(
        {"base_candidate_hash": base_hash_in(prompt), "operations": [_polarity_op(prompt)]}
    )


def stale_repair(prompt: str) -> str:
    """A repair written against a candidate this is not. `apply` refuses it, and
    the base candidate must settle exactly as it would have without the round."""
    return json.dumps({"base_candidate_hash": "f" * 64, "operations": [_polarity_op(prompt)]})


def unparseable_audit(prompt: str) -> str:
    return "the candidate looks fine to me"  # not JSON: an audit error, never a pass


def refused_audit(prompt: str) -> str:
    """An audit `judge` must refuse: a finding against an id no candidate has.

    Through semantic-audit/v2 this was an audit echoing somebody else's
    candidate hash. v3 removed the echo — which candidate an audit belongs to
    is the caller's record and hash, never a string the model retypes — so a
    fabricated target is what a refused verdict looks like now.
    """
    return json.dumps({
        "findings": [{
            "code": "importance", "targets": ["s_no_such_id"], "evidence": None,
            "explanation": "the posting words this as preferred, not required",
        }],
        "unresolved": [],
    })


class AuditingEngine(FakeEngine):
    """`FakeEngine` plus a scripted auditor and a scripted repairer.

    The three phases are told apart by the line each template opens with — the
    repair prompt's `BASE CANDIDATE HASH:`, then the audit prompt's `CANDIDATE
    HASH:`, then everything else is an extraction — so a test never has to know
    where in the call order a phase lands, which is the point, since where it
    lands is what these tests are about. Each script answers with the raw
    response text, or raises the exception it returns.

    No repairer is the default, and it is not "the repair round did nothing":
    it is a call that never reached one (`EngineTransportError`), which archives
    nothing and leaves the candidate's one round unspent — so every test written
    before the phase existed still describes the same archive.
    """

    def __init__(self, script: list[Any], audit: Any, repair: Any = None) -> None:
        super().__init__(script)
        self._audit = audit
        self._repair = repair
        self.audits: list[str] = []
        self.repairs: list[str] = []

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        if "BASE CANDIDATE HASH:" in prompt:
            self.repairs.append(prompt)
            if self._repair is None:
                raise EngineTransportError("no repairer scripted")
            answer = self._repair(prompt)
            if isinstance(answer, Exception):
                raise answer
            return EngineResult(answer, MODEL, 60, 12, 0.0)
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
    assert store.exists(keys.x_prompt_key(V2_TUPLE[0]))
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
    row = row_of(pg)
    quality = row["profile"]["quality"]
    assert (quality["semantics"], quality["completeness"]) == ("not_checked", "not_checked")
    assert quality["search_eligible"] is False
    # a phase that never ran is not a phase that failed — nothing to repair
    # against — but both passes are still there to take, and the row says so:
    # this is how a record the drain could not audit gets its audit later
    assert row["flags"] == {"audit": "not_checked", "audit_retry": True}
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


def test_an_audit_the_judge_refuses_is_an_error_not_a_pass(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """`judge` binds every finding to the candidate's own ids; a violation
    reaches the archive as an `audit_error`, with the defect list the judge
    raised — never a pass, and never a demotion of the record itself."""
    seed_case(pg, "C04")
    engine = AuditingEngine([result(emit_of("C04"))], refused_audit)
    run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)

    row = pg.execute("SELECT profile FROM extractions").fetchone()
    assert row is not None
    assert row["profile"]["quality"]["semantics"] == "error"
    assert mention_rows_in(pg) == []
    artifact = next(iter(audits_in(store).values()))
    assert artifact["outcome"] == "audit_error"
    assert any("s_no_such_id" in e for e in artifact["errors"])


def test_a_transport_failure_leaves_the_candidates_pass_unspent(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """One transport retry, mirroring the sample slots — and then nothing.

    A call that never reached the auditor produced no verdict, so it archives
    no artifact and spends none of the candidate's two passes: the archive is
    write-once, and an `audit_error` written for a dead socket would be a
    verdict the retry could never replace. The document settles ineligible and
    the next run audits it for the first time.
    """
    seed_case(pg, "C04")
    answers: list[Any] = [
        EngineTransportError("connection reset"),
        EngineTransportError("connection reset"),
    ]
    engine = AuditingEngine([result(emit_of("C04"))], lambda prompt: answers.pop(0))
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1 and len(engine.audits) == 2  # one call, one retry

    row = row_of(pg)
    assert row["status"] == "validated"
    assert row["profile"]["quality"]["semantics"] == "not_checked"
    assert row["profile"]["quality"]["search_eligible"] is False
    assert audits_in(store) == {}
    assert row["flags"] == {"audit": "not_checked", "audit_retry": True}

    second = AuditingEngine([], clean_audit)
    assert run(v2_settings(), pg, store, engine=second, max_docs=10, max_usd=5.0).reaudited == 1
    assert set(audits_in(store)) == {keys.x_audit_key(row["chosen_attempt"])}  # pass 1, not p2
    assert mention_rows_in(pg) == C04_ROWS


def test_a_refused_audit_call_does_not_abort_the_run(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """A provider that refuses the audit request is a quality failure, not a run
    failure: the document settles ineligible and the loop moves on. The
    extraction path still raises on the next document, which is where a dead
    provider belongs — one refused audit must not throw away a paid-for
    extraction. It reached no auditor either, so it archives nothing and the
    candidate keeps both passes."""
    seed_case(pg, "C04")
    engine = AuditingEngine(
        [result(emit_of("C04"))], lambda prompt: EngineFatalError("payment required (HTTP 402)")
    )
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1 and len(engine.audits) == 1  # not retried

    row = row_of(pg)
    assert row["status"] == "validated"
    assert row["profile"]["quality"]["search_eligible"] is False
    assert audits_in(store) == {}
    assert row["flags"] == {"audit": "not_checked", "audit_retry": True}


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


# --- the audit retry pass and the repair trigger (validator/18) -------------


def test_an_audit_error_is_re_audited_on_the_next_run_and_then_settles(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """An `audit_error` is not this candidate's last word.

    The document settles — nothing waits on a quality phase — but ineligible,
    and the NEXT run re-audits the same candidate: no extraction (the engine
    script is empty, so an extraction call would raise), one audit call, a
    second artifact beside the failed one. A completed audit ends the passes:
    the third run asks the model nothing.
    """
    seed_case(pg, "C04")
    first = AuditingEngine([result(emit_of("C04"))], unparseable_audit)
    run(v2_settings(), pg, store, engine=first, max_docs=10, max_usd=5.0)

    errored = row_of(pg)
    assert errored["status"] == "validated"
    assert errored["profile"]["quality"]["semantics"] == "error"
    assert errored["profile"]["quality"]["search_eligible"] is False
    assert mention_rows_in(pg) == []
    candidate = errored["chosen_attempt"]
    assert set(audits_in(store)) == {keys.x_audit_key(candidate)}

    second = AuditingEngine([], clean_audit)
    summary = run(v2_settings(), pg, store, engine=second, max_docs=10, max_usd=5.0)
    assert second.calls == [] and len(second.audits) == 1  # one re-audit, no extraction
    assert summary.reaudited == 1 and summary.repair_triggered == 0

    artifacts = audits_in(store)
    assert set(artifacts) == {keys.x_audit_key(candidate), retry_key(candidate)}
    assert artifacts[keys.x_audit_key(candidate)]["outcome"] == "audit_error"
    retried = artifacts[retry_key(candidate)]
    assert retried["outcome"] == "ok" and retried["attempt_key"] == candidate
    assert retried["audit_version"] == AUDIT_VERSION and retried["blocking"] == 0

    settled = row_of(pg)
    assert settled["status"] == "validated" and settled["chosen_attempt"] == candidate
    quality = settled["profile"]["quality"]
    assert (quality["semantics"], quality["completeness"]) == ("no_findings", "no_findings")
    assert quality["search_eligible"] is True
    assert mention_rows_in(pg) == C04_ROWS

    third = AuditingEngine([], blocking_audit)
    run(v2_settings(), pg, store, engine=third, max_docs=10, max_usd=5.0)
    assert third.audits == []  # a completed audit is written once, never re-run
    assert set(audits_in(store)) == {keys.x_audit_key(candidate), retry_key(candidate)}
    assert mention_rows_in(pg) == C04_ROWS


def test_two_audit_errors_end_the_candidates_budget(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Exactly one re-audit. A second failure is where the budget stops: the
    record stays ineligible, its row stops claiming a retry it cannot take, and
    every later run asks the model nothing."""
    seed_case(pg, "C04")
    first = AuditingEngine([result(emit_of("C04"))], unparseable_audit)
    run(v2_settings(), pg, store, engine=first, max_docs=10, max_usd=5.0)
    errored = row_of(pg)
    assert errored["status"] == "validated"
    assert errored["flags"] == {"audit": "error", "audit_retry": True}
    candidate = errored["chosen_attempt"]

    second = AuditingEngine([], unparseable_audit)
    summary = run(v2_settings(), pg, store, engine=second, max_docs=10, max_usd=5.0)
    assert len(second.audits) == 1 and summary.reaudited == 1
    artifacts = audits_in(store)
    assert set(artifacts) == {keys.x_audit_key(candidate), retry_key(candidate)}
    assert {a["outcome"] for a in artifacts.values()} == {"audit_error"}

    third = AuditingEngine([], clean_audit)
    summary = run(v2_settings(), pg, store, engine=third, max_docs=10, max_usd=5.0)
    assert third.audits == [] and summary.reaudited == 0
    assert set(audits_in(store)) == {keys.x_audit_key(candidate), retry_key(candidate)}

    still = row_of(pg)
    assert still["status"] == "validated" and still["chosen_attempt"] == candidate
    quality = still["profile"]["quality"]
    assert (quality["semantics"], quality["completeness"]) == ("error", "error")
    assert quality["search_eligible"] is False
    # an error is never a repairable finding list: nothing to repair against,
    # and no `audit_retry` — the budget is gone, so the queue must stop
    # offering this row a slot it cannot use
    assert still["flags"] == {"audit": "error"}
    assert mention_rows_in(pg) == []


def test_a_parked_cohort_is_never_promoted_by_an_automated_re_audit(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Spec §6: once a terminal needs-review state has been published, no
    automated audit may promote it.

    The cohort disagrees and its audit errors, so the document is published for
    review — a human may already be triaging it. The re-audit pass leaves it
    alone: a second automated audit would decide the very disagreement the
    review queue is showing a person, and a clean one would move the record
    into the aggregates behind their back. The exit is a human `retry`, which
    starts a fresh cohort and a fresh audit.
    """
    seed_case(pg, "C04")
    split = polarity_split_c04()
    first = AuditingEngine(
        [result(emit_of("C04")), result(split), result(split)], unparseable_audit
    )
    run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=first,
        max_docs=10, max_usd=5.0)
    parked = row_of(pg)
    assert parked["status"] == "needs_review"
    assert parked["profile"]["quality"]["sampling"] == "disagreement"
    candidate = parked["chosen_attempt"]

    second = AuditingEngine([], clean_audit)
    summary = run(v2_settings(), pg, store, engine=second, max_docs=10, max_usd=5.0)
    assert second.audits == [] and summary.reaudited == 0
    assert set(audits_in(store)) == {keys.x_audit_key(candidate)}

    still = row_of(pg)
    assert still["status"] == "needs_review" and still["chosen_attempt"] == candidate
    quality = still["profile"]["quality"]
    assert (quality["semantics"], quality["sampling"]) == ("error", "disagreement")
    assert quality["search_eligible"] is False
    assert mention_rows_in(pg) == []


def test_a_throttled_audit_spends_no_pass_and_stops_the_run(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """A 429 is the provider asking for silence, not a verdict on this
    candidate: nothing is archived, the pass is still there to take, and the
    run stops exactly as a throttled extraction stops it — one rate-limit storm
    must not spend the retry of every document in the batch."""
    seed_case(pg, "C04")
    engine = AuditingEngine(
        [result(emit_of("C04"))], lambda prompt: EngineThrottled("429 rate limited")
    )
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.throttled is True
    assert len(engine.audits) == 1  # a 429 is not retried in the same breath
    assert audits_in(store) == {}

    row = row_of(pg)
    assert row["status"] == "validated"  # the paid-for extraction is not thrown away
    assert row["profile"]["quality"]["search_eligible"] is False
    assert row["flags"] == {"audit": "not_checked", "audit_retry": True}

    second = AuditingEngine([], clean_audit)
    summary = run(v2_settings(), pg, store, engine=second, max_docs=10, max_usd=5.0)
    assert len(second.audits) == 1 and summary.reaudited == 1
    assert set(audits_in(store)) == {keys.x_audit_key(row["chosen_attempt"])}
    assert mention_rows_in(pg) == C04_ROWS


def test_a_throttled_re_audit_stops_the_pass_with_the_budget_intact(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The same rule inside the re-audit pass, which is where it costs most.

    The pass walks documents that have one pass left; a provider throttling
    every call would otherwise write an `audit_error` for each of them under a
    write-once key and leave the whole window unretryable. It stops on the
    first 429 instead, and the next healthy run takes the pass that was saved.
    """
    seed_case(pg, "C04")
    run(v2_settings(), pg, store, engine=AuditingEngine([result(emit_of("C04"))],
                                                        unparseable_audit),
        max_docs=10, max_usd=5.0)
    candidate = row_of(pg)["chosen_attempt"]

    throttled = AuditingEngine([], lambda prompt: EngineThrottled("429 rate limited"))
    summary = run(v2_settings(), pg, store, engine=throttled, max_docs=10, max_usd=5.0)
    assert summary.throttled is True and summary.reaudited == 0
    assert len(throttled.audits) == 1
    assert set(audits_in(store)) == {keys.x_audit_key(candidate)}  # no p2 artifact
    assert row_of(pg)["flags"] == {"audit": "error", "audit_retry": True}

    healthy = AuditingEngine([], clean_audit)
    summary = run(v2_settings(), pg, store, engine=healthy, max_docs=10, max_usd=5.0)
    assert summary.reaudited == 1
    assert set(audits_in(store)) == {keys.x_audit_key(candidate), retry_key(candidate)}
    assert row_of(pg)["profile"]["quality"]["search_eligible"] is True
    assert mention_rows_in(pg) == C04_ROWS


def test_the_re_audit_pass_breaks_on_a_provider_that_stops_answering(
    pg: Conn, store: ArchiveStore, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The breaker the drain has, for the same reason: a provider answering
    nothing is not a queue of documents to work through. Consecutive audits
    that reach no auditor end the run — with every one of their passes still
    unspent, which is the whole point of not archiving a verdict for them."""
    monkeypatch.setattr(runner, "BREAKER_LIMIT", 2)
    for case in ("C01", "C09"):
        run(v2_settings(), pg, store, only_doc=seed_case(pg, case),
            engine=AuditingEngine([result(emit_of(case))], unparseable_audit),
            max_docs=10, max_usd=5.0)
    before = set(audits_in(store))
    assert len(before) == 2

    dead = AuditingEngine([], lambda prompt: EngineTransportError("connection reset"))
    summary = run(v2_settings(), pg, store, engine=dead, max_docs=10, max_usd=5.0)
    assert summary.aborted == "audit_unreachable" and summary.reaudited == 0
    assert len(dead.audits) == 4  # two documents, one transport retry each
    assert set(audits_in(store)) == before
    assert [r["flags"] for r in pg.execute("SELECT flags FROM extractions")] == [
        {"audit": "error", "audit_retry": True}
    ] * 2


def test_a_spent_candidate_never_fills_the_re_audit_window(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Documents whose two passes are gone must leave the window.

    Their `updated_at` froze on the fold that recorded the second error, so
    they are the oldest rows the pass can see and they sort ahead of every
    newer one: a `LIMIT max_docs` window that still matched them would fill up
    with documents nothing can do anything for, and the pass would stop
    re-auditing corpus-wide. The row itself has to say the budget is gone — a
    row that only says `error` cannot tell "owed" from "spent".
    """
    spent: list[str] = []
    for case in ("C01", "C09"):
        dh = seed_case(pg, case)
        run(v2_settings(), pg, store, only_doc=dh,
            engine=AuditingEngine([result(emit_of(case))], unparseable_audit),
            max_docs=10, max_usd=5.0)
        candidate = pg.execute(
            "SELECT chosen_attempt FROM extractions WHERE document_hash = %s", (dh,)
        ).fetchone()
        assert candidate is not None
        archive_audit(store, candidate["chosen_attempt"], audit_pass=2, outcome="audit_error",
                      semantics="error", completeness="error")
        spent.append(dh)

    fresh = seed_case(pg, "C04")
    run(v2_settings(), pg, store, only_doc=fresh,
        engine=AuditingEngine([result(emit_of("C04"))], unparseable_audit),
        max_docs=10, max_usd=5.0)
    owed = pg.execute(
        "SELECT chosen_attempt FROM extractions WHERE document_hash = %s", (fresh,)
    ).fetchone()
    assert owed is not None
    fresh_candidate = owed["chosen_attempt"]

    # the spent pair folded first, so they are the oldest rows in the table
    older = iso(datetime.now(UTC) - timedelta(days=1))
    for dh in spent:
        settle(pg, store, dh, GLOBS, older, bundle=get_bundle("v2"))
    pg.commit()
    rows = {r["document_hash"]: r["flags"] for r in pg.execute("SELECT * FROM extractions")}
    assert [rows[dh] for dh in spent] == [{"audit": "error"}] * 2
    assert rows[fresh] == {"audit": "error", "audit_retry": True}

    window = _reaudit_queue(
        pg, prompt_version=V2_TUPLE[0], schema_version=V2_TUPLE[1],
        validator_version=V2_TUPLE[2], model_regex=globs_to_regex(GLOBS),
        only_doc=None, limit=2,
    )
    assert [dh for dh, _ in window] == [fresh]

    # and a row whose flag outlived its budget — a run that died between the
    # second artifact and its fold — is re-folded rather than allowed to eat a
    # slot. The pass is driven directly here: the catch-up scan that opens a
    # run would heal the row first, and this is the branch that has to.
    pg.execute(
        "UPDATE extractions SET flags = %s WHERE document_hash = %s",
        (json.dumps({"audit": "error", "audit_retry": True}), spent[0]),
    )
    pg.commit()
    healthy = AuditingEngine([], clean_audit)
    journal = _Journal(store, GLOBS, utcnow_precise)
    summary = ExtractSummary(run_id="x-test")
    assert _reaudit_pass(
        v2_settings(), _Session(pg, None, journal), journal, healthy, summary,
        utcnow_precise, get_bundle("v2"), None, 2, 5.0,
    ) is None
    assert summary.reaudited == 1 and len(healthy.audits) == 1
    assert retry_key(fresh_candidate) in audits_in(store)
    healed = pg.execute(
        "SELECT flags FROM extractions WHERE document_hash = %s", (spent[0],)
    ).fetchone()
    assert healed is not None and healed["flags"] == {"audit": "error"}
    assert mention_rows_in(pg) == C04_ROWS


def test_a_blocking_finding_on_the_dispute_parks_the_cohort_and_asks_for_repair(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """validator/18, the touching half: two samples read the certification
    clause as a prohibition, and the audit reports a polarity defect — the
    `negation` gate's own dimension. The audit lands ON what the samples split
    over, so nothing is adjudicated and the repair phase is asked for."""
    seed_case(pg, "C04")
    split = polarity_split_c04()
    engine = AuditingEngine(
        [result(emit_of("C04")), result(split), result(split)], polarity_audit
    )
    summary = run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 0 and summary.repair_triggered == 1

    row = row_of(pg)
    assert row["status"] == "needs_review" and row["agreement"]["failures"] == ["negation"]
    quality = row["profile"]["quality"]
    assert quality["sampling"] == "disagreement" and quality["search_eligible"] is False
    assert row["flags"] == {"audit": "ok", "repair": "dispute"}
    assert mention_rows_in(pg) == []


def test_a_blocking_finding_off_the_dispute_adjudicates_and_asks_for_repair(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """validator/18, the scoped half, over the same cohort: an `importance`
    finding is not the `negation` gate's dimension and names no disputed id, so
    the disagreement is adjudicated — and the finding goes on gating
    eligibility, which is the other repair trigger."""
    seed_case(pg, "C04")
    split = polarity_split_c04()
    engine = AuditingEngine(
        [result(emit_of("C04")), result(split), result(split)], blocking_audit
    )
    summary = run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1 and summary.repair_triggered == 1

    row = row_of(pg)
    assert row["status"] == "validated" and row["agreement"]["failures"] == ["negation"]
    quality = row["profile"]["quality"]
    assert quality["sampling"] == "adjudicated" and quality["semantics"] == "findings"
    assert quality["search_eligible"] is False  # the finding still gates the aggregate
    assert row["flags"] == {"audit": "ok", "repair": "eligibility"}
    assert mention_rows_in(pg) == []


def test_a_human_ruling_takes_the_document_out_of_the_repair_queue(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Spec §6: the bounded repair loop is not a backdoor around the human-only
    promotion rule. A record a human has ruled on is never repaired
    automatically, whatever its findings say."""
    dh = seed_case(pg, "C04")
    engine = AuditingEngine([result(emit_of("C04"))], blocking_audit)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.repair_triggered == 1
    assert row_of(pg)["flags"] == {"audit": "ok", "repair": "eligibility"}

    # after the drain's own attempts, the way `extract review flag` stamps one
    at = datetime.now(UTC) + timedelta(minutes=1)
    extraction.record_review(
        pg, review_key=keys.x_review_key(at, dh, "flag", 1), document_hash=dh,
        model=MODEL, prompt_version=V2_TUPLE[0], schema_version=V2_TUPLE[1],
        validator_version=V2_TUPLE[2], verb="flag", payload=None, actor="human",
        at=at.isoformat(),
    )
    state = settle(pg, store, dh, GLOBS, iso(at), bundle=get_bundle("v2"))
    assert state.status == "needs_review"
    assert row_of(pg)["flags"] == {"audit": "ok"}

    later = AuditingEngine([], clean_audit)
    summary = run(v2_settings(), pg, store, engine=later, max_docs=10, max_usd=5.0)
    assert later.audits == [] and summary.repair_triggered == 0
    assert row_of(pg)["flags"] == {"audit": "ok"}


# --- the repair round (spec §4 Repair, §5, validator/18) --------------------


def repaired_audit_key(attempt_key: str) -> str:
    """Where the ONE audit of a repaired candidate lands: beside its base
    candidate's audits, never over them (write-once)."""
    return keys.x_audit_key(attempt_key).removesuffix(".json.gz") + "-r1.json.gz"


def test_an_eligibility_repair_puts_the_repaired_candidate_in_the_aggregate(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Spec §4/§5.5 end to end: the candidate reads "preferred but not required"
    as a prohibition, the audit says so with a blocking polarity finding, and the
    ONE repair round fixes it inside the same pre-settlement window. What settles
    is the repaired candidate — eligible, carrying the parent link — while the
    base attempt stays in the archive exactly as it was extracted.
    """
    seed_case(pg, "C04")
    engine = AuditingEngine([result(polarity_split_c04())],
                            scripted(polarity_audit, clean_audit), repair=polarity_repair)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1
    assert summary.repair_triggered == 1 and summary.repaired == 1
    assert len(engine.repairs) == 1  # one round, whatever the verdict
    assert len(engine.audits) == 2  # the base candidate's, then the repaired one's

    row = row_of(pg)
    assert row["status"] == "validated" and row["k"] == 1
    quality = row["profile"]["quality"]
    assert (quality["semantics"], quality["completeness"]) == ("no_findings", "no_findings")
    assert quality["search_eligible"] is True
    assert mention_rows_in(pg) == C04_ROWS
    assert all(s["polarity"] == "positive" for s in row["profile"]["statements"])
    # nothing left to come back for: the round is spent and the findings are gone
    assert row["flags"] == {"audit": "ok"}

    # spec §5: a repair is not an extraction sample and never becomes one
    attempts = attempts_in(store)
    assert len(attempts) == 1 and row["chosen_attempt"] == attempts[0].attempt_key
    base = attempts[0].record
    assert any(s["polarity"] == "negative" for s in base["statements"])

    artifacts = repairs_in(store)
    assert list(artifacts) == [keys.x_repair_key(attempts[0].attempt_key)]
    artifact = artifacts[keys.x_repair_key(attempts[0].attempt_key)]
    assert artifact["outcome"] == "repaired" and artifact["reason"] == runner.REPAIR_ELIGIBILITY
    base_hash = base["extraction"]["candidate_hash"]
    assert artifact["candidate_hash"] == base_hash
    repaired = artifact["record"]
    assert repaired["extraction"]["parent_candidate_hash"] == base_hash
    assert repaired["extraction"]["candidate_hash"] not in ("", base_hash)
    assert row["profile"]["statements"] == repaired["statements"]

    # the repaired candidate has an audit of its own, beside the base's
    assert set(audits_in(store)) == {
        keys.x_audit_key(attempts[0].attempt_key),
        repaired_audit_key(attempts[0].attempt_key),
    }
    repaired_audit = audits_in(store)[repaired_audit_key(attempts[0].attempt_key)]
    assert repaired_audit["candidate_hash"] == repaired["extraction"]["candidate_hash"]
    assert repaired_audit["blocking"] == 0


def test_a_repair_on_the_dispute_adjudicates_the_cohort_it_was_asked_to_fix(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The plan's headline case: two samples read the certification clause as a
    prohibition and the audit lands ON that dispute, so nothing is adjudicated.
    The repair round fixes the medoid, its re-audit is clean, and the cohort
    settles on the repaired candidate instead of parking for a human."""
    seed_case(pg, "C04")
    split = polarity_split_c04()
    engine = AuditingEngine([result(emit_of("C04")), result(split), result(split)],
                            scripted(polarity_audit, clean_audit), repair=polarity_repair)
    summary = run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.repair_triggered == 1 and summary.repaired == 1 and summary.validated == 1

    row = row_of(pg)
    assert row["status"] == "validated" and row["k"] == 3
    assert row["agreement"]["failures"] == ["negation"]  # the cohort really did split
    quality = row["profile"]["quality"]
    assert quality["sampling"] == "adjudicated" and quality["search_eligible"] is True
    assert mention_rows_in(pg) == C04_ROWS
    assert len(attempts_in(store)) == 3  # the repaired candidate is not a fourth sample
    assert row["flags"] == {"audit": "ok"}


def test_a_failed_repair_settles_the_base_candidate(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Spec §4: "A failed repair does not erase the base candidate."

    The repairer answers with operations written against a candidate this is
    not; `apply` refuses the whole patch, the judge's defect list is archived,
    and the document settles exactly as it would have with no repair round at
    all — parked, ineligible, and no longer asking for a round it has spent.
    """
    seed_case(pg, "C04")
    split = polarity_split_c04()
    engine = AuditingEngine([result(emit_of("C04")), result(split), result(split)],
                            polarity_audit, repair=stale_repair)
    summary = run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.repair_triggered == 1 and summary.repaired == 0 and summary.validated == 0
    assert len(engine.repairs) == 1
    assert len(engine.audits) == 1  # no candidate was built, so nothing to re-audit

    row = row_of(pg)
    assert row["status"] == "needs_review"
    quality = row["profile"]["quality"]
    assert quality["sampling"] == "disagreement" and quality["search_eligible"] is False
    assert mention_rows_in(pg) == []
    assert row["flags"] == {"audit": "ok"}  # the round is spent; stop queueing it

    artifact = next(iter(repairs_in(store).values()))
    assert artifact["outcome"] == "repair_error" and artifact["record"] is None
    assert any("base_candidate_hash" in e for e in artifact["errors"])
    assert set(audits_in(store)) == {keys.x_audit_key(row["chosen_attempt"])}


def test_the_repair_round_is_offered_once_per_candidate(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Spec §5: "one semantic repair round and one subsequent audit" is the whole
    budget. The archive is write-once, so the round a candidate has taken is the
    round it took — a second pass over the same candidate asks the model nothing,
    whatever the first one produced."""
    dh = seed_case(pg, "C04")
    engine = AuditingEngine([result(polarity_split_c04())], polarity_audit,
                            repair=stale_repair)
    run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    before = repairs_in(store)
    assert len(before) == 1 and next(iter(before.values()))["outcome"] == "repair_error"
    candidate = row_of(pg)["chosen_attempt"]

    willing = AuditingEngine([], clean_audit, repair=polarity_repair)
    journal = _Journal(store, GLOBS, utcnow_precise)
    summary = ExtractSummary(run_id="x-test")
    trigger = runner.RepairTrigger(dh, candidate, runner.REPAIR_ELIGIBILITY)
    assert runner._repair_candidate(
        v2_settings(), _Session(pg, None, journal), journal, willing, dh, source("C04"),
        summary, utcnow_precise, get_bundle("v2"), None, trigger,
    ) is None
    assert willing.repairs == [] and willing.audits == [] and summary.repaired == 0
    assert repairs_in(store) == before  # write-once: never rewritten, never re-run


def test_a_repaired_candidate_whose_audit_never_reached_is_audited_next_run(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Spec §5: the round is "one semantic repair round and one subsequent
    audit", and the archive has to show both halves.

    Here the first half lands — a whole repaired candidate, archived — and the
    audit that decides whether it settles never reaches the auditor. Nothing is
    archived for it, so the round is UNFINISHED, not refused: the base settles
    meanwhile (a candidate no audit has judged is never published), the row says
    an audit is still owed, and the next healthy run takes exactly that audit —
    no extraction, no second repair round. Without the owed flag the repaired
    candidate would be orphaned for the life of the tuple: the write-once repair
    key says the round is spent, and the base's own audit completed.
    """
    seed_case(pg, "C04")
    engine = AuditingEngine(
        [result(polarity_split_c04())],
        scripted(polarity_audit, lambda prompt: EngineTransportError("down")),
        repair=polarity_repair,
    )
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.repair_triggered == 1 and summary.repaired == 0
    candidate = row_of(pg)["chosen_attempt"]
    artifact = repairs_in(store)[keys.x_repair_key(candidate)]
    assert artifact["outcome"] == "repaired" and artifact["record"] is not None
    assert set(audits_in(store)) == {keys.x_audit_key(candidate)}  # no verdict on it

    owed = row_of(pg)
    assert owed["status"] == "validated"  # the base candidate, exactly as audited
    assert owed["profile"]["quality"]["search_eligible"] is False
    assert mention_rows_in(pg) == []
    assert owed["flags"] == {"audit": "ok", "audit_retry": True}

    second = AuditingEngine([], clean_audit, repair=polarity_repair)
    summary = run(v2_settings(), pg, store, engine=second, max_docs=10, max_usd=5.0)
    assert second.calls == [] and second.repairs == []  # one round, and it is taken
    assert len(second.audits) == 1
    assert summary.reaudited == 1 and summary.repaired == 1 and summary.repair_triggered == 0
    assert set(audits_in(store)) == {
        keys.x_audit_key(candidate), repaired_audit_key(candidate)
    }

    settled = row_of(pg)
    assert settled["status"] == "validated" and settled["chosen_attempt"] == candidate
    assert settled["profile"]["quality"]["search_eligible"] is True
    assert all(s["polarity"] == "positive" for s in settled["profile"]["statements"])
    assert mention_rows_in(pg) == C04_ROWS
    assert settled["flags"] == {"audit": "ok"}  # nothing left to come back for

    third = AuditingEngine([], clean_audit, repair=polarity_repair)
    assert run(v2_settings(), pg, store, engine=third,
               max_docs=10, max_usd=5.0).reaudited == 0
    assert third.audits == [] and third.repairs == []


def test_a_refused_verdict_on_a_repaired_candidate_ends_the_round(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The other half of "one subsequent audit": a verdict the judge refused IS
    that audit. It is archived, so the round is finished — the base candidate
    settles (an `audit_error` is never a pass, spec §4) and no later run offers
    the repaired candidate a second verdict to try its luck with."""
    seed_case(pg, "C04")
    engine = AuditingEngine([result(polarity_split_c04())],
                            scripted(polarity_audit, unparseable_audit),
                            repair=polarity_repair)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.repair_triggered == 1 and summary.repaired == 0
    candidate = row_of(pg)["chosen_attempt"]
    artifacts = audits_in(store)
    assert set(artifacts) == {keys.x_audit_key(candidate), repaired_audit_key(candidate)}
    assert artifacts[repaired_audit_key(candidate)]["outcome"] == "audit_error"

    row = row_of(pg)
    assert row["status"] == "validated" and row["flags"] == {"audit": "ok"}
    assert row["profile"]["quality"]["search_eligible"] is False
    assert any(s["polarity"] == "negative" for s in row["profile"]["statements"])
    assert mention_rows_in(pg) == []

    later = AuditingEngine([], clean_audit, repair=polarity_repair)
    assert run(v2_settings(), pg, store, engine=later,
               max_docs=10, max_usd=5.0).reaudited == 0
    assert later.audits == [] and later.repairs == []
    assert set(audits_in(store)) == set(artifacts)


def test_a_throttled_repair_audit_stops_the_run_with_the_round_finishable(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """A 429 on the subsequent audit is the provider asking for silence, not a
    verdict on the repaired candidate: nothing is archived, the run stops, and
    the audit the round still owes is there for the next run to take."""
    seed_case(pg, "C04")
    engine = AuditingEngine(
        [result(polarity_split_c04())],
        scripted(polarity_audit, lambda prompt: EngineThrottled("429 rate limited")),
        repair=polarity_repair,
    )
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.throttled is True and summary.repaired == 0
    assert len(engine.audits) == 2  # a 429 is not retried in the same breath
    candidate = row_of(pg)["chosen_attempt"]
    assert set(audits_in(store)) == {keys.x_audit_key(candidate)}
    assert row_of(pg)["flags"] == {"audit": "ok", "audit_retry": True}

    healthy = AuditingEngine([], clean_audit, repair=polarity_repair)
    summary = run(v2_settings(), pg, store, engine=healthy, max_docs=10, max_usd=5.0)
    assert summary.reaudited == 1 and summary.repaired == 1
    assert healthy.repairs == []
    assert row_of(pg)["profile"]["quality"]["search_eligible"] is True
    assert mention_rows_in(pg) == C04_ROWS


def test_a_parked_cohorts_unfinished_repair_audit_is_never_taken_automatically(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Spec §6 draws the same line through the round's second half.

    The cohort disagreed, the audit landed on the dispute and the repair round
    ran before anything was published — but its audit never reached the
    auditor, so the document was published for review. A person may be triaging
    that disagreement now, and finishing the round automatically would settle
    the repaired candidate in the base's place and move a published terminal
    state into the aggregates behind them. The row states honestly that the
    audit is owed; the queue is what refuses to hand it out. The exit is a human
    `retry`, which starts a fresh cohort.
    """
    seed_case(pg, "C04")
    split = polarity_split_c04()
    engine = AuditingEngine(
        [result(emit_of("C04")), result(split), result(split)],
        scripted(polarity_audit, lambda prompt: EngineTransportError("down")),
        repair=polarity_repair,
    )
    summary = run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.repair_triggered == 1 and summary.repaired == 0
    parked = row_of(pg)
    assert parked["status"] == "needs_review"
    assert parked["profile"]["quality"]["sampling"] == "disagreement"
    assert parked["flags"] == {"audit": "ok", "audit_retry": True}

    later = AuditingEngine([], clean_audit, repair=polarity_repair)
    summary = run(v2_settings(), pg, store, engine=later, max_docs=10, max_usd=5.0)
    assert later.audits == [] and later.repairs == []
    assert summary.reaudited == 0 and summary.repaired == 0
    still = row_of(pg)
    assert still["status"] == "needs_review" and still["flags"] == parked["flags"]
    assert mention_rows_in(pg) == []


def test_a_repaired_document_folds_the_same_way_out_of_the_archive(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The archive is truth, repairs included: a catch-up scan that lost every
    derived row folds the repaired candidate back — same verdict, same profile,
    same aggregate rows — without asking a model anything."""
    seed_case(pg, "C04")
    engine = AuditingEngine([result(polarity_split_c04())],
                            scripted(polarity_audit, clean_audit), repair=polarity_repair)
    run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    before = settled_rows(pg)
    mentions_before = mention_rows_in(pg)
    assert mentions_before == C04_ROWS

    pg.execute("TRUNCATE extraction_attempts, extraction_reviews, extractions, profile_mentions")
    pg.commit()
    silent = AuditingEngine([], clean_audit, repair=polarity_repair)
    summary = run(v2_settings(), pg, store, engine=silent, max_docs=0, max_usd=0.0)
    assert silent.calls == [] and silent.audits == [] and silent.repairs == []
    assert summary.replayed > 0
    assert settled_rows(pg) == before
    assert mention_rows_in(pg) == mentions_before
    profile = row_of(pg)["profile"]
    assert all(s["polarity"] == "positive" for s in profile["statements"])


def test_the_repair_campaign_comes_back_for_the_settled_backlog(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The backlog half of the phase (plan Task 4/5).

    A settled document is never re-queued for extraction, so a candidate whose
    blocking findings keep it out of the aggregates would stay out for the life
    of the tuple. The next run's repair pass is what comes back for it — off the
    row's own `flags`, with no extraction and no new sample — and it is the pass
    the re-settle campaign hands its backlog to. Spec §6 draws the line it
    respects: the record is `validated`, so the round can only move ELIGIBILITY,
    never a published lifecycle state.
    """
    seed_case(pg, "C04")
    first = AuditingEngine([result(polarity_split_c04())], polarity_audit)  # no repairer
    summary = run(v2_settings(), pg, store, engine=first, max_docs=10, max_usd=5.0)
    assert summary.repair_triggered == 1 and summary.repaired == 0
    parked = row_of(pg)
    assert parked["status"] == "validated"
    assert parked["profile"]["quality"]["search_eligible"] is False
    assert parked["flags"] == {"audit": "ok", "repair": "eligibility"}
    assert repairs_in(store) == {}  # nothing reached a repairer; the round stands
    assert mention_rows_in(pg) == []

    second = AuditingEngine([], clean_audit, repair=polarity_repair)
    summary = run(v2_settings(), pg, store, engine=second, max_docs=10, max_usd=5.0)
    assert second.calls == []  # no extraction: the document is settled
    assert len(second.repairs) == 1 and len(second.audits) == 1
    assert summary.repair_triggered == 1 and summary.repaired == 1

    row = row_of(pg)
    assert row["status"] == "validated" and row["chosen_attempt"] == parked["chosen_attempt"]
    assert row["profile"]["quality"]["search_eligible"] is True
    assert mention_rows_in(pg) == C04_ROWS
    assert row["flags"] == {"audit": "ok"}

    third = AuditingEngine([], clean_audit, repair=polarity_repair)
    assert run(v2_settings(), pg, store, engine=third, max_docs=10, max_usd=5.0).repaired == 0
    assert third.repairs == [] and third.audits == []  # one round, and it is taken


def test_a_parked_cohort_is_never_repaired_by_the_backlog_campaign(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """Spec §6: the bounded repair loop is not a backdoor around the human-only
    promotion rule. A disagreement whose audit landed on the dispute is
    published for review, and a person may already be triaging it — so the
    campaign leaves it alone, however loudly its flags ask. Its round was the
    one the drain offered before it was ever published."""
    seed_case(pg, "C04")
    split = polarity_split_c04()
    first = AuditingEngine([result(emit_of("C04")), result(split), result(split)],
                           polarity_audit)  # no repairer: the round is not spent
    run(v2_settings(JOB_HUNTER_L2_AUDIT_MOD="1"), pg, store, engine=first,
        max_docs=10, max_usd=5.0)
    parked = row_of(pg)
    assert parked["status"] == "needs_review"
    assert parked["flags"] == {"audit": "ok", "repair": "dispute"}

    later = AuditingEngine([], clean_audit, repair=polarity_repair)
    summary = run(v2_settings(), pg, store, engine=later, max_docs=10, max_usd=5.0)
    assert later.repairs == [] and later.audits == [] and summary.repaired == 0
    assert repairs_in(store) == {}
    still = row_of(pg)
    assert still["status"] == "needs_review" and still["flags"] == parked["flags"]
    assert mention_rows_in(pg) == []


def test_every_phase_artifact_is_archived_before_the_extractions_row(
    pg: Conn, store: ArchiveStore, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """[A1]/spec §5.6: "Archive the final candidate and every phase artifact
    before settlement", repair included.

    The order is the whole recovery story. A crash after a PUT replays that
    phase out of the archive for free; a crash before it re-runs the phase. The
    reverse order would publish a verdict — or a repaired candidate — that
    nothing in the archive backs, which no later run could reproduce or explain.
    """
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
                order.append("repaired-audit" if key.endswith("-r1.json.gz") else "audit")
            elif key.startswith(keys.X_REPAIRS_PREFIX):
                order.append("repair")
            return self._inner.put(key, data)

        def get(self, key: str) -> bytes:
            return self._inner.get(key)

        def exists(self, key: str) -> bool:
            return self._inner.exists(key)

        def list(self, prefix: str, start_after: str | None = None) -> Any:
            return self._inner.list(prefix, start_after)

    seed_case(pg, "C04")
    engine = AuditingEngine([result(polarity_split_c04())],
                            scripted(polarity_audit, clean_audit), repair=polarity_repair)
    run(v2_settings(), pg, Watched(store), engine=engine, max_docs=10, max_usd=5.0)
    assert order == ["audit", "repair", "repaired-audit", "extractions-row"]



def test_live_settle_folds_attempts_from_compat_validators(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The rebuild replays archived validator-17 attempts under 18 but keeps
    the attempt rows at their archived identity; the live fold must read those
    compat attempts or every later settle of a replayed doc is a silent no-op
    (found live 2026-09-14: 2,001 docs stranded — repair artifacts archived,
    rows frozen at their pre-repair content, updated_at still the rebuild's).
    """
    dh = seed_case(pg, "C01")
    run(v2_settings(), pg, store, engine=AuditingEngine([result(emit_of("C01"))], clean_audit),
        max_docs=10, max_usd=5.0)
    # the replayed-corpus shape: attempts at the archived tuple, row at 18
    pg.execute("UPDATE extraction_attempts SET validator_version='17' WHERE document_hash=%s",
               (dh,))
    pg.commit()
    state = settle(pg, store, dh, GLOBS, "2026-09-14T09:00:00Z", bundle=get_bundle("v2"))
    assert state.status == "validated"


# --- retries carry the prior candidate (plan Task 4) -----------------------


class RecordingEngine(AuditingEngine):
    """`AuditingEngine` that also keeps every EXTRACTION prompt it was handed.

    Told apart the same way the phases are: an audit or repair prompt carries a
    candidate-hash line, an extraction never does.
    """

    def __init__(self, script: list[Any], audit: Any, repair: Any = None) -> None:
        super().__init__(script, audit, repair)
        self.extractions: list[str] = []

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        if "CANDIDATE HASH:" not in prompt:
            self.extractions.append(prompt)
        return super().complete(prompt, schema, model)


def broken_c09() -> dict[str, Any]:
    """A C09 emit whose importance evidence cites a block that does not exist.

    One binding error, on `statements[0]` — it names neither the group nor the
    condition, which is what makes the collapse below unexplained.
    """
    emit = copy.deepcopy(emit_of("C09"))
    emit["statements"][0]["importance_evidence"][0]["block_id"] = "b000009"
    return emit


def collapsed_c09() -> dict[str, Any]:
    """The retry-collapse shape, verbatim from the 2026-09-14 analysis: the
    binding error is fixed and the populated `relations` — a group and a
    condition nothing complained about — are gone.

    Every dangling reference to them is scrubbed too, so the emit binds and
    verifies clean: without the delta check this attempt is simply `ok`, and
    the alternatives route is silently lost.
    """
    emit = copy.deepcopy(emit_of("C09"))
    emit["relations"] = {"groups": [], "conditions": [], "example_sets": []}
    emit["statements"][1]["condition_ids"] = []
    emit["facts"]["entries"][0]["condition_ids"] = []
    return emit


def test_a_retry_that_drops_unnamed_relations_is_a_content_error(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The confirmed retry-collapse shape, caught by code.

    Attempt 1 fails one binding error on `statements[0]`. Attempt 2 fixes it and
    deletes the group and the condition no error named — schema-valid, bindable,
    verifier-clean, and wrong. The delta check turns it into a content error and
    the ladder goes round again; attempt 3 brings the relations back.
    """
    seed_case(pg, "C09")
    engine = RecordingEngine(
        [result(broken_c09()), result(collapsed_c09())] + [result(emit_of("C09"))] * 3,
        clean_audit,
    )
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1

    ladder = [a for a in attempts_in(store) if a.sample_slot == 1]
    assert [a.outcome for a in ladder] == [
        "attribution_failed", "attribution_failed", "ok",
    ]
    deletions = [v["error"] for v in ladder[1].validation if "error" in v]
    assert deletions == [
        "retry:unexplained_deletion at relations.groups[0] (id=g_education_route)",
        "retry:unexplained_deletion at relations.conditions[0] (id=c_equivalent_route)",
    ]
    # fed to the next rung like any other content error
    assert ladder[2].prior_errors == deletions
    # the retry prompt carried the candidate it was asked to edit
    assert json.dumps(broken_c09()) in engine.extractions[1]
    assert "Do not remove or rewrite anything the errors do not name" in engine.extractions[1]
    # and the settled record kept the alternatives route
    assert [g["id"] for g in row_of(pg)["profile"]["relations"]["groups"]] == [
        "g_education_route"
    ]


def test_a_repeated_collapse_is_never_published(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The check must not disarm after firing once (2026-09-16 review probe).

    Attempt 3 repeats attempt 2's collapse. The baseline stays the last
    un-flagged candidate and a deletion error is an order to restore, never
    permission to drop — so the loss is refused all the way down the ladder
    and the document quarantines instead of publishing the lossy record."""
    seed_case(pg, "C09")
    engine = RecordingEngine(
        [result(broken_c09()), result(collapsed_c09()), result(collapsed_c09())],
        clean_audit,
    )
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 0
    assert summary.quarantined == 1

    ladder = [a for a in attempts_in(store) if a.sample_slot == 1]
    assert [a.outcome for a in ladder] == [
        "attribution_failed", "attribution_failed", "attribution_failed",
    ]
    # attempt 3 was shown — and judged against — the un-flagged candidate
    assert json.dumps(broken_c09()) in engine.extractions[2]
    assert [v["error"] for v in ladder[2].validation if "error" in v] == [
        "retry:unexplained_deletion at relations.groups[0] (id=g_education_route)",
        "retry:unexplained_deletion at relations.conditions[0] (id=c_equivalent_route)",
    ]


def test_a_retry_that_only_fixes_what_was_named_is_not_a_deletion(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The other half of the check: a minimal targeted edit must pass silently.

    Same failed attempt 1, but the retry keeps every object and fixes only the
    block the error named — nothing is flagged and the document validates on
    attempt 2.
    """
    seed_case(pg, "C09")
    engine = RecordingEngine([result(broken_c09())] + [result(emit_of("C09"))] * 3, clean_audit)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1

    ladder = [a for a in attempts_in(store) if a.sample_slot == 1]
    assert [a.outcome for a in ladder] == ["attribution_failed", "ok"]
    archived = attempts_in(store)
    assert not any(
        "unexplained_deletion" in str(v.get("error", "")) for a in archived for v in a.validation
    )
    assert json.dumps(broken_c09()) in engine.extractions[1]


def test_a_retry_after_an_unparseable_answer_carries_no_candidate(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """There is nothing to preserve when the prior answer never parsed: the
    retry renders exactly today's bytes and the delta check cannot fire — it has
    no baseline, so a smaller retry is not a deletion.
    """
    markdown = source("C09")
    seed_case(pg, "C09")
    engine = RecordingEngine(
        [EngineResult("Sorry — here is the JSON: {", MODEL, 20, 4, 0.0),
         result(collapsed_c09())] + [result(collapsed_c09())] * 2,
        clean_audit,
    )
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.validated == 1

    ladder = [a for a in attempts_in(store) if a.sample_slot == 1]
    assert [a.outcome for a in ladder] == ["schema_invalid", "ok"]
    retry = engine.extractions[1]
    assert "Sorry — here is the JSON:" not in retry
    assert retry == get_bundle("v2").render(markdown, ladder[1].prior_errors, None)


def test_an_error_names_an_object_only_on_a_whole_token(
    # no fixtures: the delta check is pure, and this is the boundary that
    # decides whether it fires at all
) -> None:
    """`s1` is not named by an error about `s10`, and `facts/entries/1` is not
    named by one about `facts/entries/12`.

    A loose (substring) match here silently disarms the check on exactly the
    documents that have enough objects to lose one.
    """
    prior = {
        "statements": [{"id": "s1"}, {"id": "s10"}],
        "facts": {"entries": [{"id": "f_a"}, {"id": "f_b"}]},
    }
    gone: dict[str, Any] = {"statements": [], "facts": {"entries": []}}

    assert unexplained_deletions(prior, gone, ["statements[1]: bad polarity"]) == [
        "retry:unexplained_deletion at statements[0] (id=s1)",
        "retry:unexplained_deletion at facts.entries[0] (id=f_a)",
        "retry:unexplained_deletion at facts.entries[1] (id=f_b)",
    ]
    # hyphen is an id character too (emit schema id pattern ^[A-Za-z0-9_-]+$):
    # an error about `s1-alt` must not name `s1`
    prior_h = {"statements": [{"id": "s1"}, {"id": "s1-alt"}]}
    gone_h: dict[str, Any] = {"statements": []}
    assert unexplained_deletions(prior_h, gone_h, ["statements[1]: bad id s1-alt"]) == [
        "retry:unexplained_deletion at statements[0] (id=s1)",
    ]
    assert unexplained_deletions(prior, gone, ["facts/entries/12: unknown family"]) == [
        "retry:unexplained_deletion at statements[0] (id=s1)",
        "retry:unexplained_deletion at statements[1] (id=s10)",
        "retry:unexplained_deletion at facts.entries[0] (id=f_a)",
        "retry:unexplained_deletion at facts.entries[1] (id=f_b)",
    ]
    # an id the errors DO name may vanish: fixing it is what the retry was for
    assert unexplained_deletions(prior, gone, ["mentions[0]: ungrounded (id=s10)"]) == [
        "retry:unexplained_deletion at statements[0] (id=s1)",
        "retry:unexplained_deletion at facts.entries[0] (id=f_a)",
        "retry:unexplained_deletion at facts.entries[1] (id=f_b)",
    ]


def test_an_unparseable_answer_mid_ladder_keeps_the_last_real_candidate(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The prompt and the delta baseline are always the same emit.

    Attempt 1 produces a candidate, attempt 2 comes back as broken JSON, and
    attempt 3 is shown attempt 1's candidate again — so the objects it is held
    to are exactly the ones it was handed. Letting the unparseable answer
    replace the shown text but not the baseline would judge attempt 3 against a
    candidate it never saw.
    """
    seed_case(pg, "C09")
    engine = RecordingEngine(
        [result(broken_c09()),
         EngineResult("{ truncated", MODEL, 20, 4, 0.0),
         result(collapsed_c09())],
        clean_audit,
    )
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.quarantined == 1

    ladder = [a for a in attempts_in(store) if a.sample_slot == 1]
    assert [a.outcome for a in ladder] == [
        "attribution_failed", "schema_invalid", "attribution_failed",
    ]
    assert json.dumps(broken_c09()) in engine.extractions[2]
    assert [v["error"] for v in ladder[2].validation if "error" in v] == [
        "retry:unexplained_deletion at relations.groups[0] (id=g_education_route)",
        "retry:unexplained_deletion at relations.conditions[0] (id=c_equivalent_route)",
    ]
    # the retry is asked to fix what the SHOWN candidate failed on, not only
    # the parse noise the unparseable answer added (2026-09-16 review): the
    # fed errors carry both halves
    fed = ladder[2].prior_errors
    assert any(e.startswith("response is not valid JSON") for e in fed)
    assert any("statements[0]" in e for e in fed)
