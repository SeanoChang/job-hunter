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
from jobhunter.models import AttemptManifest, Board
from jobhunter.registry import Registry


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
