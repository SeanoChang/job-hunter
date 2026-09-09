import pytest

from jobhunter.l2.v2.facts import derive_date, derive_money


def m(cmp, lo, hi, cur, period):
    return {"comparison": cmp, "min_amount": lo, "max_amount": hi,
            "currency": cur, "period": period}


@pytest.mark.parametrize(
    ("value", "cmp_text", "cur_text", "period_text", "expected"),
    [
        # C02: the period lives in its own evidence; $ alone is no currency
        ("$210,300 - $273,400", None, None, "annually",
         m("range", "210300", "273400", None, "year")),
        ("$210,300 - $273,400", None, "USD", "Gross pay annually",
         m("range", "210300", "273400", "USD", "year")),
        # cents survive as decimal strings
        ("$169,100.00 - $233,200.00", None, "USD", None,
         m("range", "169100.00", "233200.00", "USD", None)),
        ("€71.000 to €95.000", None, None, "per annum",
         m("range", "71000", "95000", "EUR", "year")),
        ("£55,000", None, None, "per hour", m("unstated", "55000", "55000", "GBP", "hour")),
        ("$150,000", "up to", None, None, m("lte", None, "150000", None, None)),
        ("$130 - $150K", None, None, None, m("range", "130000", "150000", None, None)),
    ],
)
def test_money(value, cmp_text, cur_text, period_text, expected) -> None:
    assert derive_money(value, cmp_text, cur_text, period_text) == expected


@pytest.mark.parametrize(
    ("value", "cmp_text"),
    [
        ("$106,147 - $228, 781", None),   # C06's malformed upper bound: unparsed, kept
        ("£100,000 - €120,000", None),    # mixed currencies is not a range
        ("$150,000 - $120,000", None),    # inverted
        ("competitive salary", None),     # no amount
    ],
)
def test_money_unparseable_is_none(value, cmp_text) -> None:
    assert derive_money(value, cmp_text, None, None) is None


def test_dates() -> None:
    assert derive_date("September 11, 2026") == {"date": "2026-09-11", "candidates": None}
    assert derive_date("09/25/2026") == {"date": "2026-09-25", "candidates": None}  # day > 12
    # locale-ambiguous: both readings valid → candidates, no winner
    assert derive_date("03/04/2026") == {
        "date": None, "candidates": ["2026-03-04", "2026-04-03"]}
    assert derive_date("02/02/2026") == {"date": "2026-02-02", "candidates": None}
    assert derive_date("sometime soon") is None
    assert derive_date("02/30/2026") is None  # impossible calendar date
