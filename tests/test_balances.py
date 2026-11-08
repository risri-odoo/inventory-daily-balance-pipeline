"""Unit tests for the pure balance-building helpers."""

import pytest

from inventory_pipeline.balances import DayTotals, build_pair_rows, date_range, shift_day


def test_add_leg_maps_signed_legs_to_positive_components() -> None:
    totals = DayTotals()
    totals.add_leg("RECEIPT", 10)
    totals.add_leg("ISSUE", -3)
    totals.add_leg("TRANSFER_IN", 4)
    totals.add_leg("TRANSFER_OUT", -5)
    totals.add_leg("ADJUSTMENT", -2)

    assert (totals.receipt, totals.issue, totals.transfer_in, totals.transfer_out, totals.adjustment) == (10, 3, 4, 5, -2)
    assert totals.net_change() == 10 - 3 + 4 - 5 - 2


def test_add_leg_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        DayTotals().add_leg("RETURN", 1)


def test_date_helpers_cross_month_and_leap_day() -> None:
    assert shift_day("2028-02-28", 1) == "2028-02-29"
    assert shift_day("2026-03-01", -1) == "2026-02-28"
    assert list(date_range("2026-01-30", "2026-02-02")) == ["2026-01-30", "2026-01-31", "2026-02-01", "2026-02-02"]
    assert list(date_range("2026-02-02", "2026-02-01")) == []


def test_build_pair_rows_is_dense_and_carries_balance_forward() -> None:
    day_totals = {"2026-02-01": DayTotals(receipt=10), "2026-02-03": DayTotals(issue=4)}

    rows = build_pair_rows(("S1", "A"), "2026-02-01", "2026-02-04", 100, day_totals, "run-x")

    # (sku, loc, date, opening, receipt, issue, t_in, t_out, adj, net, closing, run_id)
    assert rows == [
        ("S1", "A", "2026-02-01", 100, 10, 0, 0, 0, 0, 10, 110, "run-x"),
        ("S1", "A", "2026-02-02", 110, 0, 0, 0, 0, 0, 0, 110, "run-x"),   # no movement: carried forward
        ("S1", "A", "2026-02-03", 110, 0, 4, 0, 0, 0, -4, 106, "run-x"),
        ("S1", "A", "2026-02-04", 106, 0, 0, 0, 0, 0, 0, 106, "run-x"),
    ]


def test_build_pair_rows_ignores_legs_outside_the_range() -> None:
    day_totals = {"2026-01-31": DayTotals(receipt=99), "2026-02-01": DayTotals(receipt=1)}
    rows = build_pair_rows(("S1", "A"), "2026-02-01", "2026-02-01", 100, day_totals, "run-x")
    assert [row[-2] for row in rows] == [101]


def test_build_pair_rows_allows_negative_closing() -> None:
    rows = build_pair_rows(("S2", "A"), "2026-02-01", "2026-02-01", 0, {"2026-02-01": DayTotals(issue=5)}, "run-x")
    assert rows[0][-2] == -5
