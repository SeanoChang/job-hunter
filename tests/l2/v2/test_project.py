from typing import Any

from jobhunter.l2.v2.project import mention_rows
from jobhunter.l2.v2.types import NO_IMPORTANCE


def test_rows_carry_statement_importance_not_area(v2_record_with_mentions) -> None:
    rows = mention_rows(v2_record_with_mentions, include_ineligible=True)
    by_key = {r["normalized_key"]: r for r in rows}
    # the C04 shape: a preferred certification next to a required degree in the
    # same presentation area must project as preferred
    assert by_key["cpa"]["importance"] == "preferred"
    assert by_key["cpa"]["statement_id"] == "s_cert"


def test_ineligible_records_project_nothing_by_default(v2_record_with_mentions) -> None:
    assert mention_rows(v2_record_with_mentions) == []


# --- schema 3: no verdicts to read (parsing contract v3 §2.1) ---------------


def test_schema_3_rows_never_index_a_verdict(v3_record_with_mentions) -> None:
    """A schema-3 statement has no `importance` key at all, so a projection
    that indexes one raises rather than projecting. The sentinel is v1's own
    word for "named by the posting, not demanded by it", which is exactly what
    a statement with no verdict is."""
    statements: list[dict[str, Any]] = v3_record_with_mentions["statements"]
    assert all("importance" not in s and "proficiency" not in s for s in statements)
    rows = mention_rows(v3_record_with_mentions, include_ineligible=True)
    by_key = {r["normalized_key"]: r for r in rows}
    assert by_key["cpa"]["importance"] == NO_IMPORTANCE
    assert by_key["cpa"]["statement_id"] == "s_cert"


def test_schema_2_rows_keep_every_key_they_had(v2_record_with_mentions) -> None:
    """The regression guard on the schema-3 work: schema 2 is a shipped corpus
    partition and its rows are a storage contract."""
    rows = mention_rows(v2_record_with_mentions, include_ineligible=True)
    assert [set(r) for r in rows] == [
        {"mention_id", "statement_id", "surface", "normalized_key", "role", "kind",
         "subject", "importance", "polarity", "condition_ids", "group_ids"}
    ]
