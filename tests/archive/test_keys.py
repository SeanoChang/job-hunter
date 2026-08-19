from datetime import UTC, datetime

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
