"""The v20 migration replay, and the numbers that say whether it worked.

`extract rebuild` is what actually moves the corpus: replay re-judges every
archived attempt under today's validators and, for the frozen
`(demand-profile/v10, 2)` partition the whole live archive sits in, derives the
schema-3 candidate beside it and settles that under the active tuple
(`l2/rebuild.py`). This script is the operator's wrapper around that one call —
it snapshots the surface before, runs the replay with an engine-call counter
installed, snapshots after, and prints the comparison the migration is judged
on: rows per status, rows per engine tuple, how many documents now serve skills,
and what share of the migrated partition is waiting on a human.

Three modes:

    uv run python scripts/migrate_v20_report.py --dry-run   # plan + before only
    uv run python scripts/migrate_v20_report.py             # replay + report
    uv run python scripts/migrate_v20_report.py --check     # re-count, no write

`--check` is the ticket predicate, and it asserts the three things that make the
migration what it claims to be: every document the migration covers has a row
under the active tuple, no row under that tuple still holds a schema-2 blob, and
no engine was called. The first is the one that can fail on a corpus nobody
replayed — the other two are satisfied by an untouched database as readily as by
a migrated one (`check_failures`). The counter is installed in every mode,
`--check` included, because the one way this script could quietly become a
re-extraction is a code path reaching an engine where nobody expected one.

The replay takes the extract writer lock, so it cannot run beside a drain.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg

from jobhunter.l2.bundles import get_bundle

#: The migration's target. This script exists for ONE transition — the v2
#: family's schema-2 corpus reaching the schema-3 contract — so the active
#: tuple it measures against is the v2 registration's, read from the registry
#: rather than typed here, and not `settings.l2_bundle` (which can name v1 and
#: would then report a migration that never happened as complete).
ACTIVE = get_bundle("v2")
TUPLE = (ACTIVE.prompt_version, ACTIVE.schema_version, ACTIVE.validator_version)
#: the partitions the migration CLAIMS, read off the active bundle rather than
#: typed here: `Bundle.migrated_from` is the registry's own statement of which
#: retired tuples this one adopts, and it is what `--check` measures against. A
#: schema-2 row under a retired PROMPT (v6..v9) is deliberately not migrated
#: (`l2/rebuild`'s docstring), so counting every schema-2 row as owed would make
#: the predicate fire on documents the migration is right to leave alone.
MIGRATED_FROM = ACTIVE.migrated_from
Conn = psycopg.Connection[dict[str, Any]]


def _tuple_key(prompt_version: str, schema_version: str, validator_version: str) -> str:
    return f"{prompt_version}/{schema_version}/{validator_version}"


@dataclass(frozen=True)
class Counts:
    """One snapshot of the extraction surface."""

    rows: int
    by_status: dict[str, int]
    by_tuple: dict[str, int]
    active_by_status: dict[str, int]
    #: distinct documents with `profile_mentions` rows under the active tuple —
    #: the two-tier serving measure, "docs now serving skills"
    docs_serving_skills: int
    review_share: float
    #: rows the read surface answers from that still hold the retired shape
    schema2_rows_under_active: int
    #: documents with a schema-2 row and nothing under the active tuple
    documents_not_migrated: int
    #: documents under a tuple the migration CLAIMS (`MIGRATED_FROM`) with
    #: nothing under the active tuple — the count that separates "the migration
    #: ran" from "the migration never started"
    documents_owed_migration: int


def counts(conn: Conn) -> Counts:
    """Everything the report compares, in five queries."""
    by_status = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, count(*) AS n FROM extractions GROUP BY status"
        ).fetchall()
    }
    by_tuple = {
        _tuple_key(r["prompt_version"], r["schema_version"], r["validator_version"]): r["n"]
        for r in conn.execute(
            "SELECT prompt_version, schema_version, validator_version, count(*) AS n"
            " FROM extractions GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        ).fetchall()
    }
    active_by_status = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, count(*) AS n FROM extractions"
            " WHERE prompt_version=%s AND schema_version=%s AND validator_version=%s"
            " GROUP BY status",
            TUPLE,
        ).fetchall()
    }
    serving = conn.execute(
        "SELECT count(DISTINCT document_hash) AS n FROM profile_mentions"
        " WHERE prompt_version=%s AND schema_version=%s AND validator_version=%s",
        TUPLE,
    ).fetchone()
    stale = conn.execute(
        # a row with no profile (nothing settled) holds no shape at all; only a
        # STORED blob can be of the wrong one
        "SELECT count(*) AS n FROM extractions"
        " WHERE prompt_version=%s AND schema_version=%s AND validator_version=%s"
        "   AND profile IS NOT NULL AND profile->>'schema' IS DISTINCT FROM %s",
        (*TUPLE, ACTIVE.schema_version),
    ).fetchone()
    unmigrated = conn.execute(
        # schema 2 is the v2 family's retired shape and nothing else writes it,
        # so "has a schema-2 row" is exactly "was extracted before the v20 bump"
        "SELECT count(DISTINCT document_hash) AS n FROM extractions e"
        " WHERE e.schema_version = '2' AND NOT EXISTS ("
        "   SELECT 1 FROM extractions a WHERE a.document_hash = e.document_hash"
        "     AND a.prompt_version=%s AND a.schema_version=%s AND a.validator_version=%s)",
        TUPLE,
    ).fetchone()
    owed = 0
    for prompt_version, schema_version in MIGRATED_FROM:
        row = conn.execute(
            "SELECT count(DISTINCT document_hash) AS n FROM extractions e"
            " WHERE e.prompt_version=%s AND e.schema_version=%s AND NOT EXISTS ("
            "   SELECT 1 FROM extractions a WHERE a.document_hash = e.document_hash"
            "     AND a.prompt_version=%s AND a.schema_version=%s AND a.validator_version=%s)",
            (prompt_version, schema_version, *TUPLE),
        ).fetchone()
        owed += int(row["n"]) if row else 0
    active_rows = sum(active_by_status.values())
    return Counts(
        rows=sum(by_status.values()),
        by_status=by_status,
        by_tuple=by_tuple,
        active_by_status=active_by_status,
        docs_serving_skills=int(serving["n"]) if serving else 0,
        review_share=(
            active_by_status.get("needs_review", 0) / active_rows if active_rows else 0.0
        ),
        schema2_rows_under_active=int(stale["n"]) if stale else 0,
        documents_not_migrated=int(unmigrated["n"]) if unmigrated else 0,
        documents_owed_migration=owed,
    )


@dataclass
class EngineCalls:
    """A counter over every extraction engine the package can construct.

    The migration's whole claim is that 17k documents reach schema 3 for $0, and
    the only way to hold that claim honestly is to count. Wrapping the three
    engine classes catches a call from anywhere under the replay — a settle path
    reaching for a re-audit, a repair round, a future helper — rather than
    trusting that `rebuild_extractions` takes no engine parameter.
    """

    n: int = 0

    @contextlib.contextmanager
    def installed(self) -> Iterator[EngineCalls]:
        from jobhunter.l2 import engines

        classes = (engines.OpenAICompat, engines.CodexCli, engines.ClaudeCli)
        originals = [(cls, cls.complete) for cls in classes]

        def counted(original: Any) -> Any:
            def complete(*args: Any, **kwargs: Any) -> Any:
                self.n += 1
                return original(*args, **kwargs)

            return complete

        for cls, original in originals:
            cls.complete = counted(original)  # type: ignore[method-assign]
        try:
            yield self
        finally:
            for cls, original in originals:
                cls.complete = original  # type: ignore[method-assign]


@dataclass(frozen=True)
class Result:
    before: Counts
    after: Counts
    engine_calls: int
    attempts_replayed: int
    reviews_replayed: int


def run_report(conn: Conn, store: Any, models: tuple[str, ...]) -> Result:
    """Snapshot, replay, snapshot. The caller owns the transaction and the lock."""
    from jobhunter.l2.rebuild import rebuild_extractions

    before = counts(conn)
    calls = EngineCalls()
    with calls.installed():
        attempts, reviews = rebuild_extractions(conn, store, models)
    return Result(before, counts(conn), calls.n, attempts, reviews)


def check_failures(found: Counts, *, engine_calls: int) -> list[str]:
    """The `--check` predicate: what, if anything, says the migration did not
    happen the way it promised.

    The ticket's invariant is "migration is offline and TOTAL", and the two
    original terms only ever spoke to "offline". Both were vacuously satisfied
    on a surface the migration had never touched: a schema-2 blob cannot be
    stored under the active tuple by any code path (`serve.profile_of` stamps
    the record's own schema, and `rebuild._derive3` files nothing that is not
    schema 3), and `--check` runs no replay, so its engine counter is zero by
    construction. The predicate said PASS on an empty database.

    `documents_owed_migration` is the positive term, and it is scoped to the
    tuples the registry says the migration covers (`MIGRATED_FROM`) rather than
    to every schema-2 row: a retired-prompt row is history the migration
    deliberately leaves behind (`l2/rebuild`), but a `(demand-profile/v10, 2)`
    document with nothing under the active tuple is either a replay that has not
    run or a derivation that refused — both of them things the operator has to
    see before calling ac-2 met.
    """
    failures: list[str] = []
    if found.documents_owed_migration:
        failures.append(
            f"{found.documents_owed_migration} document(s) under "
            f"{', '.join(f'{p}/{s}' for p, s in MIGRATED_FROM)} have no row under the "
            f"active tuple {_tuple_key(*TUPLE)}; the migration is incomplete"
        )
    if found.schema2_rows_under_active:
        failures.append(
            f"{found.schema2_rows_under_active} schema-2 row(s) still stored under the "
            f"active tuple {_tuple_key(*TUPLE)}"
        )
    if engine_calls:
        failures.append(f"{engine_calls} engine call(s) made; the migration must be offline")
    return failures


def _counts_lines(label: str, found: Counts) -> list[str]:
    lines = [f"{label}: {found.rows} extraction rows"]
    lines += [f"  status {status:>13}: {n}" for status, n in sorted(found.by_status.items())]
    lines += [f"  tuple  {key}: {n}" for key, n in sorted(found.by_tuple.items())]
    lines.append(f"  docs now serving skills: {found.docs_serving_skills}")
    lines.append(f"  review share (active tuple): {found.review_share:.1%}")
    lines.append(f"  documents not migrated: {found.documents_not_migrated}")
    lines.append(f"  documents owed migration: {found.documents_owed_migration}")
    return lines


def plan(conn: Conn) -> str:
    """What a real run would do, and the surface it would do it to."""
    found = counts(conn)
    return "\n".join([
        f"plan: truncate the extraction surface and replay it from the archive "
        f"(`extract rebuild` semantics), deriving schema-3 rows under {_tuple_key(*TUPLE)}",
        "plan: zero engine calls; the extract writer lock is held for the whole replay",
        *_counts_lines("before", found),
    ])


def render(result: Result) -> str:
    before, after = result.before, result.after
    return "\n".join([
        *_counts_lines("before", before),
        *_counts_lines("after", after),
        f"replayed {result.attempts_replayed} attempts, {result.reviews_replayed} reviews",
        f"engine calls: {result.engine_calls}",
        "docs now serving skills: "
        f"{before.docs_serving_skills} -> {after.docs_serving_skills}",
        f"review share: {before.review_share:.1%} -> {after.review_share:.1%}",
    ])


def main(argv: list[str] | None = None) -> int:
    from jobhunter.archive import open_store
    from jobhunter.config import Settings
    from jobhunter.store import db

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="re-count only; non-zero exit if the migration is incomplete")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and the before counts; write nothing")
    # `extract rebuild` replays under `settings.l2_models`, and the accepted
    # globs decide which attempts settlement will even look at — a different
    # default here would report a different corpus from the one the CLI builds
    parser.add_argument("--models", default=None,
                        help="comma-separated model globs the replay accepts "
                             "(default: JOB_HUNTER_L2_MODELS, as `extract rebuild` uses)")
    args = parser.parse_args(argv)

    settings = Settings.load()
    conn = db.connect(settings.require_database_url())
    calls = EngineCalls()
    try:
        if args.dry_run:
            print(plan(conn))
            return 0
        if args.check:
            with calls.installed():
                found = counts(conn)
            failures = check_failures(found, engine_calls=calls.n)
            print("\n".join(_counts_lines("now", found)))
            for failure in failures:
                print(f"FAIL: {failure}")
            print("PASS" if not failures else "FAIL")
            return 1 if failures else 0
        if not db.try_lock(conn, db.EXTRACT_LOCK_KEY):
            print("extract lock held; a drain is running. Stop it and retry.", file=sys.stderr)
            return 5
        try:
            models = (
                tuple(args.models.split(",")) if args.models else tuple(settings.l2_models)
            )
            result = run_report(conn, open_store(settings.archive_url), models)
            conn.commit()
        except Exception:
            conn.rollback()  # the replay is all-or-nothing
            raise
        finally:
            with contextlib.suppress(Exception):
                db.unlock(conn, db.EXTRACT_LOCK_KEY)
        print(render(result))
        failures = check_failures(result.after, engine_calls=result.engine_calls)
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1 if failures else 0
    finally:
        with contextlib.suppress(Exception):
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
