from __future__ import annotations

import gzip
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from jobhunter.archive.base import ArchiveStore
from jobhunter.archive.keys import attempt_key, blob_key, registry_key
from jobhunter.archive.manifests import write_manifest
from jobhunter.hashing import sha256_hex
from jobhunter.models import AttemptManifest, Board, DetailAttempt, PostingVersion
from jobhunter.registry import Registry
from jobhunter.sources.base import (
    EnvelopeError,
    ListPage,
    ListRow,
    NormalizeError,
    RequestSpec,
    load_json,
)


def write_registry(store: ArchiveStore, boards: Iterable[Board]) -> str:
    ordered = tuple(sorted(boards, key=lambda b: (b.source, b.board)))
    reg = Registry(boards=ordered, revision="")
    snap = reg.snapshot_json()
    rev = sha256_hex(snap)
    store.put(registry_key(rev), snap)
    return rev


def gh_record(id_: int | str, title: str, content_html: str, **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "id": id_, "title": title, "company_name": "Anthropic",
        "absolute_url": f"https://job-boards.greenhouse.io/anthropic/jobs/{id_}",
        "location": {"name": "SF"}, "offices": [], "departments": [{"name": "Eng"}],
        "first_published": "2026-04-14T06:00:34-04:00", "updated_at": "2026-08-03T18:25:22-04:00",
        "content": content_html.replace("<", "&lt;").replace(">", "&gt;"),
    }
    rec.update(extra)
    return rec


def lv_record(id_: str, text: str, opening: str, **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "id": id_, "text": text, "categories": {"commitment": "Full-time", "location": "NYC",
                                                 "team": "Eng", "allLocations": ["NYC"]},
        "workplaceType": "hybrid", "createdAt": 1711403416463, "opening": opening,
        "descriptionBody": "", "additional": "", "lists": [],
        "hostedUrl": f"https://jobs.lever.co/palantir/{id_}",
        "applyUrl": f"https://jobs.lever.co/palantir/{id_}/apply",
    }
    rec.update(extra)
    return rec


def ab_record(id_: str, title: str, description_html: str, **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "id": id_, "title": title, "department": "Eng", "team": "Web",
        "employmentType": "FullTime", "location": "NYC", "secondaryLocations": [],
        "isRemote": False, "isListed": True, "workplaceType": "Hybrid",
        "publishedAt": "2026-03-09T17:44:00.817+00:00",
        "jobUrl": f"https://jobs.ashbyhq.com/ramp/{id_}",
        "applyUrl": f"https://jobs.ashbyhq.com/ramp/{id_}/application",
        "descriptionHtml": description_html, "compensation": None,
    }
    rec.update(extra)
    return rec


def board_payload(source: str, records: list[Any]) -> bytes:
    if source == "greenhouse":
        obj: Any = {"jobs": records, "meta": {"total": len(records)}}
    elif source == "lever":
        obj = records
    elif source == "ashby":
        obj = {"apiVersion": "v0.1", "jobs": records}
    else:
        raise ValueError(source)
    return json.dumps(obj).encode("utf-8")


def make_manifest(
    store: ArchiveStore,
    source: str,
    board: str,
    started_at: datetime,
    body: bytes | None,
    *,
    run_id: str = "r",
    registry_revision: str = "rev",
    transport: str = "ok",
    http_status: int | None = 200,
    adapter_version: str | None = None,
) -> AttemptManifest:
    sha: str | None = None
    if body is not None and transport == "ok":
        sha = sha256_hex(body)
        store.put(blob_key(sha), gzip.compress(body, mtime=0))
    m = AttemptManifest(
        attempt_id=attempt_key(source, board, started_at), run_id=run_id, source=source,
        board=board, started_at=started_at, finished_at=started_at, url="u",
        http_status=http_status, transport=transport, blob_sha256=sha,
        payload_bytes=len(body or b""), record_count=None,
        adapter_version=adapter_version or f"{source}/1", registry_revision=registry_revision,
        cli_version="test", error=None if transport == "ok" else "boom",
    )
    write_manifest(store, m)
    return m


# ---- two-phase (list + detail) boards, spec 2026-09-04 §3.2/§3.4 -------------


class FakeTwoPhase:
    """A `TwoPhaseSource` with no I/O, mirroring the fake in tests/test_fetch.py.

    The store tests need it for the other direction: replaying archived list pages and
    detail bodies back into presence rows and versions. A row whose title is blank fails
    to normalise, which is how a detail-side `NormalizeError` is exercised.
    """

    name = "workday"
    adapter_version = "fake/1"

    def list_url(self, board: Board, offset: int) -> RequestSpec:
        return RequestSpec(f"https://wd.example/{board.board}/jobs", "POST", {"offset": offset})

    def parse_list(self, body: bytes) -> ListPage:
        data = load_json(body)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise EnvelopeError("no jobs array")
        rows = tuple(
            ListRow(uid=str(j["id"]), detail_path=f"/job/{j['id']}", title=j.get("title"),
                    payload=j)
            for j in data["jobs"]
        )
        return ListPage(rows=rows, total=int(data.get("total", len(rows))))

    def detail_url(self, board: Board, row: ListRow) -> RequestSpec:
        return RequestSpec(f"https://wd.example{row.detail_path}")

    def normalize_detail(self, body: bytes, row: ListRow, board: Board) -> PostingVersion:
        d = load_json(body)
        title = (row.title or "").strip()
        if not title:
            raise NormalizeError("missing title")
        return PostingVersion(
            source=self.name, board=board.board, source_id=row.uid, title=title,
            company=board.company, locations=tuple(d.get("locations", ())), workplace_type=None,
            is_remote=None, department=None, team=None, employment_type=None, compensation=None,
            url=f"https://wd.example/{board.board}/job/{row.uid}", apply_url=None,
            source_created_at=None, source_updated_at=None, description_html=d["description"],
        )


def wd_row(uid: str, title: str | None = None) -> dict[str, Any]:
    return {"id": uid, "title": f"Engineer {uid}" if title is None else title}


def wd_detail(description_html: str, **extra: Any) -> bytes:
    return json.dumps({"description": description_html, **extra}).encode("utf-8")


def make_two_phase_manifest(
    store: ArchiveStore,
    board: str,
    started_at: datetime,
    rows: list[dict[str, Any]],
    details: dict[str, bytes] | None = None,
    *,
    source: str = "workday",
    run_id: str = "r",
    registry_revision: str = "rev",
    page_size: int = 10,
    transport: str = "ok",
    http_status: int | None = 200,
    error: str | None = None,
    failed_details: dict[str, str] | None = None,
) -> AttemptManifest:
    """One archived list+detail attempt: pages of `rows`, plus a detail body per uid.

    `blob_sha256` is null as `fetch.py` writes it; every blob is put before the manifest
    that names it, so a replay never sees a manifest whose bytes are missing.
    """
    page_blobs: list[str] = []
    if transport == "ok":
        pages = [rows[i:i + page_size] for i in range(0, len(rows), page_size)] or [[]]
        for page in pages:
            body = json.dumps({"total": len(rows), "jobs": page}).encode("utf-8")
            sha = sha256_hex(body)
            store.put(blob_key(sha), gzip.compress(body, mtime=0))
            page_blobs.append(sha)
    attempts: list[DetailAttempt] = []
    for uid, body in (details or {}).items():
        sha = sha256_hex(body)
        store.put(blob_key(sha), gzip.compress(body, mtime=0))
        attempts.append(DetailAttempt(uid, sha, 200, None))
    for uid, msg in (failed_details or {}).items():
        attempts.append(DetailAttempt(uid, None, 404, msg))
    m = AttemptManifest(
        attempt_id=attempt_key(source, board, started_at), run_id=run_id, source=source,
        board=board, started_at=started_at, finished_at=started_at, url="u",
        http_status=http_status, transport=transport, blob_sha256=None, payload_bytes=0,
        record_count=len(rows) if transport == "ok" else None, adapter_version="fake/1",
        registry_revision=registry_revision, cli_version="test",
        error=error if transport == "ok" else (error or "boom"),
        page_blobs=tuple(page_blobs), details=tuple(attempts),
    )
    write_manifest(store, m)
    return m
