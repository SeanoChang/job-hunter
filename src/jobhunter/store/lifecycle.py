"""The one write path: archive manifest -> store (spec §5.4). One transaction per attempt.

Set-based on purpose. Every attempt costs a bounded number of statements (a handful of
prefetches, then one statement per write group) instead of a per-record round trip: against
a hosted Postgres the round trip, not the work, is the cost. The transaction plus the
single-writer advisory lock make the read-then-compute-then-write shape race-free, so the
classification (new version, new document, presence extend, opened/changed/closed/reopened)
happens in Python against the prefetched state.
"""

from __future__ import annotations

import gzip
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from jobhunter import markdown as md
from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.keys import blob_key, version_key
from jobhunter.hashing import VERSION_HASH_V, sha256_hex, version_hash
from jobhunter.models import AttemptManifest, Board, PostingVersion
from jobhunter.sources import get_source, get_two_phase
from jobhunter.sources.base import EnvelopeError, ListRow, NormalizeError, TwoPhaseSource
from jobhunter.store import db
from jobhunter.store.db import Conn
from jobhunter.store.panel import apply_snapshot, load_snapshot
from jobhunter.timeutil import iso, parse_iso

PUT_WORKERS = 8
"""Parallel R2 puts per attempt. The archive is content-addressed, so the puts commute."""

PENDING_DETAIL = "pending_detail"
"""`presence.parse_status` for a uid a two-phase list has shown but whose detail has not
been fetched yet (spec 2026-09-04 §3.4). The posting is open and its presence interval
runs; it has no version and therefore no document, so it cannot enter the L2 queue —
that queue selects from `documents`. It leaves the status the moment a detail lands."""


class OutOfOrder(Exception):
    """A manifest older than the last ingested one; run `rebuild`."""


def gunzip(data: bytes) -> bytes:
    return gzip.decompress(data)


@dataclass(slots=True)
class AttemptResult:
    attempt_id: str
    health: str
    observed_count: int = 0
    parsed_count: int = 0
    failed_count: int = 0
    unidentifiable_count: int = 0
    pending_count: int = 0
    """Two-phase only: listed uids whose detail this attempt did not carry."""
    new_versions: int = 0
    new_documents: int = 0
    opened: int = 0
    changed: int = 0
    closed: int = 0
    reopened: int = 0
    warnings: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _Seen:
    uid: str
    source_id: str
    version_hash: str | None
    parse_status: str  # ok | failed | pending_detail
    pv: PostingVersion | None
    source_updated_at: datetime | None
    pending: bool = False
    """A two-phase list row this attempt carried no detail for: what it observed depends on
    what the store already holds, so `version_hash`/`parse_status` are settled in `_plan`."""


@dataclass(slots=True)
class _Writes:
    """Everything one attempt will write, grouped so each group costs one statement.

    Row order inside `events` is payload order and is part of the contract: `event_id` is an
    identity column, a rebuild must reproduce the incremental store row for row, and the
    single ordered INSERT below assigns the ids in list order.
    """

    versions: list[tuple[Any, ...]] = field(default_factory=list)
    puts: list[tuple[str, str]] = field(default_factory=list)  # (version_hash, description_html)
    documents: list[tuple[Any, ...]] = field(default_factory=list)
    presence_new: list[tuple[Any, ...]] = field(default_factory=list)
    presence_extend: list[tuple[Any, ...]] = field(default_factory=list)
    postings_new: list[tuple[Any, ...]] = field(default_factory=list)
    postings_touch: list[tuple[Any, ...]] = field(default_factory=list)
    postings_changed: list[tuple[Any, ...]] = field(default_factory=list)
    postings_reopened: list[tuple[Any, ...]] = field(default_factory=list)
    events: list[tuple[Any, ...]] = field(default_factory=list)


# ---- batched statements. Values travel as one array per column (`unnest`), never as a
# generated VALUES list: the row count then never touches Postgres' 65535 parameter ceiling.
_KNOWN_VERSIONS = "SELECT uid, version_hash FROM posting_versions WHERE version_hash = ANY(%s)"

_KNOWN_DOCUMENTS = (
    "SELECT version_hash FROM documents WHERE normalizer_version = %s AND version_hash = ANY(%s)"
)

_LATEST_PRESENCE = (
    "SELECT DISTINCT ON (uid) uid, first_attempt, last_attempt, version_hash, parse_status "
    "FROM presence WHERE uid = ANY(%s) ORDER BY uid, last_at DESC, first_attempt DESC"
)

_LOCK_POSTINGS = (
    "SELECT uid, status, current_version_hash FROM postings WHERE uid = ANY(%s) FOR UPDATE"
)

_INSERT_VERSIONS = """
INSERT INTO posting_versions (version_hash, version_hash_v, uid, source, board, source_id, title,
    company, locations, workplace_type, is_remote, department, team, employment_type,
    compensation, url, apply_url, source_created_at, first_seen_attempt)
SELECT * FROM unnest(%s::text[], %s::int[], %s::text[], %s::text[], %s::text[], %s::text[],
    %s::text[], %s::text[], %s::jsonb[], %s::text[], %s::bool[], %s::text[], %s::text[],
    %s::text[], %s::jsonb[], %s::text[], %s::text[], %s::timestamptz[], %s::text[])
ON CONFLICT (uid, version_hash) DO NOTHING
"""

_INSERT_DOCUMENTS = """
INSERT INTO documents (version_hash, normalizer_version, document_hash, markdown)
SELECT version_hash, %s, document_hash, markdown
FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(version_hash, document_hash, markdown)
ON CONFLICT (version_hash, normalizer_version) DO NOTHING
"""

_INSERT_PRESENCE = """
INSERT INTO presence (uid, version_hash, parse_status, first_attempt, last_attempt,
    first_at, last_at, runs)
SELECT uid, version_hash, parse_status, %s, %s, %s, %s, 1
FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(uid, version_hash, parse_status)
"""

_EXTEND_PRESENCE = """
UPDATE presence p SET last_attempt = %s, last_at = %s, runs = p.runs + 1
FROM unnest(%s::text[], %s::text[]) AS t(uid, first_attempt)
WHERE p.uid = t.uid AND p.first_attempt = t.first_attempt
"""

_INSERT_POSTINGS = """
INSERT INTO postings (uid, source, board, source_id, status, current_version_hash, version_count,
    reopen_count, first_seen_attempt, first_seen_at, last_seen_attempt, last_seen_at,
    source_updated_at)
SELECT uid, %s, %s, source_id, 'open', version_hash, version_count, 0, %s, %s, %s, %s,
    source_updated_at
FROM unnest(%s::text[], %s::text[], %s::text[], %s::int[], %s::timestamptz[])
    AS t(uid, source_id, version_hash, version_count, source_updated_at)
"""

_TOUCH_POSTINGS = """
UPDATE postings p SET last_seen_attempt = %s, last_seen_at = %s,
    source_updated_at = COALESCE(t.source_updated_at, p.source_updated_at)
FROM unnest(%s::text[], %s::timestamptz[]) AS t(uid, source_updated_at)
WHERE p.uid = t.uid
"""

_CHANGE_POSTINGS = """
UPDATE postings p SET current_version_hash = t.version_hash, version_count = p.version_count + 1,
    last_seen_attempt = %s, last_seen_at = %s,
    source_updated_at = COALESCE(t.source_updated_at, p.source_updated_at)
FROM unnest(%s::text[], %s::text[], %s::timestamptz[])
    AS t(uid, version_hash, source_updated_at)
WHERE p.uid = t.uid
"""

_REOPEN_POSTINGS = """
UPDATE postings p SET status = 'open', reopen_count = p.reopen_count + 1,
    closed_lower_at = NULL, closed_upper_at = NULL, closed_by_attempt = NULL,
    last_seen_attempt = %s, last_seen_at = %s,
    current_version_hash = COALESCE(t.version_hash, p.current_version_hash),
    version_count = p.version_count + t.bump,
    source_updated_at = COALESCE(t.source_updated_at, p.source_updated_at)
FROM unnest(%s::text[], %s::text[], %s::int[], %s::timestamptz[])
    AS t(uid, version_hash, bump, source_updated_at)
WHERE p.uid = t.uid
"""

_INSERT_EVENTS = """
INSERT INTO posting_events (uid, kind, attempt_id, at, from_version, to_version)
SELECT uid, kind, %s, %s, from_version, to_version
FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[])
    WITH ORDINALITY AS t(uid, kind, from_version, to_version, ord)
ORDER BY t.ord
"""

_CLOSE_POSTINGS = """
UPDATE postings SET status = 'closed', closed_lower_at = last_seen_at,
    closed_upper_at = %s, closed_by_attempt = %s
WHERE source = %s AND board = %s AND status = 'open'
  AND uid NOT IN (SELECT uid FROM presence WHERE last_attempt = %s)
RETURNING uid, current_version_hash, closed_lower_at, closed_upper_at
"""

_INSERT_CLOSED_EVENTS = """
INSERT INTO posting_events (uid, kind, attempt_id, at, from_version, to_version,
    closed_lower_at, closed_upper_at)
SELECT uid, 'closed', %s, %s, from_version, NULL, closed_lower_at, closed_upper_at
FROM unnest(%s::text[], %s::text[], %s::timestamptz[], %s::timestamptz[])
    WITH ORDINALITY AS t(uid, from_version, closed_lower_at, closed_upper_at, ord)
ORDER BY t.ord
"""


class Ingestor:
    def __init__(
        self,
        conn: Conn,
        store: ArchiveStore,
        *,
        drop_ratio: float = 0.5,
        normalizer_version: str = md.NORMALIZER_VERSION,
        to_markdown: Callable[[str], str] = md.to_markdown,
    ) -> None:
        self.conn = conn
        self.store = store
        self.drop_ratio = drop_ratio
        self.normalizer_version = normalizer_version
        self.to_markdown = to_markdown
        self._boards_by_rev: dict[str, dict[str, Board]] = {}

    # ---- registry / panel
    def _boards(self, revision: str) -> dict[str, Board]:
        if revision not in self._boards_by_rev:
            try:
                boards = load_snapshot(self.store, revision)
            except KeyError:
                boards = ()
            self._boards_by_rev[revision] = {b.key: b for b in boards}
        return self._boards_by_rev[revision]

    def _apply_registry_if_changed(self, m: AttemptManifest) -> None:
        if db.get_meta(self.conn, "last_registry_revision") == m.registry_revision:
            return
        boards = self._boards(m.registry_revision)
        if not boards:
            # Missing snapshot: leave the panel untouched AND leave the watermark unset so a
            # later attempt with the same revision applies it once the object exists.
            return
        apply_snapshot(self.conn, boards.values(), m.started_at, m.registry_revision)
        db.set_meta(self.conn, "last_registry_revision", m.registry_revision)

    def _board(self, m: AttemptManifest) -> Board:
        return self._boards(m.registry_revision).get(
            f"{m.source}:{m.board}", Board(company=m.board, source=m.source, board=m.board)
        )

    # ---- public
    def ingest(self, m: AttemptManifest) -> AttemptResult | None:
        with self.conn.transaction():
            if self.conn.execute(
                "SELECT 1 FROM fetch_attempts WHERE attempt_id = %s", (m.attempt_id,)
            ).fetchone():
                return None
            last_at = db.get_meta(self.conn, "last_ingested_at")
            if last_at and m.started_at < parse_iso(last_at):
                raise OutOfOrder(
                    f"{m.attempt_id} is older than last ingested {last_at}; run rebuild"
                )
            self._apply_registry_if_changed(m)
            result = self._ingest_inner(m)
            self._upsert_run(m.run_id)
            db.set_meta(self.conn, "last_ingested_attempt", m.attempt_id)
            db.set_meta(self.conn, "last_ingested_at", iso(m.started_at))
            return result

    # ---- steps
    def _ingest_inner(self, m: AttemptManifest) -> AttemptResult:
        if is_two_phase(m):
            return self._ingest_two_phase(m)
        return self._ingest_single_phase(m)

    def _error_attempt(self, m: AttemptManifest, error: str | None) -> AttemptResult:
        """An attempt we cannot read as an observation: provenance only, nothing derived."""
        res = AttemptResult(m.attempt_id, "error")
        self._insert_attempt(m, "error", res, None, error)
        return res

    def _ingest_single_phase(self, m: AttemptManifest) -> AttemptResult:
        if m.transport != "ok" or not m.blob_sha256:
            return self._error_attempt(m, m.error)
        source = get_source(m.source)
        board = self._board(m)
        body = gunzip(self.store.get(blob_key(m.blob_sha256)))
        try:
            records = list(source.parse(body))
        except EnvelopeError as e:
            return self._error_attempt(m, f"envelope: {e}")

        # phase 1: pure compute
        seen: dict[str, _Seen] = {}
        res = AttemptResult(m.attempt_id, "ok")
        for rec in records:
            if rec.source_id is None:
                res.unidentifiable_count += 1
                continue
            if rec.source_id in seen:
                res.warnings["duplicate_ids"] = res.warnings.get("duplicate_ids", 0) + 1
                continue
            try:
                pv = source.normalize(rec, board)
            except NormalizeError:
                uid = f"{_prefix(m.source)}:{m.board}:{rec.source_id}"
                seen[rec.source_id] = _Seen(uid, rec.source_id, None, "failed", None, None)
                res.failed_count += 1
                continue
            seen[rec.source_id] = _Seen(
                pv.uid, rec.source_id, version_hash(pv), "ok", pv, pv.source_updated_at
            )
            res.parsed_count += 1
        res.observed_count = len(seen)
        return self._finish(m, seen, res)

    # ---- two-phase (list + detail) attempts, spec 2026-09-04 §3.4
    def _ingest_two_phase(self, m: AttemptManifest) -> AttemptResult:
        """The LIST is the presence snapshot; each fetched detail is a version.

        Every uid the archived list pages name is present this attempt, with or without a
        detail this run — that is what keeps close-on-absence honest while the detail budget
        works through the board. A uid whose detail has landed carries the version it
        normalises to, exactly as a single-phase record does; a uid still owed one carries no
        version and presence records it as `pending_detail`.
        """
        if m.transport != "ok" or m.error is not None:
            # A list that was truncated (page cap), refused (blocked) or unparseable is not a
            # snapshot: reconciling against it would close every posting it never reached.
            return self._error_attempt(m, m.error)
        source = get_two_phase(m.source)
        if source is None:
            return self._error_attempt(m, f"no two-phase adapter for source {m.source!r}")
        board = self._board(m)
        try:
            rows = self._replay_list(m, source)
        except EnvelopeError as e:
            return self._error_attempt(m, f"envelope: {e}")

        blobs = {d.uid: d.blob_sha256 for d in m.details or () if d.blob_sha256 is not None}
        seen: dict[str, _Seen] = {}
        res = AttemptResult(m.attempt_id, "ok")
        for row in rows:
            if not row.uid:
                res.unidentifiable_count += 1
                continue
            uid = f"{_prefix(m.source)}:{m.board}:{row.uid}"
            sha = blobs.get(row.uid)
            if sha is None:  # detail not fetched this run, or its fetch failed
                seen[row.uid] = _Seen(uid, row.uid, None, PENDING_DETAIL, None, None, pending=True)
                res.pending_count += 1
                continue
            try:
                pv = source.normalize_detail(gunzip(self.store.get(blob_key(sha))), row, board)
            except (EnvelopeError, NormalizeError):
                seen[row.uid] = _Seen(uid, row.uid, None, "failed", None, None)
                res.failed_count += 1
                continue
            seen[row.uid] = _Seen(pv.uid, row.uid, version_hash(pv), "ok", pv, pv.source_updated_at)
            res.parsed_count += 1
        res.observed_count = len(seen)
        return self._finish(m, seen, res)

    def _replay_list(self, m: AttemptManifest, source: TwoPhaseSource) -> list[ListRow]:
        """The archived list pages in order, deduplicated as the fetcher deduplicated them:
        a uid repeated across pages (unstable pagination) is one posting, first occurrence
        wins. Details naming a uid the list does not are ignored — presence comes from the
        list alone."""
        rows: dict[str, ListRow] = {}
        for sha in m.page_blobs or ():
            for row in source.parse_list(gunzip(self.store.get(blob_key(sha)))).rows:
                rows.setdefault(row.uid, row)
        return list(rows.values())

    def _finish(
        self, m: AttemptManifest, seen: dict[str, _Seen], res: AttemptResult
    ) -> AttemptResult:
        # Two different "previous" attempts, on purpose:
        #  - prev (non-error) feeds the drop guard: it is the last attempt that said anything
        #    about the board's size;
        #  - prev_any (any health) feeds presence continuity: an interval may only be extended
        #    across consecutive attempts; an error attempt in between is a gap we did not observe.
        prev = self.conn.execute(
            "SELECT attempt_id, observed_count FROM fetch_attempts "
            "WHERE source = %s AND board = %s AND health <> 'error' "
            "ORDER BY started_at DESC, attempt_id DESC LIMIT 1",
            (m.source, m.board),
        ).fetchone()
        prev_any = self.conn.execute(
            "SELECT attempt_id FROM fetch_attempts WHERE source = %s AND board = %s "
            "ORDER BY started_at DESC, attempt_id DESC LIMIT 1",
            (m.source, m.board),
        ).fetchone()
        prev_any_id = prev_any["attempt_id"] if prev_any else None
        prev_count = int(prev["observed_count"]) if prev else None
        if prev_count is not None and res.observed_count < self.drop_ratio * prev_count:
            res.health = "suspect_drop"

        # phase 2: prefetch the state this attempt depends on, classify, then write in batches
        self._insert_attempt(m, res.health, res, prev_count, None)
        w = self._plan(seen, m, prev_any_id)
        res.new_versions = len(w.versions)
        res.new_documents = len(w.documents)
        res.opened = len(w.postings_new)
        res.changed = len(w.postings_changed)
        res.reopened = len(w.postings_reopened)
        # Before the writes and inside the transaction: an attempt that later fails leaves only
        # content-addressed objects behind, and Neon's transaction never waits on R2 latency.
        self._archive_versions(w.puts)
        self._write(w, m)
        if res.health == "ok":
            self._reconcile(m, res)
        return res

    # ---- planning: one read per fact, then pure Python classification
    def _plan(
        self, seen: dict[str, _Seen], m: AttemptManifest, prev_any_id: str | None
    ) -> _Writes:
        w = _Writes()
        if not seen:
            return w
        uids = [s.uid for s in seen.values()]
        hashes = list({s.version_hash for s in seen.values() if s.version_hash is not None})
        pairs: set[tuple[str, str]] = set()
        known_hashes: set[str] = set()
        known_docs: set[str] = set()
        if hashes:
            for row in self.conn.execute(_KNOWN_VERSIONS, (hashes,)).fetchall():
                pairs.add((str(row["uid"]), str(row["version_hash"])))
                known_hashes.add(str(row["version_hash"]))
            known_docs = {
                str(r["version_hash"])
                for r in self.conn.execute(
                    _KNOWN_DOCUMENTS, (self.normalizer_version, hashes)
                ).fetchall()
            }
        presence = {
            str(r["uid"]): r for r in self.conn.execute(_LATEST_PRESENCE, (uids,)).fetchall()
        }
        postings = {
            str(r["uid"]): r for r in self.conn.execute(_LOCK_POSTINGS, (uids,)).fetchall()
        }
        for s in seen.values():
            if s.pending:
                _resolve_pending(s, postings.get(s.uid))
            if s.pv is not None and s.version_hash is not None:
                self._plan_version(w, m, s, s.pv, s.version_hash, pairs, known_hashes, known_docs)
            self._plan_presence(w, s, presence.get(s.uid), prev_any_id)
            self._plan_transition(w, s, postings.get(s.uid))
        return w

    def _plan_version(
        self,
        w: _Writes,
        m: AttemptManifest,
        s: _Seen,
        pv: PostingVersion,
        vh: str,
        pairs: set[tuple[str, str]],
        known_hashes: set[str],
        known_docs: set[str],
    ) -> None:
        if (s.uid, vh) not in pairs:
            pairs.add((s.uid, vh))
            w.versions.append((
                vh, VERSION_HASH_V, pv.uid, pv.source, pv.board, pv.source_id, pv.title,
                pv.company, Jsonb(list(pv.locations)), pv.workplace_type, pv.is_remote,
                pv.department, pv.team, pv.employment_type,
                Jsonb({"min": pv.compensation.min, "max": pv.compensation.max,
                       "currency": pv.compensation.currency,
                       "interval": pv.compensation.interval}) if pv.compensation else None,
                pv.url, pv.apply_url, pv.source_created_at, m.attempt_id,
            ))
            if vh not in known_hashes:
                # Globally new content: the only case that owes the archive an object.
                known_hashes.add(vh)
                w.puts.append((vh, pv.description_html))
        if vh not in known_docs:
            known_docs.add(vh)
            markdown = self.to_markdown(pv.description_html)  # ~0.8 ms; never redone on replays
            w.documents.append((vh, sha256_hex(markdown.encode("utf-8")), markdown))

    def _plan_presence(
        self, w: _Writes, s: _Seen, cur: dict[str, Any] | None, prev_any_id: str | None
    ) -> None:
        if (
            cur is not None
            and prev_any_id is not None
            and cur["last_attempt"] == prev_any_id
            and cur["version_hash"] == s.version_hash
            and cur["parse_status"] == s.parse_status
        ):
            w.presence_extend.append((s.uid, cur["first_attempt"]))
        else:
            w.presence_new.append((s.uid, s.version_hash, s.parse_status))

    def _plan_transition(self, w: _Writes, s: _Seen, row: dict[str, Any] | None) -> None:
        if row is None:
            w.postings_new.append((
                s.uid, s.source_id, s.version_hash, 1 if s.version_hash else 0, s.source_updated_at
            ))
            w.events.append((s.uid, "opened", None, s.version_hash))
            return
        cur_vh = row["current_version_hash"]
        version_changed = s.version_hash is not None and s.version_hash != cur_vh
        if row["status"] == "closed":
            w.postings_reopened.append((
                s.uid, s.version_hash, 1 if version_changed else 0, s.source_updated_at
            ))
            w.events.append((s.uid, "reopened", cur_vh, s.version_hash or cur_vh))
        elif version_changed:
            w.postings_changed.append((s.uid, s.version_hash, s.source_updated_at))
            w.events.append((s.uid, "changed", cur_vh, s.version_hash))
        else:
            w.postings_touch.append((s.uid, s.source_updated_at))

    # ---- writing
    def _batch(self, query: str, rows: Sequence[tuple[Any, ...]], *scalars: Any) -> None:
        """One statement for the whole group: scalars first, then one array per column."""
        if not rows:
            return
        self.conn.execute(query, [*scalars, *(list(col) for col in zip(*rows, strict=True))])

    def _write(self, w: _Writes, m: AttemptManifest) -> None:
        at = (m.attempt_id, m.started_at)
        self._batch(_INSERT_VERSIONS, w.versions)
        self._batch(_INSERT_DOCUMENTS, w.documents, self.normalizer_version)
        self._batch(_INSERT_PRESENCE, w.presence_new, m.attempt_id, *at, m.started_at)
        self._batch(_EXTEND_PRESENCE, w.presence_extend, *at)
        self._batch(_INSERT_POSTINGS, w.postings_new, m.source, m.board, *at, *at)
        self._batch(_TOUCH_POSTINGS, w.postings_touch, *at)
        self._batch(_CHANGE_POSTINGS, w.postings_changed, *at)
        self._batch(_REOPEN_POSTINGS, w.postings_reopened, *at)
        self._batch(_INSERT_EVENTS, w.events, *at)

    def _archive_versions(self, puts: Sequence[tuple[str, str]]) -> None:
        if len(puts) <= 1:
            for vh, html in puts:
                self._put_version(vh, html)
            return
        with ThreadPoolExecutor(max_workers=PUT_WORKERS) as pool:
            futures = [pool.submit(self._put_version, vh, html) for vh, html in puts]
            for f in futures:
                f.result()  # a failed put must abort the attempt, not be swallowed

    def _put_version(self, vh: str, html: str) -> None:
        self.store.put(version_key(vh), gzip.compress(html.encode("utf-8"), mtime=0))

    def _insert_attempt(
        self,
        m: AttemptManifest,
        health: str,
        res: AttemptResult,
        prev_count: int | None,
        error: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO fetch_attempts (attempt_id, run_id, source, board, started_at, "
            "finished_at, http_status, transport, health, blob_sha256, payload_bytes, "
            "observed_count, parsed_count, failed_count, unidentifiable_count, "
            "prev_observed_count, adapter_version, "
            "registry_revision, cli_version, warnings, error) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (attempt_id) DO NOTHING",
            (m.attempt_id, m.run_id, m.source, m.board, m.started_at, m.finished_at, m.http_status,
             m.transport, health, m.blob_sha256, m.payload_bytes, res.observed_count,
             res.parsed_count, res.failed_count, res.unidentifiable_count, prev_count,
             m.adapter_version, m.registry_revision, m.cli_version,
             Jsonb(res.warnings) if res.warnings else None, error),
        )

    def _reconcile(self, m: AttemptManifest, res: AttemptResult) -> None:
        rows = self.conn.execute(
            _CLOSE_POSTINGS,
            (m.started_at, m.attempt_id, m.source, m.board, m.attempt_id),
        ).fetchall()
        closed = sorted(
            (
                (str(r["uid"]), r["current_version_hash"], r["closed_lower_at"],
                 r["closed_upper_at"])
                for r in rows
            ),
            key=lambda r: r[0],
        )
        self._batch(_INSERT_CLOSED_EVENTS, closed, m.attempt_id, m.started_at)
        res.closed = len(closed)

    def _upsert_run(self, run_id: str) -> None:
        self.conn.execute(
            "INSERT INTO runs (run_id, started_at, finished_at, cli_version, boards_total, "
            "boards_ok, boards_suspect, boards_error) "
            "SELECT run_id, min(started_at), max(finished_at), max(cli_version), count(*), "
            "count(*) FILTER (WHERE health = 'ok'), "
            "count(*) FILTER (WHERE health = 'suspect_drop'), "
            "count(*) FILTER (WHERE health = 'error') FROM fetch_attempts WHERE run_id = %s "
            "GROUP BY run_id "
            "ON CONFLICT (run_id) DO UPDATE SET started_at = EXCLUDED.started_at, "
            "finished_at = EXCLUDED.finished_at, cli_version = EXCLUDED.cli_version, "
            "boards_total = EXCLUDED.boards_total, boards_ok = EXCLUDED.boards_ok, "
            "boards_suspect = EXCLUDED.boards_suspect, boards_error = EXCLUDED.boards_error",
            (run_id,),
        )


def is_two_phase(m: AttemptManifest) -> bool:
    """A list+detail attempt: it carries list pages and detail fetches instead of one body.
    Single-phase manifests leave both fields absent, so the two shapes never blur."""
    return m.page_blobs is not None or m.details is not None


def _resolve_pending(s: _Seen, row: dict[str, Any] | None) -> None:
    """Settle what a detail-less list row observed, against the posting as it stands.

    Once a detail has landed, later list-only sightings observe that same version: saying
    `pending_detail` again would split the presence interval every run and claim the store
    has no text when it does. Only a posting still without a version is pending.
    """
    cur = row["current_version_hash"] if row is not None else None
    s.version_hash = str(cur) if cur else None
    s.parse_status = "ok" if cur else PENDING_DETAIL


def _prefix(source: str) -> str:
    from jobhunter.models import SOURCE_PREFIX

    return SOURCE_PREFIX[source]
