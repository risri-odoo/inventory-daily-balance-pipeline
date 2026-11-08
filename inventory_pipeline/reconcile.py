"""Reconciliation checks: prove the balance table agrees with the ledger and the run log.

Each check is one function that runs SQL against the database and returns a CheckResult
with status PASS, FAIL or WARN, how many rows failed, and up to MAX_EXAMPLES example rows.
The checks recompute things independently in SQL rather than reusing balances.py, so a
bug in the Python rebuild would show up here as a FAIL.
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from inventory_pipeline.db import utc_now_iso

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

MAX_EXAMPLES = 5

# Ledger legs that count towards balances: current legs, minus any leg dated on or before
# its pair's opening as_of_date (already included in the opening snapshot; see A19).
LEDGER_LEGS_SQL = """
SELECT l.movement_id, l.leg_type, l.sku, l.location_id, l.business_date, l.qty_delta
FROM v_movement_leg AS l
LEFT JOIN opening_balance AS o
  ON o.sku = l.sku AND o.location_id = l.location_id
WHERE o.as_of_date IS NULL OR l.business_date > o.as_of_date
"""


@dataclass
class CheckResult:
    """The outcome of one reconciliation check."""

    name: str
    description: str
    status: str
    failures: int = 0
    details: str = ""
    examples: list[dict] = field(default_factory=list)


@dataclass
class ReconciliationReport:
    """All check results plus summary counts."""

    generated_at: str
    checks: list[CheckResult]
    table_counts: dict[str, int]

    @property
    def overall_status(self) -> str:
        """FAIL if any check failed, else WARN if any warned, else PASS."""
        statuses = {check.status for check in self.checks}
        if FAIL in statuses:
            return FAIL
        if WARN in statuses:
            return WARN
        return PASS

    @property
    def passed(self) -> bool:
        """True when no check FAILED (warnings are allowed)."""
        return self.overall_status != FAIL


def query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return the rows as plain dicts."""
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def result_from_failures(name: str, description: str, failing_rows: list[dict], details: str = "") -> CheckResult:
    """PASS if there are no failing rows, otherwise FAIL with a few examples."""
    return CheckResult(
        name=name,
        description=description,
        status=PASS if not failing_rows else FAIL,
        failures=len(failing_rows),
        details=details,
        examples=failing_rows[:MAX_EXAMPLES],
    )


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_run_counts(conn: sqlite3.Connection) -> CheckResult:
    """rows_read = accepted + rejected + duplicate for every run, and each count matches the tables.

    For runs that did not succeed, the tables must hold no rows for that run_id (rollback worked).
    """
    rows = query(conn, """
        SELECT r.run_id, r.source_file, r.status,
               r.rows_read, r.rows_accepted, r.rows_rejected, r.rows_duplicate,
               (SELECT COUNT(*) FROM movement_event e WHERE e.run_id = r.run_id) AS table_accepted,
               (SELECT COUNT(*) FROM quarantine_movement q
                 WHERE q.run_id = r.run_id AND q.reject_reason <> 'DUPLICATE_EVENT') AS table_rejected,
               (SELECT COUNT(*) FROM quarantine_movement q
                 WHERE q.run_id = r.run_id AND q.reject_reason = 'DUPLICATE_EVENT') AS table_duplicate
        FROM pipeline_run AS r
        ORDER BY r.rowid  -- insertion order (started_at has only 1-second resolution)
    """)
    failing = []
    for row in rows:
        if row["status"] == "SUCCESS":
            ok = (
                row["rows_read"] == row["rows_accepted"] + row["rows_rejected"] + row["rows_duplicate"]
                and row["rows_accepted"] == row["table_accepted"]
                and row["rows_rejected"] == row["table_rejected"]
                and row["rows_duplicate"] == row["table_duplicate"]
            )
        else:
            ok = row["table_accepted"] == 0 and row["table_rejected"] == 0 and row["table_duplicate"] == 0
        if not ok:
            failing.append(row)
    total_read = sum(r["rows_read"] for r in rows if r["status"] == "SUCCESS")
    total_in_tables = sum(r["table_accepted"] + r["table_rejected"] + r["table_duplicate"] for r in rows)
    return result_from_failures(
        "run_counts",
        "Per run: rows_read = accepted + rejected + duplicate, matching movement_event and quarantine_movement counts for that run_id.",
        failing,
        f"{len(rows)} runs checked; rows read by successful runs = {total_read}; rows in tables = {total_in_tables}",
    )


def check_independent_recompute(conn: sqlite3.Connection) -> CheckResult:
    """Recompute closing_qty with a SQL window running sum and compare on every movement date."""
    failing = query(conn, f"""
        WITH legs AS ({LEDGER_LEGS_SQL}),
        daily AS (
            SELECT sku, location_id, business_date, SUM(qty_delta) AS delta
            FROM legs
            GROUP BY sku, location_id, business_date
        ),
        running AS (
            SELECT d.sku, d.location_id, d.business_date,
                   COALESCE(o.quantity, 0)
                     + SUM(d.delta) OVER (
                         PARTITION BY d.sku, d.location_id
                         ORDER BY d.business_date
                         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS expected_closing
            FROM daily AS d
            LEFT JOIN opening_balance AS o ON o.sku = d.sku AND o.location_id = d.location_id
        )
        SELECT r.sku, r.location_id, r.business_date, r.expected_closing, b.closing_qty
        FROM running AS r
        LEFT JOIN daily_inventory_balance AS b
          ON b.sku = r.sku AND b.location_id = r.location_id AND b.balance_date = r.business_date
        WHERE b.closing_qty IS NULL OR b.closing_qty <> r.expected_closing
        ORDER BY r.sku, r.location_id, r.business_date
    """)
    checked = conn.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT sku, location_id, business_date FROM ({LEDGER_LEGS_SQL}))").fetchone()[0]
    return result_from_failures(
        "independent_recompute",
        "Opening balance + SQL window cumulative sum of leg deltas equals closing_qty on every movement date.",
        failing,
        f"{checked} (pair, movement date) points compared",
    )


def check_continuity(conn: sqlite3.Connection) -> CheckResult:
    """Today's opening = yesterday's closing, no date gaps, and the first opening = opening balance (or 0)."""
    failing = query(conn, """
        WITH ordered AS (
            SELECT b.sku, b.location_id, b.balance_date, b.opening_qty,
                   LAG(b.closing_qty) OVER w AS previous_closing,
                   LAG(b.balance_date) OVER w AS previous_date,
                   COALESCE(o.quantity, 0) AS opening_balance_qty
            FROM daily_inventory_balance AS b
            LEFT JOIN opening_balance AS o ON o.sku = b.sku AND o.location_id = b.location_id
            WINDOW w AS (PARTITION BY b.sku, b.location_id ORDER BY b.balance_date)
        )
        SELECT sku, location_id, balance_date, opening_qty, previous_date, previous_closing, opening_balance_qty,
               CASE
                   WHEN previous_date IS NULL THEN 'first opening differs from opening balance'
                   WHEN julianday(balance_date) - julianday(previous_date) <> 1 THEN 'date gap'
                   ELSE 'opening differs from previous closing'
               END AS problem
        FROM ordered
        WHERE (previous_date IS NULL AND opening_qty <> opening_balance_qty)
           OR (previous_date IS NOT NULL AND julianday(balance_date) - julianday(previous_date) <> 1)
           OR (previous_date IS NOT NULL AND opening_qty <> previous_closing)
        ORDER BY sku, location_id, balance_date
    """)
    return result_from_failures(
        "continuity",
        "Opening = previous day's closing with no date gaps; first opening = opening balance (or 0).",
        failing,
    )


def check_row_arithmetic(conn: sqlite3.Connection) -> CheckResult:
    """closing = opening + net_change, and net_change = receipt - issue + transfer_in - transfer_out + adjustment."""
    failing = query(conn, """
        SELECT sku, location_id, balance_date, opening_qty, receipt_qty, issue_qty, transfer_in_qty,
               transfer_out_qty, adjustment_qty, net_change_qty, closing_qty
        FROM daily_inventory_balance
        WHERE closing_qty <> opening_qty + net_change_qty
           OR net_change_qty <> receipt_qty - issue_qty + transfer_in_qty - transfer_out_qty + adjustment_qty
        ORDER BY sku, location_id, balance_date
    """)
    rows = conn.execute("SELECT COUNT(*) FROM daily_inventory_balance").fetchone()[0]
    return result_from_failures(
        "row_arithmetic",
        "closing = opening + net_change, and net_change = receipt - issue + transfer_in - transfer_out + adjustment, on every row.",
        failing,
        f"{rows} balance rows checked",
    )


def check_component_totals(conn: sqlite3.Connection) -> CheckResult:
    """Total receipts, issues, transfers in/out and adjustments in balances equal the ledger totals."""
    ledger = query(conn, f"""
        SELECT
            COALESCE(SUM(CASE WHEN leg_type = 'RECEIPT' THEN qty_delta END), 0)       AS receipt,
            COALESCE(SUM(CASE WHEN leg_type = 'ISSUE' THEN -qty_delta END), 0)        AS issue,
            COALESCE(SUM(CASE WHEN leg_type = 'TRANSFER_IN' THEN qty_delta END), 0)   AS transfer_in,
            COALESCE(SUM(CASE WHEN leg_type = 'TRANSFER_OUT' THEN -qty_delta END), 0) AS transfer_out,
            COALESCE(SUM(CASE WHEN leg_type = 'ADJUSTMENT' THEN qty_delta END), 0)    AS adjustment
        FROM ({LEDGER_LEGS_SQL})
    """)[0]
    balance = query(conn, """
        SELECT COALESCE(SUM(receipt_qty), 0) AS receipt, COALESCE(SUM(issue_qty), 0) AS issue,
               COALESCE(SUM(transfer_in_qty), 0) AS transfer_in, COALESCE(SUM(transfer_out_qty), 0) AS transfer_out,
               COALESCE(SUM(adjustment_qty), 0) AS adjustment
        FROM daily_inventory_balance
    """)[0]
    failing = [
        {"component": name, "ledger_total": ledger[name], "balance_total": balance[name]}
        for name in ("receipt", "issue", "transfer_in", "transfer_out", "adjustment")
        if ledger[name] != balance[name]
    ]
    summary = ", ".join(f"{name}={ledger[name]}" for name in ledger)
    return result_from_failures(
        "component_totals",
        "Receipt, issue, transfer-in, transfer-out and adjustment totals in daily_inventory_balance equal the ledger.",
        failing,
        f"ledger totals: {summary}",
    )


def check_transfer_balance(conn: sqlite3.Connection) -> CheckResult:
    """Every unit transferred out somewhere is transferred in somewhere else."""
    row = query(conn, """
        SELECT COALESCE(SUM(transfer_in_qty), 0) AS transfer_in, COALESCE(SUM(transfer_out_qty), 0) AS transfer_out
        FROM daily_inventory_balance
    """)[0]
    failing = [row] if row["transfer_in"] != row["transfer_out"] else []
    return result_from_failures(
        "transfer_balance",
        "Total transfer_in_qty equals total transfer_out_qty.",
        failing,
        f"transfer_in={row['transfer_in']}, transfer_out={row['transfer_out']}",
    )


def check_pair_coverage(conn: sqlite3.Connection) -> CheckResult:
    """Every pair that should exist has rows from its start date to the global horizon."""
    horizon = conn.execute("SELECT MAX(business_date) FROM movement_event").fetchone()[0]
    if horizon is None:
        count = conn.execute("SELECT COUNT(*) FROM daily_inventory_balance").fetchone()[0]
        failing = [{"problem": "balance rows exist but no movements", "rows": count}] if count else []
        return result_from_failures("pair_coverage", "Every pair runs from its start date to the global horizon.",
                                    failing, "no movements loaded, so no horizon")
    failing = query(conn, """
        WITH pairs AS (
            SELECT sku, location_id FROM opening_balance
            UNION
            SELECT sku, location_id FROM v_movement_leg
        ),
        expected AS (
            SELECT p.sku, p.location_id,
                   CASE WHEN o.as_of_date IS NOT NULL THEN date(o.as_of_date, '+1 day')
                        ELSE (SELECT MIN(l.business_date) FROM v_movement_leg AS l
                              WHERE l.sku = p.sku AND l.location_id = p.location_id)
                   END AS expected_start
            FROM pairs AS p
            LEFT JOIN opening_balance AS o ON o.sku = p.sku AND o.location_id = p.location_id
        ),
        actual AS (
            SELECT sku, location_id, MIN(balance_date) AS first_date, MAX(balance_date) AS last_date
            FROM daily_inventory_balance
            GROUP BY sku, location_id
        )
        SELECT e.sku, e.location_id, e.expected_start, a.first_date, ? AS horizon, a.last_date
        FROM expected AS e
        LEFT JOIN actual AS a ON a.sku = e.sku AND a.location_id = e.location_id
        WHERE e.expected_start <= ?
          AND (a.first_date IS NULL OR a.first_date <> e.expected_start OR a.last_date <> ?)
        UNION ALL
        SELECT a.sku, a.location_id, NULL, a.first_date, ?, a.last_date
        FROM actual AS a
        WHERE NOT EXISTS (SELECT 1 FROM expected AS e WHERE e.sku = a.sku AND e.location_id = a.location_id)
    """, (horizon, horizon, horizon, horizon))
    pairs = conn.execute("SELECT COUNT(*) FROM (SELECT DISTINCT sku, location_id FROM daily_inventory_balance)").fetchone()[0]
    return result_from_failures(
        "pair_coverage",
        "Every pair runs from its start date (as_of_date + 1, or first movement) to the global horizon; no unexpected pairs.",
        failing,
        f"{pairs} pairs; global horizon {horizon}",
    )


def check_current_version_unique(conn: sqlite3.Connection) -> CheckResult:
    """Exactly one current version per movement_id."""
    failing = query(conn, """
        SELECT movement_id, COUNT(*) AS current_rows
        FROM v_movement_current
        GROUP BY movement_id
        HAVING COUNT(*) <> 1
    """)
    movements = conn.execute("SELECT COUNT(DISTINCT movement_id) FROM movement_event").fetchone()[0]
    return result_from_failures(
        "current_version_unique",
        "Exactly one current version per movement_id.",
        failing,
        f"{movements} distinct movements",
    )


def check_referential_integrity(conn: sqlite3.Connection) -> CheckResult:
    """No SKU or location in events, balances or opening balances that is missing from the dimensions."""
    failing = query(conn, """
        SELECT 'movement_event.sku' AS reference, sku AS value FROM movement_event
         WHERE sku NOT IN (SELECT sku FROM dim_sku)
        UNION
        SELECT 'movement_event.location_id', location_id FROM movement_event
         WHERE location_id NOT IN (SELECT location_id FROM dim_location)
        UNION
        SELECT 'movement_event.to_location_id', to_location_id FROM movement_event
         WHERE to_location_id IS NOT NULL AND to_location_id NOT IN (SELECT location_id FROM dim_location)
        UNION
        SELECT 'daily_inventory_balance.sku', sku FROM daily_inventory_balance
         WHERE sku NOT IN (SELECT sku FROM dim_sku)
        UNION
        SELECT 'daily_inventory_balance.location_id', location_id FROM daily_inventory_balance
         WHERE location_id NOT IN (SELECT location_id FROM dim_location)
        UNION
        SELECT 'opening_balance.sku', sku FROM opening_balance WHERE sku NOT IN (SELECT sku FROM dim_sku)
        UNION
        SELECT 'opening_balance.location_id', location_id FROM opening_balance
         WHERE location_id NOT IN (SELECT location_id FROM dim_location)
    """)
    return result_from_failures(
        "referential_integrity",
        "No orphan sku or location values in movement_event, daily_inventory_balance or opening_balance.",
        failing,
    )


def check_quarantine_traceability(conn: sqlite3.Connection) -> CheckResult:
    """Every quarantined row has a source_file, a source_line and its raw_record."""
    failing = query(conn, """
        SELECT quarantine_id, run_id, source_file, source_line, reject_reason
        FROM quarantine_movement
        WHERE source_file IS NULL OR source_file = ''
           OR source_line IS NULL OR source_line < 1
           OR raw_record IS NULL OR raw_record = ''
           OR reject_reason IS NULL OR reject_reason = ''
    """)
    rows = conn.execute("SELECT COUNT(*) FROM quarantine_movement").fetchone()[0]
    return result_from_failures(
        "quarantine_traceability",
        "Every quarantine row has source_file, source_line, raw_record and a reject_reason.",
        failing,
        f"{rows} quarantine rows checked",
    )


def check_closed_period_legs(conn: sqlite3.Connection) -> CheckResult:
    """WARN if any current leg is dated on/before its pair's opening as_of_date (ignored by balances)."""
    rows = query(conn, """
        SELECT l.movement_id, l.leg_type, l.sku, l.location_id, l.business_date, o.as_of_date
        FROM v_movement_leg AS l
        JOIN opening_balance AS o ON o.sku = l.sku AND o.location_id = l.location_id
        WHERE l.business_date <= o.as_of_date
        ORDER BY l.sku, l.location_id, l.business_date
    """)
    return CheckResult(
        name="closed_period_legs",
        description="No accepted leg is dated on or before its pair's opening as_of_date (can only happen if an opening balance changed after loading).",
        status=WARN if rows else PASS,
        failures=len(rows),
        details="such legs are excluded from balances" if rows else "",
        examples=rows[:MAX_EXAMPLES],
    )


def check_negative_balances(conn: sqlite3.Connection) -> CheckResult:
    """Negative closing balances are a data-quality WARN (not a failure), with a count and examples."""
    rows = query(conn, """
        SELECT sku, location_id, balance_date, closing_qty
        FROM daily_inventory_balance
        WHERE closing_qty < 0
        ORDER BY sku, location_id, balance_date
    """)
    pairs = len({(r["sku"], r["location_id"]) for r in rows})
    return CheckResult(
        name="negative_balances",
        description="Closing balances below zero (data-quality warning: stock issued that was never received).",
        status=WARN if rows else PASS,
        failures=len(rows),
        details=f"{len(rows)} negative rows across {pairs} pairs" if rows else "no negative balances",
        examples=rows[:MAX_EXAMPLES],
    )


ALL_CHECKS: list[Callable[[sqlite3.Connection], CheckResult]] = [
    check_run_counts,
    check_independent_recompute,
    check_continuity,
    check_row_arithmetic,
    check_component_totals,
    check_transfer_balance,
    check_pair_coverage,
    check_current_version_unique,
    check_referential_integrity,
    check_quarantine_traceability,
    check_closed_period_legs,
    check_negative_balances,
]

COUNTED_TABLES = [
    "dim_sku", "dim_location", "opening_balance", "movement_event", "quarantine_movement",
    "daily_inventory_balance", "pipeline_run", "processed_file",
]


def run_reconciliation(conn: sqlite3.Connection) -> ReconciliationReport:
    """Run every check and collect the results."""
    checks = [check(conn) for check in ALL_CHECKS]
    table_counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]   # table names are constants
        for table in COUNTED_TABLES
    }
    return ReconciliationReport(generated_at=utc_now_iso(), checks=checks, table_counts=table_counts)
