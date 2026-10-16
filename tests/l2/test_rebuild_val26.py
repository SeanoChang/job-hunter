"""validator/26 keeps the audits the corpus already paid for.

A record sealed under 25 and re-judged under 26 gains the skill pass's
mentions. Those are outside the candidate's identity
(`assemble.candidate_hash` leaves them out), so replay adopts the archived
candidate with the pass mentions added, its hash stays the one the audit
artifact names, and the document stays search-eligible with no model call.
A different model output still gets no borrowed verdict.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import psycopg

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.hashing import sha256_hex
from jobhunter.l2.attempts import from_bytes, to_bytes
from jobhunter.l2.bundles import get_bundle
from jobhunter.l2.rebuild import _rejudge, rebuild_extractions
from jobhunter.l2.v2.assemble import candidate_hash
from jobhunter.l2.v2.skill_pass import is_pass_mention
from jobhunter.timeutil import iso, parse_iso
from tests.l2 import test_runner_v2 as v2
from tests.l2.test_attempts import _attempt
from tests.l2.test_runner import _seed_doc, store  # noqa: F401
from tests.l2.v2.conftest import VISA_MD, make_visa_emit

Conn = psycopg.Connection[dict[str, Any]]

V3 = get_bundle("v3")
STARTED = "2026-10-11T06:12:04Z"
DH = sha256_hex(VISA_MD.encode("utf-8"))


def _emit(*dropped: str) -> dict[str, Any]:
    """VISA with some model mentions left out, for the pass to find."""
    emit = copy.deepcopy(make_visa_emit())
    emit["mentions"] = [m for m in emit["mentions"] if m["id"] not in dropped]
    return emit


def _archive_v25(pg: Conn, store: ArchiveStore, raw: dict[str, Any]) -> str:  # noqa: F811
    """A validated, audited 25 candidate for `_emit("m_docker")`, whose
    archived attempt holds `raw` as its response. Returns its hash."""
    _seed_doc(pg, dh=DH, markdown=VISA_MD, uid="gh:x:visa")
    record = V3.assemble(_emit("m_docker"), VISA_MD, document_hash=DH,
                         observed_model=v2.MODEL, at=iso(parse_iso(STARTED)))
    assert [m["surface"] for m in record["mentions"] if is_pass_mention(m)] == ["Docker"]
    # the candidate as validator 25 sealed it: no pass mentions, its own stamp
    record["mentions"] = [m for m in record["mentions"] if not is_pass_mention(m)]
    record["extraction"]["validator_version"] = "25"
    record["extraction"]["candidate_hash"] = candidate_hash(record)
    attempt = _attempt(
        attempt_key=keys.x_attempt_key(parse_iso(STARTED), DH, 1, 1), document_hash=DH,
        prompt_version=V3.prompt_version, schema_version="4", validator_version="25",
        requested_model=v2.MODEL, observed_model=v2.MODEL, record=record,
        raw_response=json.dumps(raw), started_at=STARTED, finished_at=STARTED,
    )
    store.put(attempt.attempt_key, to_bytes(attempt))
    hashed = str(record["extraction"]["candidate_hash"])
    v2.archive_audit(store, attempt.attempt_key, candidate_hash=hashed,
                     version=V3.audit_version)
    return hashed


def _row(pg: Conn) -> dict[str, Any]:
    row = pg.execute(
        "SELECT * FROM extractions WHERE prompt_version=%s AND schema_version='4'"
        " AND validator_version=%s", (V3.prompt_version, V3.validator_version)).fetchone()
    assert row is not None
    return dict(row)


def test_an_audited_25_record_stays_eligible_under_26(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    hashed = _archive_v25(pg, store, _emit("m_docker"))
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    row = _row(pg)
    assert row["status"] == "validated"
    assert row["profile"]["quality"]["search_eligible"] is True  # the carried audit
    mentions = row["profile"]["mentions"]
    assert [m["surface"] for m in mentions if is_pass_mention(m)] == ["Docker"]
    assert "Docker" in {r["mention"] for r in pg.execute(
        "SELECT mention FROM profile_mentions WHERE document_hash=%s", (DH,)).fetchall()}
    # the audit attached by hash: the re-judged record keeps the archived hash,
    # and that hash is still true of it, pass mentions and all
    assert row["flags"].get("audit") == "ok"
    attempt = from_bytes(store.get(keys.x_attempt_key(parse_iso(STARTED), DH, 1, 1)))
    rejudged = _rejudge(attempt, VISA_MD, V3).record
    assert rejudged is not None
    assert any(is_pass_mention(m) for m in rejudged["mentions"])
    assert rejudged["extraction"]["candidate_hash"] == hashed == candidate_hash(rejudged)


def test_a_different_model_output_borrows_no_audit(
    pg: Conn, store: ArchiveStore  # noqa: F811
) -> None:
    # the archived response is not the candidate the audit describes
    _archive_v25(pg, store, _emit("m_docker", "m_k8s"))
    rebuild_extractions(pg, store, ("z-ai/*",))
    pg.commit()
    row = _row(pg)
    assert row["status"] == "validated"
    assert row["profile"]["quality"]["search_eligible"] is False
    surfaces = [m["surface"] for m in row["profile"]["mentions"] if is_pass_mention(m)]
    assert surfaces == ["Docker", "Kubernetes"]
