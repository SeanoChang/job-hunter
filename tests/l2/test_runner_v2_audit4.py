"""The `semantic-audit/v4` + `semantic-repair/v2` bump, end to end over
Postgres + LocalFS.

The v20 contract retires `importance` and rewrites the auditor's scope
(parsing contract v3 §6), so every verdict `semantic-audit/v3` reached was
reached under a scope this corpus no longer publishes on. What has to happen
to a document the v3 auditor already cleared is therefore: nothing is
overwritten, and the document is owed one call.

This file exercises exactly that — the version-keyed artifact plumbing
(`keys.x_audit_key`) under a bump it has never carried before. The plumbing
itself is `test_runner_v2.py`'s, and none of it changes here: what the bump
needs is already in place, and this file is the proof of that claim rather
than a new mechanism.

It also holds the repair phase's own boundary test, for the same reason: the
bump gave `emit_schema` a schema argument, and the thing a parameter with a
default can get wrong is which shape the RUNNER ends up advertising. Every
fake engine in this suite discards the schema it is handed, so that pairing
had no test at all.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import jsonschema
import psycopg

from jobhunter.archive.base import ArchiveStore
from jobhunter.l2.bundles import get_bundle
from jobhunter.l2.engines import EngineResult, EngineThrottled
from jobhunter.l2.runner import run, settle
from jobhunter.l2.v2.audit import AUDIT_VERSION
from jobhunter.timeutil import iso
from tests.l2.test_runner import store  # noqa: F401  (the LocalFS archive fixture)
from tests.l2.test_runner_v2 import (
    C04_ROWS,
    AuditingEngine,
    archive_audit,
    attempts_in,
    audit_key,
    audits_in,
    clean_audit,
    emit_of,
    flags,
    mention_rows_in,
    polarity_audit,
    polarity_repair,
    polarity_split_c04,
    reaudit_window,
    result,
    retry_key,
    row_of,
    scripted,
    seed_case,
    v2_settings,
)

Conn = psycopg.Connection[dict[str, Any]]

#: the audit contract this corpus settled under before the v20 bump
PRIOR_AUDIT_VERSION = "semantic-audit/v3"
#: the v2 engine tuple as it stood then: same prompt, schema, validator and
#: judge — only the audit contract differs, which is what a bump IS
V3_BUNDLE = replace(get_bundle("v2"), audit_version=PRIOR_AUDIT_VERSION)


def v3_audited(pg: Conn, store: ArchiveStore, case: str = "C04") -> tuple[str, str]:  # noqa: F811
    """A document as the corpus holds it after a `semantic-audit/v3` run.

    The drain extracts it with an auditor that never answers (so the runner
    archives no verdict of its own), the v3 artifact is then written at the
    `.a3` key, and the row is folded under the v3 bundle: a validated,
    eligible, fully audited document, which is what the table looked like the
    moment before the v4 bump.
    """
    dh = seed_case(pg, case)
    throttled = AuditingEngine(
        [result(emit_of(case))], lambda prompt: EngineThrottled("429 rate limited")
    )
    run(v2_settings(), pg, store, engine=throttled, max_docs=10, max_usd=5.0)
    candidate = row_of(pg)["chosen_attempt"]
    chosen = {a.attempt_key: a for a in attempts_in(store)}[candidate]
    archive_audit(
        store, candidate, version=PRIOR_AUDIT_VERSION,
        candidate_hash=chosen.record["extraction"]["candidate_hash"],
    )
    settle(pg, store, dh, ("z-ai/*",), iso(datetime.now(UTC)), bundle=V3_BUNDLE)
    pg.commit()
    row = row_of(pg)
    assert row["status"] == "validated" and row["profile"]["quality"]["search_eligible"]
    assert row["flags"] == {"audit": "ok", "audit_version": PRIOR_AUDIT_VERSION}
    assert set(audits_in(store)) == {audit_key(candidate, PRIOR_AUDIT_VERSION)}
    return dh, candidate


def test_the_v3_audit_key_and_the_v4_one_are_different_artifacts(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The segment carries the number, so the bump writes a new key rather
    than a second verdict at the old one (the archive is write-once)."""
    _, candidate = v3_audited(pg, store)
    assert audit_key(candidate, PRIOR_AUDIT_VERSION).endswith(".a3.json.gz")
    assert audit_key(candidate, AUDIT_VERSION).endswith(".a4.json.gz")
    assert audit_key(candidate) == audit_key(candidate, AUDIT_VERSION)


def test_a_v3_audited_document_is_owed_a_v4_re_audit(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """A v3 verdict is not a v4 verdict.

    `semantic-audit/v4` removed a finding code and re-scoped what
    `search_eligible` asserts, so the document has to be judged again under the
    contract in force — and the existing re-audit queue is what reaches it. One
    call per document, no extraction, the `.a3` artifact left exactly where it
    was, and the candidate's second pass unspent: the bump costs the corpus one
    audit each, which is the budget the queue already enforces.
    """
    dh, candidate = v3_audited(pg, store)
    old_key = audit_key(candidate, PRIOR_AUDIT_VERSION)
    before = audits_in(store)

    # the row the bump leaves behind is work owed, and the queue is what says so
    assert [d for d, _ in reaudit_window(pg, limit=10)] == [dh]

    engine = AuditingEngine([], clean_audit)  # an extraction call would raise
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert engine.calls == [] and len(engine.audits) == 1
    assert summary.reaudited == 1

    artifacts = audits_in(store)
    assert set(artifacts) == {old_key, audit_key(candidate)}
    assert artifacts[old_key] == before[old_key]  # beside the v3 verdict, never over it
    fresh = artifacts[audit_key(candidate)]
    assert fresh["audit_version"] == AUDIT_VERSION and fresh["audit_pass"] == 1
    assert fresh["attempt_key"] == candidate and fresh["outcome"] == "ok"
    # pass 1 of the new version, so the retry pass is still there to spend
    assert retry_key(candidate) not in artifacts

    row = row_of(pg)
    assert row["status"] == "validated" and row["chosen_attempt"] == candidate
    assert row["flags"] == flags("ok")
    assert row["profile"]["quality"]["search_eligible"] is True
    assert mention_rows_in(pg) == C04_ROWS

    # and it is owed once: the next run asks the model nothing
    assert reaudit_window(pg, limit=10) == []
    again = AuditingEngine([], clean_audit)
    summary = run(v2_settings(), pg, store, engine=again, max_docs=10, max_usd=5.0)
    assert again.audits == [] and summary.reaudited == 0
    assert set(audits_in(store)) == {old_key, audit_key(candidate)}


# --- semantic-repair/v2: the shape the runner advertises --------------------


class SchemaSpyEngine(AuditingEngine):
    """`AuditingEngine` that also keeps the schema each repair call was handed.

    Every real engine passes that schema to the provider as structured output
    — `response_format.json_schema` with `strict`, `claude --json-schema`,
    `codex --output-schema` — so it is a contract the model answers under, not
    a hint. The fakes in `test_runner_v2.py` drop the argument, which is
    exactly why the runner↔contract pairing below has never been checked.
    """

    def __init__(self, script: list[Any], audit: Any, repair: Any = None) -> None:
        super().__init__(script, audit, repair)
        self.repair_schemas: list[dict[str, Any]] = []

    def complete(self, prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
        if "BASE CANDIDATE HASH:" in prompt:
            self.repair_schemas.append(schema)
        return super().complete(prompt, schema, model)


def _obedient(node: Any) -> Any:
    """`node` with the fields the repair prompt forbids a model to send.

    "Never send a span, a derived value, a normalized key or an object_hash
    inside an object: code computes all of them from the evidence you cite."
    `test_runner_v2.py`'s repairer echoes the spans anyway because `apply`
    strips them before judging — harmless there, but it hides the shape under
    a field the schema would reject for every version alike, which is not the
    disagreement this test is about.
    """
    if isinstance(node, dict):
        return {k: _obedient(v) for k, v in node.items()
                if k not in ("span", "derived", "normalized_key", "object_hash")}
    if isinstance(node, list):
        return [_obedient(item) for item in node]
    return node


def obedient_polarity_repair(prompt: str) -> str:
    """`polarity_repair`, written the way the prompt says to write one."""
    return json.dumps(_obedient(json.loads(polarity_repair(prompt))))


def test_the_repair_schema_the_runner_advertises_admits_the_repair_it_accepts(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    """The shape handed to the repair engine is the shape the record takes.

    `_REPAIR_CONTRACTS` is keyed by SCHEMA version and the runner calls
    `contract.emit_schema()` with no argument, so whatever that default is
    becomes the output schema for every record of the shape the contract is
    registered under. `apply` then holds the answer to the BASE RECORD's own
    shape. If those two disagree the round is unwinnable and unrepeatable —
    `x_repair_key` is write-once, and the failure artifact is written at it —
    so a model obeying the advertised schema spends the document's one repair
    round on a `repair_error` it could not have avoided.

    The assertion is that pairing: the emit this run's `apply` ACCEPTED must
    validate against the schema this run HANDED the engine.
    """
    seed_case(pg, "C04")
    engine = SchemaSpyEngine([result(polarity_split_c04())],
                             scripted(polarity_audit, clean_audit),
                             repair=obedient_polarity_repair)
    summary = run(v2_settings(), pg, store, engine=engine, max_docs=10, max_usd=5.0)
    assert summary.repair_triggered == 1 and summary.repaired == 1

    (advertised,) = engine.repair_schemas
    (prompt,) = engine.repairs
    accepted = json.loads(obedient_polarity_repair(prompt))  # the emit `apply` just took
    validator = jsonschema.Draft202012Validator(advertised)
    assert validator.is_valid(accepted), [
        f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(accepted)
    ]
