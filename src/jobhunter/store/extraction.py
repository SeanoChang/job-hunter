"""The extraction surface's write path and queue. Exactly one writer (the
extraction runner / review verbs / rebuild) under EXTRACT_LOCK_KEY; ingestion's
lifecycle.py never touches these tables and this module never touches
ingestion's. Rows are fed only from archived attempt/review objects, so the
surface is recomputable by replay."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from jobhunter.l2.attempts import Attempt
from jobhunter.l2.state import DerivedState, Review

Conn = psycopg.Connection[dict[str, Any]]


def globs_to_regex(globs: tuple[str, ...] | list[str]) -> str:
    """fnmatch-style globs -> one anchored POSIX regex for Postgres `~`."""
    parts = []
    for g in globs:
        escaped = re.escape(g).replace(r"\*", ".*").replace(r"\?", ".")
        parts.append(escaped)
    return "^(" + "|".join(parts or ["$^"]) + ")$"


def record_attempt(conn: Conn, a: Attempt, error_detail: dict[str, Any] | None) -> None:
    conn.execute(
        """
        INSERT INTO extraction_attempts (
          attempt_key, run_id, document_hash, normalizer_version, sample_slot,
          attempt_no, requested_engine, requested_model, observed_model,
          prompt_version, schema_version, validator_version, outcome,
          ladder_exhausted, error_detail, input_tokens, output_tokens, cost_usd,
          started_at, finished_at, cli_version
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (attempt_key) DO NOTHING
        """,
        (
            a.attempt_key, a.run_id, a.document_hash, a.normalizer_version, a.sample_slot,
            a.attempt_no, a.requested_engine, a.requested_model, a.observed_model,
            a.prompt_version, a.schema_version, a.validator_version, a.outcome,
            a.ladder_exhausted, Jsonb(error_detail) if error_detail else None,
            a.input_tokens, a.output_tokens, a.cost_usd, a.started_at, a.finished_at,
            a.cli_version,
        ),
    )


def record_review(
    conn: Conn,
    *,
    review_key: str,
    document_hash: str,
    model: str,
    prompt_version: str,
    schema_version: str,
    validator_version: str,
    verb: str,
    payload: dict[str, Any] | None,
    actor: str,
    at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO extraction_reviews (
          review_key, document_hash, model, prompt_version, schema_version,
          validator_version, verb, payload, actor, at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (review_key) DO NOTHING
        """,
        (
            review_key, document_hash, model, prompt_version, schema_version,
            validator_version, verb, Jsonb(payload) if payload else None, actor, at,
        ),
    )


def upsert_state(
    conn: Conn,
    *,
    document_hash: str,
    model: str,
    prompt_version: str,
    schema_version: str,
    validator_version: str,
    state: DerivedState,
    profile: dict[str, Any] | None,
    flags: dict[str, Any] | None = None,
    k: int = 1,
    reviewed_by: str | None = None,
    updated_at: str,
) -> None:
    """status None (pending, e.g. after a human retry) removes the row."""
    key = (document_hash, model, prompt_version, schema_version, validator_version)
    if state.status is None:
        conn.execute(
            "DELETE FROM extractions WHERE document_hash=%s AND model=%s"
            " AND prompt_version=%s AND schema_version=%s AND validator_version=%s",
            key,
        )
        return
    conn.execute(
        """
        INSERT INTO extractions (
          document_hash, model, prompt_version, schema_version, validator_version,
          status, chosen_attempt, k, agreement, profile, flags, reviewed_by, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (document_hash, model, prompt_version, schema_version, validator_version)
        DO UPDATE SET status = EXCLUDED.status,
                      chosen_attempt = EXCLUDED.chosen_attempt,
                      k = EXCLUDED.k,
                      profile = EXCLUDED.profile,
                      flags = EXCLUDED.flags,
                      reviewed_by = EXCLUDED.reviewed_by,
                      updated_at = EXCLUDED.updated_at
        """,
        (
            *key, state.status, state.chosen_attempt, k, None,
            Jsonb(profile) if profile else None, Jsonb(flags) if flags else None,
            reviewed_by, updated_at,
        ),
    )


def queue(
    conn: Conn,
    *,
    prompt_version: str,
    schema_version: str,
    validator_version: str,
    model_regex: str,
    normalizer_version: str,
    limit: int,
) -> list[str]:
    """Pending = absence of ANY row under the current config (spec §4.6);
    priority: current text of open postings -> older versions of open postings
    -> closes within 60 days -> rest; recency DESC within a class."""
    rows = conn.execute(
        """
        WITH satisfied AS (
          SELECT document_hash FROM extractions
          WHERE prompt_version = %(pv)s AND schema_version = %(sv)s
            AND validator_version = %(vv)s AND model ~ %(model_regex)s
        )
        SELECT d.document_hash,
               CASE WHEN bool_or(p.status = 'open'
                                 AND p.current_version_hash = d.version_hash) THEN 0
                    WHEN bool_or(p.status = 'open') THEN 1
                    WHEN max(p.closed_upper_at) > now() - interval '60 days' THEN 2
                    ELSE 3 END AS prio,
               max(p.last_seen_at) AS recency
        FROM documents d
        JOIN posting_versions v ON v.version_hash = d.version_hash
        JOIN postings p ON p.uid = v.uid
        WHERE d.normalizer_version = %(nv)s
          AND d.document_hash NOT IN (SELECT document_hash FROM satisfied)
        GROUP BY d.document_hash
        ORDER BY prio, recency DESC
        LIMIT %(limit)s
        """,
        {
            "pv": prompt_version, "sv": schema_version, "vv": validator_version,
            "model_regex": model_regex, "nv": normalizer_version, "limit": limit,
        },
    ).fetchall()
    return [r["document_hash"] for r in rows]


def watermark(conn: Conn) -> datetime | None:
    row = conn.execute("SELECT max(started_at) AS w FROM extraction_attempts").fetchone()
    return row["w"] if row else None


def markdown_for(conn: Conn, document_hash: str, normalizer_version: str) -> str | None:
    row = conn.execute(
        "SELECT markdown FROM documents WHERE document_hash=%s AND normalizer_version=%s LIMIT 1",
        (document_hash, normalizer_version),
    ).fetchone()
    return row["markdown"] if row else None


def attempts_for(conn: Conn, document_hash: str) -> list[Attempt]:
    """Rows re-inflated for derive_state; raw_response/prior_errors/validation
    live only in the archive and are irrelevant to state."""
    rows = conn.execute(
        "SELECT * FROM extraction_attempts WHERE document_hash=%s ORDER BY started_at, attempt_no",
        (document_hash,),
    ).fetchall()
    out: list[Attempt] = []
    for r in rows:
        out.append(
            Attempt(
                attempt_key=r["attempt_key"], run_id=r["run_id"],
                cli_version=r["cli_version"], document_hash=r["document_hash"],
                normalizer_version=r["normalizer_version"], sample_slot=r["sample_slot"],
                attempt_no=r["attempt_no"], requested_engine=r["requested_engine"],
                requested_model=r["requested_model"], observed_model=r["observed_model"],
                prompt_version=r["prompt_version"], prompt_sha256="",
                schema_version=r["schema_version"],
                validator_version=r["validator_version"], prior_errors=[],
                raw_response=None, validation=[], outcome=r["outcome"],
                ladder_exhausted=r["ladder_exhausted"],
                input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                cost_usd=float(r["cost_usd"]) if r["cost_usd"] is not None else None,
                started_at=r["started_at"].isoformat(),
                finished_at=r["finished_at"].isoformat(),
            )
        )
    return out


def reviews_for(conn: Conn, document_hash: str) -> list[Review]:
    rows = conn.execute(
        "SELECT verb, at, actor FROM extraction_reviews WHERE document_hash=%s ORDER BY at",
        (document_hash,),
    ).fetchall()
    return [Review(verb=r["verb"], at=r["at"].isoformat(), actor=r["actor"]) for r in rows]


def update_status(
    conn: Conn,
    *,
    document_hash: str,
    model: str,
    prompt_version: str,
    schema_version: str,
    validator_version: str,
    state: DerivedState,
    reviewed_by: str | None,
    updated_at: str,
) -> None:
    """Status-only rewrite after a review event; the stored profile is kept
    (review verbs judge, they never regenerate). status None deletes the row."""
    key = (document_hash, model, prompt_version, schema_version, validator_version)
    if state.status is None:
        conn.execute(
            "DELETE FROM extractions WHERE document_hash=%s AND model=%s"
            " AND prompt_version=%s AND schema_version=%s AND validator_version=%s",
            key,
        )
        return
    conn.execute(
        "UPDATE extractions SET status=%s, reviewed_by=%s, updated_at=%s"
        " WHERE document_hash=%s AND model=%s AND prompt_version=%s"
        " AND schema_version=%s AND validator_version=%s",
        (state.status, reviewed_by, updated_at, *key),
    )
