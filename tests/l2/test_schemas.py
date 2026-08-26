import pytest

from jobhunter.l2.schemas import emit_schema, record_schema, validate_record
from tests.l2.conftest import minimal_record


def test_schemas_load() -> None:
    assert record_schema("1")["$defs"]["quote"]["required"] == ["text", "span", "occurrence"]
    assert "span" not in emit_schema("1")["$defs"]["quote"]["properties"]
    with pytest.raises(KeyError):
        record_schema("99")


def test_minimal_record_validates() -> None:
    assert validate_record(minimal_record(), "1") == []


def test_extra_property_rejected() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["claims"][0]["quote"]["extra"] = 1
    errors = validate_record(rec, "1")
    assert errors and "extra" in errors[0]


def test_empty_fragments_rejected() -> None:
    rec = minimal_record()
    rec["demand_profile"]["areas"][0]["mentions"] = [""]
    assert validate_record(rec, "1")

    rec2 = minimal_record()
    rec2["demand_profile"]["areas"][0]["claims"][0]["qualifiers"] = [""]
    assert validate_record(rec2, "1")


def test_schema_accessor_returns_copy() -> None:
    schema = record_schema("1")
    schema["$defs"]["quote"]["required"] = []
    assert record_schema("1")["$defs"]["quote"]["required"] == ["text", "span", "occurrence"]
    assert validate_record(minimal_record(), "1") == []


def test_pathlike_version_is_keyerror() -> None:
    import pytest as _pytest

    with _pytest.raises(KeyError):
        record_schema("1/record.schema.json")
