from jobhunter.l2.v2.project import mention_rows


def test_rows_carry_statement_importance_not_area(v2_record_with_mentions) -> None:
    rows = mention_rows(v2_record_with_mentions, include_ineligible=True)
    by_key = {r["normalized_key"]: r for r in rows}
    # the C04 shape: a preferred certification next to a required degree in the
    # same presentation area must project as preferred
    assert by_key["cpa"]["importance"] == "preferred"
    assert by_key["cpa"]["statement_id"] == "s_cert"


def test_ineligible_records_project_nothing_by_default(v2_record_with_mentions) -> None:
    assert mention_rows(v2_record_with_mentions) == []
