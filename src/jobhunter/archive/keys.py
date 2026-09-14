"""Archive key layout (spec §5.2). Only place that knows the paths."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from jobhunter.timeutil import iso

ATTEMPTS_PREFIX = "attempts/"
_ATTEMPT_KEY_RE = re.compile(
    r"^attempts/([^/]+)/([^/]+)/(\d{4})/(\d{2})/(\d{2})T(\d{2})(\d{2})(\d{2})Z\.json$"
)


def blob_key(sha256: str) -> str:
    return f"blobs/sha256/{sha256[:2]}/{sha256}.gz"


def attempt_key(source: str, board: str, started_at: datetime) -> str:
    ts = iso(started_at)  # 2026-08-18T06:01:02Z
    y, m, rest = ts[0:4], ts[5:7], ts[8:]
    d, hms = rest[0:2], rest[3:11].replace(":", "")
    return f"{ATTEMPTS_PREFIX}{source}/{board}/{y}/{m}/{d}T{hms}Z.json"


def parse_attempt_key(key: str) -> tuple[str, str, datetime] | None:
    """Recover (source, board, started_at) from a manifest key; None for non-attempt keys.

    The key encodes everything the replay watermark needs, so callers can filter
    without fetching the manifest body (R2 GET per manifest was the daily cost).
    """
    m = _ATTEMPT_KEY_RE.match(key)
    if not m:
        return None
    source, board, y, mo, d, hh, mm, ss = m.groups()
    return source, board, datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss), tzinfo=UTC)


def attempts_prefix(source: str | None = None, board: str | None = None) -> str:
    p = ATTEMPTS_PREFIX
    if source:
        p += f"{source}/"
        if board:
            p += f"{board}/"
    return p


def registry_key(revision: str) -> str:
    return f"registry/{revision}.json"


def version_key(version_hash: str) -> str:
    return f"versions/{version_hash[:2]}/{version_hash}.html.gz"


# -- extraction layer (harness spec §4.2): date-first so the catch-up scan can
# list "keys newer than the watermark" with start_after; the document hash is
# inside the leaf name, not the path.

X_PREFIX = "extractions/"
X_ATTEMPTS_PREFIX = "extractions/attempts/"
X_REVIEWS_PREFIX = "extractions/reviews/"
_X_ATTEMPT_KEY_RE = re.compile(
    r"^extractions/attempts/(\d{4})/(\d{2})/(\d{2})T(\d{2})(\d{2})(\d{2})Z"
    r"-([0-9a-f]{12})-s(\d+)a(\d+)\.json\.gz$"
)


def x_prompt_key(prompt_version: str) -> str:
    return f"{X_PREFIX}prompts/{prompt_version.replace('/', '__')}.txt"


def x_schema_key(schema_version: str) -> str:
    return f"{X_PREFIX}schemas/{schema_version}.json"


def _x_stamp(at: datetime) -> str:
    ts = iso(at)  # 2026-08-27T06:12:04Z
    return f"{ts[0:4]}/{ts[5:7]}/{ts[8:10]}T{ts[11:19].replace(':', '')}Z"


def x_attempt_key(started_at: datetime, document_hash: str, slot: int, attempt_no: int) -> str:
    leaf = f"{_x_stamp(started_at)}-{document_hash[:12]}-s{slot}a{attempt_no}.json.gz"
    return X_ATTEMPTS_PREFIX + leaf


def parse_x_attempt_key(key: str) -> tuple[datetime, str, int, int] | None:
    m = _X_ATTEMPT_KEY_RE.match(key)
    if not m:
        return None
    y, mo, d, hh, mm, ss, dochash12, slot, no = m.groups()
    at = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss), tzinfo=UTC)
    return at, dochash12, int(slot), int(no)


# The semantic audit (spec §4) is a phase of its own: its artifact is keyed by
# the candidate attempt it audited, in a separate namespace. Deriving the key
# instead of recording it is what lets settlement fold identically live, in the
# catch-up scan and in replay — one `exists` probe, no table, no migration.
X_AUDITS_PREFIX = "extractions/audits/"


def x_audit_key(attempt_key: str) -> str:
    """The audit artifact for one extraction attempt's candidate.

    `extractions/attempts/<stamp>-<doc12>-s<slot>a<no>.json.gz`
    -> `extractions/audits/<stamp>-<doc12>-s<slot>a<no>.json.gz`

    One-to-one with the attempt, and never under `X_ATTEMPTS_PREFIX`: spec §5
    keeps the phases distinct, so an audit artifact must not be listed, folded
    or counted as an extraction sample. A key that is not an extraction attempt
    raises — mapping garbage would point settlement at another document's audit.
    """
    if parse_x_attempt_key(attempt_key) is None:
        raise ValueError(
            f"not an extraction attempt key: {attempt_key!r}; an audit artifact is keyed "
            "by the candidate attempt it audited"
        )
    return X_AUDITS_PREFIX + attempt_key[len(X_ATTEMPTS_PREFIX) :]


# The bounded repair round (spec §4 Repair) is the third phase, and it gets the
# third namespace for the audit's reasons: one artifact per candidate attempt,
# derived rather than recorded, and never listed as an extraction.
X_REPAIRS_PREFIX = "extractions/repairs/"


def x_repair_key(attempt_key: str) -> str:
    """The `semantic-repair/v1` artifact for one extraction attempt's candidate.

    `extractions/attempts/<stamp>-<doc12>-s<slot>a<no>.json.gz`
    -> `extractions/repairs/<stamp>-<doc12>-s<slot>a<no>.json.gz`

    Write-once, and that is the one-round cap itself (spec §5: one repair round
    per candidate): a key that already exists is a round already taken, whatever
    it produced. A key that is not an extraction attempt raises — a repaired
    candidate belongs to exactly one base candidate, and mapping garbage would
    attach it to another document's.
    """
    if parse_x_attempt_key(attempt_key) is None:
        raise ValueError(
            f"not an extraction attempt key: {attempt_key!r}; a repair artifact is keyed "
            "by the candidate attempt it repaired"
        )
    return X_REPAIRS_PREFIX + attempt_key[len(X_ATTEMPTS_PREFIX) :]


X_CONSOLIDATION_PREFIX = "extractions/consolidation/"


def x_consolidation_key(at: datetime) -> str:
    """One append-only artifact per consolidation run (harness spec §2)."""
    return f"{X_CONSOLIDATION_PREFIX}{_x_stamp(at)}.json"


def x_review_key(at: datetime, document_hash: str, verb: str, seq: int) -> str:
    # seq is the per-document review ordinal: utcnow() is second-granular, so
    # same-second verbs need an order the fold can reproduce from the event
    # alone (key sort == seq order == fold order, live and in replay)
    return f"{X_PREFIX}reviews/{_x_stamp(at)}-{document_hash[:12]}-{seq:04d}-{verb}.json"
