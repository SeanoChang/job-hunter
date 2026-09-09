from jobhunter.l2.schemas import emit_schema
from jobhunter.l2.v2 import types


def test_enums_match_schema_2() -> None:
    defs = emit_schema("2")["$defs"]
    assert set(types.STATEMENT_KINDS) == set(defs["statement"]["properties"]["kind"]["enum"])
    assert set(types.IMPORTANCE) == set(defs["importance"]["enum"])
    assert set(types.OPERATORS) == set(defs["group"]["properties"]["operator"]["enum"])
    assert set(types.FAMILIES) == set(defs["fact_entry"]["properties"]["family"]["enum"])
    assert set(types.MENTION_ROLES) == set(defs["mention"]["properties"]["role"]["enum"])
    assert None not in types.PROFICIENCY  # the null lives at the field, not the enum


def test_block_is_frozen() -> None:
    b = types.Block(id="b000001", text="hello", span=(0, 5))
    assert b.span == (0, 5)
