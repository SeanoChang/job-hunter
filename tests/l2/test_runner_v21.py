"""Integration: bundle v3 drives a drain end to end (parsing contract v4).

A V1-shaped emit (Visa: a responsibility line naming Kafka, Docker and
Kubernetes, and a "will not sponsor" policy) goes through the real runner with
a fake engine: the v12 prompt and the schema-4 strict emit schema are what the
engine is handed, assembly and verification run at schema 4 under validator
21, the auditor is asked under `semantic-audit/v5`, and the archived record
says `authorization.sponsorship == "no"` with three typed skill mentions on
the responsibility. Recorded emits only; no model is called.
"""

from __future__ import annotations

from typing import Any

import psycopg

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.hashing import sha256_hex
from jobhunter.l2.engines import EngineResult
from jobhunter.l2.runner import run
from jobhunter.l2.schemas import strict_schema
from jobhunter.l2.v2.audit import AUDIT_VERSION_V5
from jobhunter.l2.v2.emit_guard import engine_emit_schema
from jobhunter.l2.v2.prompt_v14 import PROMPT_VERSION
from tests.l2.test_runner import _seed_doc, _settings, store  # noqa: F401
from tests.l2.test_runner_v2 import (
    AuditingEngine,
    attempts_in,
    audits_in,
    clean_audit,
    result,
)
from tests.l2.v2.conftest import VISA_MD, make_visa_emit

Conn = psycopg.Connection[dict[str, Any]]

V3_TUPLE = ("demand-profile/v14", "4", "22")


class SchemaRecordingEngine(AuditingEngine):
    """`AuditingEngine` that keeps every (prompt, schema) it was handed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.handed: list[tuple[str, dict[str, Any]]] = []

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        self.handed.append((prompt, schema))
        return super().complete(prompt, schema, model)


def test_bundle_v3_extracts_a_visa_posting_end_to_end(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    dh = sha256_hex(VISA_MD.encode("utf-8"))
    _seed_doc(pg, dh=dh, markdown=VISA_MD, uid="gh:x:visa")
    engine = SchemaRecordingEngine([result(make_visa_emit())], clean_audit)
    summary = run(_settings(JOB_HUNTER_L2_BUNDLE="v3"), pg, store, engine=engine,
                  max_docs=10, max_usd=5.0)
    assert summary.validated == 1 and summary.docs_attempted == 1

    # the engine was asked under v12 with the schema-4 guard
    extraction_prompt, schema = engine.handed[0]
    assert "WORK AUTHORIZATION: THREE FAMILIES" in extraction_prompt
    assert schema in (engine_emit_schema("4"), strict_schema(engine_emit_schema("4")))
    assert "Self-serve deployment of Kafka clusters" in extraction_prompt

    # the archive holds the v3 tuple's attempt and a schema-4 record
    assert store.exists(keys.x_prompt_key(PROMPT_VERSION))
    assert store.exists(keys.x_schema_key("4"))
    attempt = attempts_in(store)[0]
    assert attempt.outcome == "ok"
    assert (attempt.prompt_version, attempt.schema_version,
            attempt.validator_version) == V3_TUPLE
    record = attempt.record
    assert record is not None
    assert record["authorization"]["sponsorship"] == "no"
    assert record["authorization"]["citizenship_required"] is False
    assert record["authorization"]["evidence"]["sponsorship"][0]["text"].startswith(
        "Visa will not sponsor")
    kinds = {s["id"]: s["kind"] for s in record["statements"]}
    skills = {m["surface"]: (m["type"], [kinds[s] for s in m["statement_ids"]])
              for m in record["mentions"]}
    assert skills == {name: ("skill", ["responsibility"])
                      for name in ("Kafka", "Docker", "Kubernetes")}

    # the auditor was asked under semantic-audit/v5, and its verdict archived there
    assert engine.audits
    assert "not recorded as relations.tracks is bad_exclusion" in " ".join(
        engine.audits[0].split())
    artifacts = audits_in(store)
    assert artifacts and all(a["audit_version"] == AUDIT_VERSION_V5
                             for a in artifacts.values())

    # the settled row is the v3 tuple's, validated
    row = pg.execute("SELECT * FROM extractions").fetchone()
    assert row is not None
    assert row["status"] == "validated" and row["document_hash"] == dh
    assert (row["prompt_version"], row["schema_version"],
            row["validator_version"]) == V3_TUPLE
