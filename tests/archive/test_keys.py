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
    audit = keys.x_audit_key(attempt, None)
    assert audit == "extractions/audits/2026/08/27T061204Z-9f3ab0000000-s1a2.json.gz"
    # a separate namespace: an audit artifact must never be listed, folded or
    # counted as an extraction attempt (spec §5: the phases are distinct)
    assert not audit.startswith(keys.X_ATTEMPTS_PREFIX)
    assert keys.parse_x_attempt_key(audit) is None
    # one audit per candidate: distinct attempts never share an audit key
    other = keys.x_audit_key(keys.x_attempt_key(at, "9f3ab" + "0" * 59, 2, 2), None)
    assert other != audit
    assert keys.x_audit_key(attempt, None) == audit  # deterministic


def test_x_audit_key_carries_the_audit_version_that_wrote_it() -> None:
    """An `AUDIT_VERSION` bump has to be re-auditable, and the archive is
    write-once, so the version is part of the KEY.

    A reader derives the key of the version it wants: an artifact of an older
    version is simply not at it — "audit owed", never "audit done" — and the
    older artifact stays exactly where it was written. The bare key is a
    `semantic-audit/v2` artifact by definition: the phase shipped unversioned
    keys through v2, and those keys stay readable as what they are.
    """
    from jobhunter.archive import keys

    at = datetime(2026, 8, 27, 6, 12, 4, tzinfo=UTC)
    attempt = keys.x_attempt_key(at, "9f3ab" + "0" * 59, 1, 2)
    legacy = keys.x_audit_key(attempt, None)
    assert keys.x_audit_key(attempt, keys.LEGACY_AUDIT_VERSION) == legacy
    v3 = keys.x_audit_key(attempt, "semantic-audit/v3")
    assert v3 == "extractions/audits/2026/08/27T061204Z-9f3ab0000000-s1a2.a3.json.gz"
    assert v3 != legacy
    assert keys.x_audit_key(attempt, "semantic-audit/v3") == v3  # deterministic
    # versions never collide with each other, and never with the legacy key
    assert len({legacy, v3, keys.x_audit_key(attempt, "semantic-audit/v10")}) == 3
    # still the audit namespace, still never an extraction sample
    assert v3.startswith(keys.X_AUDITS_PREFIX)
    assert keys.parse_x_attempt_key(v3) is None
    # a version this cannot spell must never quietly become the legacy key: a
    # v2 artifact and a v4 one would then share a write-once key
    for unspellable in ("", "   ", "semantic-audit/", "///"):
        with pytest.raises(ValueError):
            keys.x_audit_key(attempt, unspellable)


def test_x_audit_key_refuses_a_version_its_segment_cannot_spell_apart() -> None:
    """The guard is two-sided or it is not a guard.

    The segment is short because the name already carries a stamp, a hash and a
    slot — but a short spelling only stays safe while every version it accepts
    maps to a segment of its own. A tail it had to normalise (`v2.1` -> `a21`,
    which is `semantic-audit/v21`'s segment) and a family it drops (`x/v3` ->
    `a3`, which is `semantic-audit/v3`'s) both land two versions on one
    write-once key — the exact collision the segment exists to prevent. Neither
    is reachable today, and neither may become reachable by accident: a version
    the spelling cannot tell apart raises, so extending the family or the
    numbering is a deliberate change to this module.
    """
    from jobhunter.archive import keys

    at = datetime(2026, 8, 27, 6, 12, 4, tzinfo=UTC)
    attempt = keys.x_attempt_key(at, "9f3ab" + "0" * 59, 1, 2)
    # the numbering the segment does spell, and the only one
    assert keys.x_audit_key(attempt, "semantic-audit/v21").endswith(".a21.json.gz")
    for ambiguous in ("semantic-audit/v2.1", "semantic-audit/v3-rc1", "semantic-audit/v3b",
                      "semantic-audit/v 3", "semantic-audit/V3"):
        with pytest.raises(ValueError):
            keys.x_audit_key(attempt, ambiguous)
    # a family rename is a new namespace, not a silent alias of this one
    for renamed in ("omission-audit/v3", "audit/v3", "semantic-audit/extra/v3", "v3"):
        with pytest.raises(ValueError):
            keys.x_audit_key(attempt, renamed)


def test_x_repair_key_sits_beside_the_audit_of_the_same_candidate() -> None:
    """`semantic-repair/v1` is a phase of its own (spec §5: the archived
    extract/audit/repair phases are distinct), so its artifact takes its own
    namespace and is derived from the candidate attempt it repaired — the one
    thing live settlement, the catch-up scan and replay all hold."""
    from jobhunter.archive import keys

    at = datetime(2026, 8, 27, 6, 12, 4, tzinfo=UTC)
    attempt = keys.x_attempt_key(at, "9f3ab" + "0" * 59, 1, 2)
    repair = keys.x_repair_key(attempt)
    assert repair == "extractions/repairs/2026/08/27T061204Z-9f3ab0000000-s1a2.json.gz"
    assert repair != keys.x_audit_key(attempt, None)
    # never an attempt: a repaired candidate is not an extraction sample
    assert not repair.startswith(keys.X_ATTEMPTS_PREFIX)
    assert keys.parse_x_attempt_key(repair) is None
    assert keys.x_repair_key(attempt) == repair  # deterministic


def test_x_repair_key_rejects_non_attempt_keys() -> None:
    from jobhunter.archive import keys

    at = datetime(2026, 8, 27, 6, 12, 4, tzinfo=UTC)
    attempt = keys.x_attempt_key(at, "9f3ab" + "0" * 59, 1, 2)
    for key in ("", "blobs/sha256/ab/x.gz", keys.x_audit_key(attempt, None),
                keys.x_repair_key(attempt), "extractions/attempts/garbage.json.gz"):
        with pytest.raises(ValueError):
            keys.x_repair_key(key)


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
        keys.x_audit_key(attempt, None),  # an audit key is not an attempt key
        keys.x_audit_key(attempt, "semantic-audit/v3"),
    ]
    for key in bad:
        with pytest.raises(ValueError):
            keys.x_audit_key(key, None)
        with pytest.raises(ValueError):
            keys.x_audit_key(key, "semantic-audit/v3")
