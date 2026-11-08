"""Daily balances: rebuild daily_inventory_balance from the ledger legs.

Grain: one row per (sku, location_id, balance_date), dense (every day, even with no
movements), from the pair's start date up to the global horizon.

- Pair start: the day after the opening balance's as_of_date if the pair has one,
  otherwise the date of its first current movement.
- Global horizon: the latest business_date in movement_event. movement_event is
  append-only, so the horizon never moves backwards.
- A pair exists if it has an opening balance or at least one current ledger leg.

Two ways to rebuild:
- incremental_rebuild: only the affected pairs, from their earliest affected date onwards
  (earlier days are untouched), plus extending other pairs when the horizon moves forward.
- full_rebuild: delete everything and rebuild every pair from its start.
Both use rebuild_pair, and a test checks they produce identical tables.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterator, Optional

Pair = tuple[str, str]   # (sku, location_id)


@dataclass
class DayTotals:
    """Movement totals for one pair on one day.

    issue and transfer_out are positive amounts; adjustment keeps its sign.
    """

    receipt: int = 0
    issue: int = 0
    transfer_in: int = 0
    transfer_out: int = 0
    adjustment: int = 0

    def net_change(self) -> int:
        return self.receipt - self.issue + self.transfer_in - self.transfer_out + self.adjustment

    def add_leg(self, leg_type: str, qty_delta: int) -> None:
        """Add one signed ledger leg (from v_movement_leg) to the right component."""
        if leg_type == "RECEIPT":
            self.receipt += qty_delta
        elif leg_type == "ISSUE":
            self.issue += -qty_delta
        elif leg_type == "TRANSFER_IN":
            self.transfer_in += qty_delta
        elif leg_type == "TRANSFER_OUT":
            self.transfer_out += -qty_delta
        elif leg_type == "ADJUSTMENT":
            self.adjustment += qty_delta
        else:
            raise ValueError(f"unknown leg_type {leg_type}")


@dataclass
class Opening:
    """An opening balance: on-hand quantity at the END of as_of_date."""

    as_of_date: str
    quantity: int


@dataclass
class RebuildStats:
    """What a rebuild did."""

    pairs_rebuilt: int = 0
    rows_written: int = 0
    rebuilt_pairs: list[Pair] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Date helpers (dates are 'YYYY-MM-DD' strings, which sort correctly as text)
# ---------------------------------------------------------------------------


def shift_day(day: str, days: int) -> str:
    """Return the date `days` after (or before, if negative) the given date."""
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def date_range(start: str, end: str) -> Iterator[str]:
    """Every date from start to end, inclusive. Empty if start > end."""
    current = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while current <= last:
        yield current.isoformat()
        current += timedelta(days=1)


# ---------------------------------------------------------------------------
# Reading inputs
# ---------------------------------------------------------------------------


def fetch_leg_totals(conn: sqlite3.Connection, pairs: Optional[set[Pair]] = None) -> dict[Pair, dict[str, DayTotals]]:
    """Sum the current ledger legs per pair, day and leg type, in one query.

    With `pairs`, only those pairs are read: they are put in a temporary table and the
    query filters on it, which SQLite pushes into the view so it can use the pair indexes.
    """
    totals: dict[Pair, dict[str, DayTotals]] = {}
    if pairs is None:
        rows = conn.execute(
            """
            SELECT sku, location_id, business_date, leg_type, SUM(qty_delta) AS qty
            FROM v_movement_leg
            GROUP BY sku, location_id, business_date, leg_type
            """
        )
    else:
        if not pairs:
            return totals
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS wanted_pair (sku TEXT, location_id TEXT, PRIMARY KEY (sku, location_id))")
        conn.execute("DELETE FROM wanted_pair")
        conn.executemany("INSERT INTO wanted_pair (sku, location_id) VALUES (?, ?)", sorted(pairs))
        rows = conn.execute(
            """
            SELECT sku, location_id, business_date, leg_type, SUM(qty_delta) AS qty
            FROM v_movement_leg
            WHERE (sku, location_id) IN (SELECT sku, location_id FROM wanted_pair)
            GROUP BY sku, location_id, business_date, leg_type
            """
        )
    for sku, location_id, business_date, leg_type, qty in rows:
        day_totals = totals.setdefault((sku, location_id), {})
        day_totals.setdefault(business_date, DayTotals()).add_leg(leg_type, qty)
    return totals


def fetch_openings(conn: sqlite3.Connection) -> dict[Pair, Opening]:
    """All opening balances, keyed by pair."""
    return {
        (sku, location_id): Opening(as_of_date, quantity)
        for sku, location_id, as_of_date, quantity in conn.execute(
            "SELECT sku, location_id, as_of_date, quantity FROM opening_balance"
        )
    }


def fetch_horizon(conn: sqlite3.Connection) -> Optional[str]:
    """The global horizon: the latest business_date ever loaded (None if no movements yet)."""
    return conn.execute("SELECT MAX(business_date) FROM movement_event").fetchone()[0]


def pair_start_date(pair: Pair, openings: dict[Pair, Opening], leg_totals: dict[Pair, dict[str, DayTotals]]) -> Optional[str]:
    """First balance date of a pair, or None if the pair has no opening and no legs."""
    if pair in openings:
        return shift_day(openings[pair].as_of_date, 1)
    days = leg_totals.get(pair)
    return min(days) if days else None


# ---------------------------------------------------------------------------
# Building rows (pure function, easy to test)
# ---------------------------------------------------------------------------


def build_pair_rows(
    pair: Pair,
    start: str,
    end: str,
    opening_qty: int,
    day_totals: dict[str, DayTotals],
    run_id: str,
) -> list[tuple]:
    """Build one balance row per day from start to end, carrying the balance forward.

    Legs dated outside [start, end] are ignored (for example, legs on or before an
    opening balance's as_of_date are already included in that opening quantity).
    """
    sku, location_id = pair
    rows = []
    balance = opening_qty
    empty = DayTotals()
    for day in date_range(start, end):
        totals = day_totals.get(day, empty)
        net = totals.net_change()
        closing = balance + net
        rows.append((
            sku, location_id, day, balance,
            totals.receipt, totals.issue, totals.transfer_in, totals.transfer_out, totals.adjustment,
            net, closing, run_id,
        ))
        balance = closing
    return rows


# ---------------------------------------------------------------------------
# Writing rows
# ---------------------------------------------------------------------------

INSERT_BALANCE_SQL = """
INSERT INTO daily_inventory_balance
    (sku, location_id, balance_date, opening_qty, receipt_qty, issue_qty, transfer_in_qty,
     transfer_out_qty, adjustment_qty, net_change_qty, closing_qty, run_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def rebuild_pair(
    conn: sqlite3.Connection,
    pair: Pair,
    from_date: Optional[str],
    horizon: Optional[str],
    openings: dict[Pair, Opening],
    leg_totals: dict[Pair, dict[str, DayTotals]],
    run_id: str,
) -> int:
    """Recompute one pair's rows from from_date (None = from the pair's start) to the horizon.

    Rows before from_date are kept, and the day before from_date supplies the opening quantity.
    Returns the number of rows written.
    """
    sku, location_id = pair
    start = pair_start_date(pair, openings, leg_totals)

    if start is None or horizon is None or start > horizon:
        # The pair no longer exists (e.g. a correction moved its only movement elsewhere).
        conn.execute("DELETE FROM daily_inventory_balance WHERE sku = ? AND location_id = ?", (sku, location_id))
        return 0

    begin = start
    opening_qty = openings[pair].quantity if pair in openings else 0
    if from_date is not None and from_date > start:
        previous = conn.execute(
            "SELECT closing_qty FROM daily_inventory_balance WHERE sku = ? AND location_id = ? AND balance_date = ?",
            (sku, location_id, shift_day(from_date, -1)),
        ).fetchone()
        if previous is not None:
            begin = from_date
            opening_qty = previous[0]
        # If yesterday's row is missing, fall back to rebuilding the whole pair.

    # Remove rows before the start (left over if the first movement moved later)
    # and every row from `begin` onwards; then write the new rows.
    conn.execute(
        "DELETE FROM daily_inventory_balance WHERE sku = ? AND location_id = ? AND (balance_date < ? OR balance_date >= ?)",
        (sku, location_id, start, begin),
    )
    rows = build_pair_rows(pair, begin, horizon, opening_qty, leg_totals.get(pair, {}), run_id)
    conn.executemany(INSERT_BALANCE_SQL, rows)
    return len(rows)


def full_rebuild(conn: sqlite3.Connection, run_id: str) -> RebuildStats:
    """Delete every balance row and rebuild every pair from its start."""
    openings = fetch_openings(conn)
    leg_totals = fetch_leg_totals(conn)
    horizon = fetch_horizon(conn)
    conn.execute("DELETE FROM daily_inventory_balance")

    stats = RebuildStats()
    for pair in sorted(set(openings) | set(leg_totals)):
        stats.rows_written += rebuild_pair(conn, pair, None, horizon, openings, leg_totals, run_id)
        stats.pairs_rebuilt += 1
        stats.rebuilt_pairs.append(pair)
    return stats


def extend_pair(conn: sqlite3.Connection, pair: Pair, last_date: str, horizon: str, run_id: str) -> int:
    """Carry an unaffected pair's last closing balance forward from last_date + 1 to the horizon.

    The pair has no movements after last_date (any new movement would have made it affected),
    so the new days have no movements and the ledger does not need to be read.
    """
    sku, location_id = pair
    closing = conn.execute(
        "SELECT closing_qty FROM daily_inventory_balance WHERE sku = ? AND location_id = ? AND balance_date = ?",
        (sku, location_id, last_date),
    ).fetchone()[0]
    rows = build_pair_rows(pair, shift_day(last_date, 1), horizon, closing, {}, run_id)
    conn.executemany(INSERT_BALANCE_SQL, rows)
    return len(rows)


def incremental_rebuild(conn: sqlite3.Connection, affected: dict[Pair, str], run_id: str) -> RebuildStats:
    """Rebuild only what changed, reading the ledger only for the pairs being rebuilt.

    1. Each affected pair: from its earliest affected date to the horizon.
    2. Each pair with an opening balance but no rows yet: build it from its start.
       (A pair with movements always has rows, because it was affected when they loaded.)
    3. Every other pair whose rows stop before the horizon: extend it (carry the balance forward).
    """
    openings = fetch_openings(conn)
    horizon = fetch_horizon(conn)
    last_dates: dict[Pair, str] = {
        (sku, location_id): last_date
        for sku, location_id, last_date in conn.execute(
            "SELECT sku, location_id, MAX(balance_date) FROM daily_inventory_balance GROUP BY sku, location_id"
        )
    }
    new_opening_pairs = sorted(p for p in openings if p not in last_dates and p not in affected)
    leg_totals = fetch_leg_totals(conn, set(affected) | set(new_opening_pairs))

    stats = RebuildStats()

    def count(pair: Pair, rows_written: int) -> None:
        stats.rows_written += rows_written
        stats.pairs_rebuilt += 1
        stats.rebuilt_pairs.append(pair)

    for pair in sorted(affected):
        from_date = affected[pair]
        # If the pair's rows stop before the affected date, start right after its last row.
        if pair in last_dates:
            from_date = min(from_date, shift_day(last_dates[pair], 1))
        count(pair, rebuild_pair(conn, pair, from_date, horizon, openings, leg_totals, run_id))

    if horizon is not None:
        for pair in new_opening_pairs:
            count(pair, rebuild_pair(conn, pair, None, horizon, openings, leg_totals, run_id))
        for pair in sorted(last_dates):
            if pair not in affected and last_dates[pair] < horizon:
                count(pair, extend_pair(conn, pair, last_dates[pair], horizon, run_id))
    return stats


def count_negative_rows(conn: sqlite3.Connection) -> int:
    """How many balance rows (in the whole table) have a negative closing quantity."""
    return conn.execute("SELECT COUNT(*) FROM daily_inventory_balance WHERE closing_qty < 0").fetchone()[0]
