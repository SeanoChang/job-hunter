"""Weekly consolidation (harness spec §2): reads validated extractions only
and emits append-only artifacts to the archive — a drift report (status
counts, agreement series, engine-tuple mix, per-source quarantine rates), the
human audit queue (needs_review and quarantined, oldest first, with reasons),
and the refuter summary. Pure read-side over the extraction tables plus one
write-once archive object per run; no DB writes, so no lock.

The freshness check lives here rather than in a scheduler: the hourly
workflow calls the verb every run and it fires when the newest artifact is a
week old — a second cron is a second thing that can die silently
(durability doc §1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from jobhunter.archive import keys
from jobhunter.archive.base import ArchiveStore
from jobhunter.hashing import canonical_json
from jobhunter.store.extraction import Conn
from jobhunter.timeutil import iso, utcnow

FRESH_DAYS = 7


def _latest_artifact_at(store: ArchiveStore) -> datetime | None:
    latest: datetime | None = None
    for key in store.list(keys.X_CONSOLIDATION_PREFIX):
        stamp = key.removeprefix(keys.X_CONSOLIDATION_PREFIX).removesuffix(".json")
        try:
            at = datetime.strptime(stamp, "%Y/%m/%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            continue
        if latest is None or at > latest:
            latest = at
    return latest


def consolidate(
    conn: Conn,
    store: ArchiveStore,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    at = now or utcnow()
    if not force:
        latest = _latest_artifact_at(store)
        if latest is not None and at - latest < timedelta(days=FRESH_DAYS):
            return {"skipped": "fresh", "last": iso(latest), "artifact": None}

    counts = {
        str(r["status"]): int(r["n"])
        for r in conn.execute(
            "SELECT status, count(*) AS n FROM extractions GROUP BY status"
        ).fetchall()
    }
    tuple_mix = [
        dict(r)
        for r in conn.execute(
            "SELECT prompt_version, validator_version, count(*) AS n"
            " FROM extractions GROUP BY 1, 2 ORDER BY 3 DESC"
        ).fetchall()
    ]
    agreement = conn.execute(
        "SELECT count(*) AS gated,"
        " avg((agreement->>'mean_f1')::float) AS mean_f1,"
        " count(*) FILTER (WHERE agreement->'failures' <> '[]'::jsonb) AS failed"
        " FROM extractions WHERE agreement IS NOT NULL"
    ).fetchone()
    audit_queue = [
        {
            "document_hash": r["document_hash"],
            "status": r["status"],
            "updated_at": str(r["updated_at"]),
            "reasons": (
                (r["agreement"] or {}).get("failures")
                or ([r["reviewed_by"]] if r["reviewed_by"] else [])
            ),
        }
        for r in conn.execute(
            "SELECT document_hash, status, updated_at, agreement, reviewed_by"
            " FROM extractions WHERE status IN ('needs_review', 'quarantined')"
            " ORDER BY updated_at ASC LIMIT 200"
        ).fetchall()
    ]
    refuter = {
        str(r["verb"]): int(r["n"])
        for r in conn.execute(
            "SELECT verb, count(*) AS n FROM extraction_reviews"
            " WHERE actor LIKE 'refuter:%' GROUP BY verb"
        ).fetchall()
    }

    artifact: dict[str, Any] = {
        "at": iso(at),
        "counts": counts,
        "engine_tuples": tuple_mix,
        "agreement_series": {
            "gated": int(agreement["gated"]) if agreement else 0,
            "mean_f1": float(agreement["mean_f1"]) if agreement and agreement["mean_f1"] else None,
            "failed": int(agreement["failed"]) if agreement else 0,
        },
        "audit_queue": audit_queue,
        "refuter": refuter,
    }
    key = keys.x_consolidation_key(at)
    store.put(key, canonical_json(artifact))
    return {"skipped": None, "artifact": key, "counts": counts,
            "audit_queue_depth": len(audit_queue), "refuter": refuter}
