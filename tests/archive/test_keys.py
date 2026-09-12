from datetime import UTC, datetime

import pytest

from jobhunter.archive.keys import (
    attempt_key,
    attempts_prefix,
    blob_key,
    registry_key,
    version_key,
)


def test_blob_key_shards_by_first_two_hex() -> None:
    assert blob_key("abcd" * 16) == "blobs/sha256/ab/" + "abcd" * 16 + ".gz"


def test_attempt_key_layout() -> None:
    t = datetime(2026, 8, 18, 6, 1, 2, tzinfo=UTC)
    assert attempt_key("greenhouse", "anthropic", t) == (
        "attempts/greenhouse/anthropic/2026/08/18T060102Z.json"
    )


def test_attempts_prefix() -> None:
    assert attempts_prefix() == "attempts/"
    assert attempts_prefix("lever") == "attempts/lever/"
    assert attempts_prefix("lever", "palantir") == "attempts/lever/palantir/"


def test_registry_and_version_keys() -> None:
    assert registry_key("r" * 64) == "registry/" + "r" * 64 + ".json"
    assert version_key("ef" * 32) == "versions/ef/" + "ef" * 32 + ".html.gz"


def test_parse_attempt_key_roundtrip() -> None:
    from jobhunter.archive.keys import parse_attempt_key

    t = datetime(2026, 8, 18, 6, 1, 2, tzinfo=UTC)
    key = attempt_key("greenhouse", "anthropic", t)
    assert parse_attempt_key(key) == ("greenhouse", "anthropic", t)
    assert parse_attempt_key("blobs/sha256/ab/x.gz") is None
    assert parse_attempt_key("attempts/greenhouse/anthropic/garbage.json") is None


def test_x_attempt_key_roundtrip() -> None:
    from jobhunter.archive import keys

    at = datetime(2026, 8, 27, 6, 12, 4, tzinfo=UTC)
    key = keys.x_attempt_key(at, "9f3ab" + "0" * 59, 1, 2)
    assert key == "extractions/attempts/2026/08/27T061204Z-9f3ab0000000-s1a2.json.gz"
    assert keys.parse_x_attempt_key(key) == (at, "9f3ab0000000", 1, 2)
    assert keys.parse_x_attempt_key("attempts/greenhouse/x/2026/08/27T061204Z.json") is None
    assert keys.parse_x_attempt_key("extractions/prompts/demand-profile__v1.txt") is None


def test_x_attempt_keys_sort_by_time() -> None:
    from jobhunter.archive import keys

    earlier = keys.x_attempt_key(datetime(2026, 8, 27, 6, 0, 0, tzinfo=UTC), "a" * 64, 1, 1)
    later = keys.x_attempt_key(datetime(2026, 8, 27, 6, 0, 1, tzinfo=UTC), "0" * 64, 1, 1)
    assert earlier < later  # date-first: the catch-up scan lists by recency


def test_x_prompt_schema_review_keys() -> None:
    from jobhunter.archive import keys

    assert keys.x_prompt_key("demand-profile/v1") == "extractions/prompts/demand-profile__v1.txt"
    assert keys.x_schema_key("1") == "extractions/schemas/1.json"
    at = datetime(2026, 8, 27, 6, 12, 4, tzinfo=UTC)
    assert keys.x_review_key(at, "ab" * 32, "flag", 1) == (
        "extractions/reviews/2026/08/27T061204Z-abababababab-0001-flag.json"
    )
    later = keys.x_review_key(at, "ab" * 32, "accept", 2)
    assert keys.x_review_key(at, "ab" * 32, "flag", 1) < later  # key order == fold order


def test_x_audit_key_is_derived_from_the_attempt_it_audited() -> None:
    """`semantic-audit/v1` needs no table: settle probes the key the audited
    candidate's attempt key maps to, so live, catch-up and replay all find the
    same artifact (plan Architecture)."""
    from jobhunter.archive import keys

    at = datetime(2026, 8, 27, 6, 12, 4, tzinfo=UTC)
    attempt = keys.x_attempt_key(at, "9f3ab" + "0" * 59, 1, 2)
    audit = keys.x_audit_key(attempt)
    assert audit == "extractions/audits/2026/08/27T061204Z-9f3ab0000000-s1a2.json.gz"
    # a separate namespace: an audit artifact must never be listed, folded or
    # counted as an extraction attempt (spec §5: the phases are distinct)
    assert not audit.startswith(keys.X_ATTEMPTS_PREFIX)
    assert keys.parse_x_attempt_key(audit) is None
    # one audit per candidate: distinct attempts never share an audit key
    other = keys.x_audit_key(keys.x_attempt_key(at, "9f3ab" + "0" * 59, 2, 2))
    assert other != audit
    assert keys.x_audit_key(attempt) == audit  # deterministic


def test_x_audit_key_rejects_non_attempt_keys() -> None:
    """Never silently map garbage: a derived key that is not one-to-one with a
    real attempt would let settle read some other document's audit."""
    from jobhunter.archive import keys

    at = datetime(2026, 8, 27, 6, 12, 4, tzinfo=UTC)
    attempt = keys.x_attempt_key(at, "9f3ab" + "0" * 59, 1, 2)
    bad = [
        "",
        "blobs/sha256/ab/x.gz",
        attempt_key("greenhouse", "anthropic", at),  # a FETCH attempt, not an extraction
        keys.x_review_key(at, "ab" * 32, "flag", 1),
        keys.x_prompt_key("demand-profile/v9"),
        "extractions/attempts/garbage.json.gz",
        "extractions/attempts/2026/08/27T061204Z-9f3ab0000000-s1a2.json",  # ungzipped
        keys.x_audit_key(attempt),  # an audit key is not an attempt key
    ]
    for key in bad:
        with pytest.raises(ValueError):
            keys.x_audit_key(key)
