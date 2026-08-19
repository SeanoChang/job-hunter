"""The one write path: archive manifest -> store (spec §5.4). One transaction per attempt."""

from __future__ import annotations

import gzip
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from psycopg.types.json import Jsonb

from jobhunter import markdown as md
from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.keys import blob_key, version_key
from jobhunter.hashing import VERSION_HASH_V, sha256_hex, version_hash
from jobhunter.models import AttemptManifest, Board, PostingVersion
from jobhunter.sources import get_source
from jobhunter.sources.base import EnvelopeError, NormalizeError
from jobhunter.store import db
from jobhunter.store.db import Conn
from jobhunter.store.panel import apply_snapshot, load_snapshot
from jobhunter.timeutil import iso, parse_iso


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
    parse_status: str  # ok | failed
    pv: PostingVersion | None
    source_updated_at: datetime | None


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
        if m.transport != "ok" or not m.blob_sha256:
            res = AttemptResult(m.attempt_id, "error")
            self._insert_attempt(m, "error", res, None, m.error)
            return res
        source = get_source(m.source)
        board = self._board(m)
        body = gunzip(self.store.get(blob_key(m.blob_sha256)))
        try:
            records = list(source.parse(body))
        except EnvelopeError as e:
            res = AttemptResult(m.attempt_id, "error")
            self._insert_attempt(m, "error", res, None, f"envelope: {e}")
            return res

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

        # phase 2: writes
        self._insert_attempt(m, res.health, res, prev_count, None)
        for s in seen.values():
            if s.pv is not None and s.version_hash is not None:
                if self._insert_version(m, s.pv, s.version_hash):
                    res.new_versions += 1
                if self._insert_document(s.pv, s.version_hash):
                    res.new_documents += 1
            self._presence(s, m, prev_any_id)
        self._transitions(seen, m, res)
        if res.health == "ok":
            self._reconcile(m, res)
        return res

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

    def _insert_version(self, m: AttemptManifest, pv: PostingVersion, vh: str) -> bool:
        cur = self.conn.execute(
            "INSERT INTO posting_versions (version_hash, version_hash_v, uid, source, board, "
            "source_id, title, company, locations, workplace_type, is_remote, department, team, "
            "employment_type, "
            "compensation, url, apply_url, source_created_at, first_seen_attempt) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (uid, version_hash) DO NOTHING",
            (vh, VERSION_HASH_V, pv.uid, pv.source, pv.board, pv.source_id, pv.title, pv.company,
             Jsonb(list(pv.locations)), pv.workplace_type, pv.is_remote, pv.department, pv.team,
             pv.employment_type,
             Jsonb({"min": pv.compensation.min, "max": pv.compensation.max,
                    "currency": pv.compensation.currency, "interval": pv.compensation.interval})
             if pv.compensation else None,
             pv.url, pv.apply_url, pv.source_created_at, m.attempt_id),
        )
        inserted = cur.rowcount == 1
        if inserted:
            self.store.put(
                version_key(vh), gzip.compress(pv.description_html.encode("utf-8"), mtime=0)
            )
        return inserted

    def _insert_document(self, pv: PostingVersion, vh: str) -> bool:
        exists = self.conn.execute(
            "SELECT 1 FROM documents WHERE version_hash = %s AND normalizer_version = %s",
            (vh, self.normalizer_version),
        ).fetchone()
        if exists is not None:
            return False  # conversion costs ~0.8 ms/record; never redo it on replays
        markdown = self.to_markdown(pv.description_html)
        dh = sha256_hex(markdown.encode("utf-8"))
        cur = self.conn.execute(
            "INSERT INTO documents (version_hash, normalizer_version, document_hash, markdown) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (version_hash, normalizer_version) DO NOTHING",
            (vh, self.normalizer_version, dh, markdown),
        )
        return cur.rowcount == 1

    def _presence(self, s: _Seen, m: AttemptManifest, prev_any_id: str | None) -> None:
        cur = self.conn.execute(
            "SELECT first_attempt, last_attempt, version_hash, parse_status FROM presence "
            "WHERE uid = %s ORDER BY last_at DESC, first_attempt DESC LIMIT 1",
            (s.uid,),
        ).fetchone()
        if (
            cur is not None
            and prev_any_id is not None
            and cur["last_attempt"] == prev_any_id
            and cur["version_hash"] == s.version_hash
            and cur["parse_status"] == s.parse_status
        ):
            self.conn.execute(
                "UPDATE presence SET last_attempt = %s, last_at = %s, runs = runs + 1 "
                "WHERE uid = %s AND first_attempt = %s",
                (m.attempt_id, m.started_at, s.uid, cur["first_attempt"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO presence (uid, version_hash, parse_status, first_attempt, "
                "last_attempt, first_at, last_at, runs) VALUES (%s,%s,%s,%s,%s,%s,%s,1)",
                (s.uid, s.version_hash, s.parse_status, m.attempt_id, m.attempt_id,
                 m.started_at, m.started_at),
            )

    def _transitions(
        self, seen: dict[str, _Seen], m: AttemptManifest, res: AttemptResult
    ) -> None:
        for s in seen.values():
            row = self.conn.execute(
                "SELECT status, current_version_hash FROM postings WHERE uid = %s FOR UPDATE",
                (s.uid,),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO postings (uid, source, board, source_id, status, "
                    "current_version_hash, version_count, reopen_count, first_seen_attempt, "
                    "first_seen_at, last_seen_attempt, last_seen_at, source_updated_at) "
                    "VALUES (%s,%s,%s,%s,'open',%s,%s,0,%s,%s,%s,%s,%s)",
                    (s.uid, m.source, m.board, s.source_id, s.version_hash,
                     1 if s.version_hash else 0, m.attempt_id, m.started_at, m.attempt_id,
                     m.started_at, s.source_updated_at),
                )
                self._event("opened", s.uid, m, None, s.version_hash)
                res.opened += 1
                continue
            cur_vh = row["current_version_hash"]
            version_changed = s.version_hash is not None and s.version_hash != cur_vh
            if row["status"] == "closed":
                self.conn.execute(
                    "UPDATE postings SET status = 'open', reopen_count = reopen_count + 1, "
                    "closed_lower_at = NULL, closed_upper_at = NULL, closed_by_attempt = NULL, "
                    "last_seen_attempt = %s, last_seen_at = %s, "
                    "current_version_hash = COALESCE(%s, current_version_hash), "
                    "version_count = version_count + %s, "
                    "source_updated_at = COALESCE(%s, source_updated_at) WHERE uid = %s",
                    (m.attempt_id, m.started_at, s.version_hash, 1 if version_changed else 0,
                     s.source_updated_at, s.uid),
                )
                self._event("reopened", s.uid, m, cur_vh, s.version_hash or cur_vh)
                res.reopened += 1
            elif version_changed:
                self.conn.execute(
                    "UPDATE postings SET current_version_hash = %s, "
                    "version_count = version_count + 1, "
                    "last_seen_attempt = %s, last_seen_at = %s, "
                    "source_updated_at = COALESCE(%s, source_updated_at) WHERE uid = %s",
                    (s.version_hash, m.attempt_id, m.started_at, s.source_updated_at, s.uid),
                )
                self._event("changed", s.uid, m, cur_vh, s.version_hash)
                res.changed += 1
            else:
                self.conn.execute(
                    "UPDATE postings SET last_seen_attempt = %s, last_seen_at = %s, "
                    "source_updated_at = COALESCE(%s, source_updated_at) WHERE uid = %s",
                    (m.attempt_id, m.started_at, s.source_updated_at, s.uid),
                )

    def _reconcile(self, m: AttemptManifest, res: AttemptResult) -> None:
        rows = self.conn.execute(
            "UPDATE postings SET status = 'closed', closed_lower_at = last_seen_at, "
            "closed_upper_at = %s, closed_by_attempt = %s "
            "WHERE source = %s AND board = %s AND status = 'open' "
            "AND uid NOT IN (SELECT uid FROM presence WHERE last_attempt = %s) "
            "RETURNING uid, current_version_hash, closed_lower_at, closed_upper_at",
            (m.started_at, m.attempt_id, m.source, m.board, m.attempt_id),
        ).fetchall()
        for r in sorted(rows, key=lambda r: str(r["uid"])):
            self.conn.execute(
                "INSERT INTO posting_events (uid, kind, attempt_id, at, from_version, to_version, "
                "closed_lower_at, closed_upper_at) VALUES (%s,'closed',%s,%s,%s,NULL,%s,%s)",
                (r["uid"], m.attempt_id, m.started_at, r["current_version_hash"],
                 r["closed_lower_at"], r["closed_upper_at"]),
            )
            res.closed += 1

    def _event(
        self, kind: str, uid: str, m: AttemptManifest, from_v: str | None, to_v: str | None
    ) -> None:
        self.conn.execute(
            "INSERT INTO posting_events (uid, kind, attempt_id, at, from_version, to_version) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (uid, kind, m.attempt_id, m.started_at, from_v, to_v),
        )

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


def _prefix(source: str) -> str:
    from jobhunter.models import SOURCE_PREFIX

    return SOURCE_PREFIX[source]
