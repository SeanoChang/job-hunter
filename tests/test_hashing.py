from dataclasses import replace
from datetime import UTC, datetime

from jobhunter.hashing import (
    VERSION_HASH_V,
    canonical_json,
    sha256_hex,
    version_fields,
    version_hash,
)
from jobhunter.models import Compensation, PostingVersion


def test_canonical_json_sorts_keys_and_is_compact() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'


def test_canonical_json_keeps_unicode() -> None:
    assert canonical_json({"t": "東京"}) == '{"t":"東京"}'.encode()


def test_sha256_hex_known_value() -> None:
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def _pv(**over: object) -> PostingVersion:
    base = PostingVersion(
        source="ashby", board="ramp", source_id="1", title="  Engineer ", company="Ramp",
        locations=("NYC", "Remote", "NYC"), workplace_type="Hybrid", is_remote=True,
        department="Eng", team=None, employment_type="full_time",
        compensation=Compensation(100.0, 200.0, "USD", "year"),
        url="https://a", apply_url="https://b",
        source_created_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_updated_at=datetime(2026, 2, 2, tzinfo=UTC),
        description_html="<p>Hello   \n world</p>",
    )
    return replace(base, **over)  # type: ignore[arg-type]


def test_version_fields_preparation() -> None:
    f = version_fields(_pv())
    assert f == {
        "title": "Engineer",
        "locations": ["NYC", "Remote"],
        "workplace_type": "hybrid",
        "is_remote": True,
        "department": "Eng",
        "team": None,
        "employment_type": "full_time",
        "compensation": {"min": 100.0, "max": 200.0, "currency": "USD", "interval": "year"},
        "description_html": "<p>Hello world</p>",
    }


def test_version_hash_is_stable_golden() -> None:
    assert VERSION_HASH_V == 1
    assert version_hash(_pv()) == version_hash(_pv())
    assert len(version_hash(_pv())) == 64
    # Golden: pin the value so an accidental change to the field list is caught.
    assert version_hash(_pv()) == (
        "0353853a77586da655d5aeb8cd106fd59710e32f3cdc50e2344ebc398e0c5de9"
    )


def test_excluded_fields_do_not_change_hash() -> None:
    h = version_hash(_pv())
    assert version_hash(_pv(url="https://z", apply_url=None)) == h
    assert version_hash(_pv(company="Other")) == h
    assert version_hash(_pv(source_updated_at=datetime(2030, 1, 1, tzinfo=UTC))) == h
    assert version_hash(_pv(source_created_at=None)) == h
    assert version_hash(_pv(locations=("Remote", "NYC"))) == h  # order-insensitive


def test_included_fields_change_hash() -> None:
    h = version_hash(_pv())
    assert version_hash(_pv(title="Engineer II")) != h
    assert version_hash(_pv(description_html="<p>Hello world!</p>")) != h
    assert version_hash(_pv(compensation=None)) != h
    assert version_hash(_pv(is_remote=None)) != h
