"""Identity computation. The single owner of canonical serialisation and hashing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # `jobhunter.models` imports this module; keep the runtime edge one-way.
    from jobhunter.models import PostingVersion

VERSION_HASH_V = 1
_WS = re.compile(r"\s+")


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version_fields(pv: PostingVersion) -> dict[str, Any]:
    """The employer-visible fields that define a posting version (spec §5.1), prepared."""
    return {
        "title": pv.title.strip(),
        "locations": sorted({s.strip() for s in pv.locations if s and s.strip()}),
        "workplace_type": pv.workplace_type.strip().lower() if pv.workplace_type else None,
        "is_remote": pv.is_remote,
        "department": pv.department.strip() if pv.department else None,
        "team": pv.team.strip() if pv.team else None,
        "employment_type": pv.employment_type.strip() if pv.employment_type else None,
        "compensation": asdict(pv.compensation) if pv.compensation else None,
        "description_html": _WS.sub(" ", pv.description_html).strip(),
    }


def version_hash(pv: PostingVersion) -> str:
    return sha256_hex(canonical_json(version_fields(pv)))
