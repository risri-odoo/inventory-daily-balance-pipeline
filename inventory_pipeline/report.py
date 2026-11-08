"""Reports: reconciliation report (Markdown + JSON) and CSV exports.

Files written to the output folder:
- reconciliation_report.md / reconciliation_report.json
- daily_inventory_balance.csv, quarantine_movement.csv, pipeline_run.csv
"""

import csv
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from inventory_pipeline.reconcile import ReconciliationReport

# Runs are ordered by rowid, i.e. the order they were inserted: started_at has only
# 1-second resolution, so two runs in the same second would otherwise come out in random order.
EXPORTS = {
    "daily_inventory_balance.csv": "SELECT * FROM daily_inventory_balance ORDER BY sku, location_id, balance_date",
    "quarantine_movement.csv": "SELECT * FROM quarantine_movement ORDER BY quarantine_id",
    "pipeline_run.csv": "SELECT * FROM pipeline_run ORDER BY rowid",
}


def export_csv(conn: sqlite3.Connection, sql: str, path: Path) -> int:
    """Write a query's result to a CSV file (with header). Returns the number of data rows."""
    cursor = conn.execute(sql)
    columns = [description[0] for description in cursor.description]
    rows = cursor.fetchall()
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([tuple(row) for row in rows])
    return len(rows)


def reject_summary(conn: sqlite3.Connection) -> list[dict]:
    """Quarantined rows per reject reason."""
    return [dict(row) for row in conn.execute(
        "SELECT reject_reason, COUNT(*) AS row_count FROM quarantine_movement GROUP BY reject_reason ORDER BY reject_reason"
    )]


def run_summary(conn: sqlite3.Connection) -> list[dict]:
    """One line per run, for the report."""
    return [dict(row) for row in conn.execute(
        """
        SELECT run_id, source_file, status, rows_read, rows_accepted, rows_rejected, rows_duplicate,
               rows_late, rows_superseding, min_affected_date, pairs_rebuilt, balance_rows_written,
               negative_balance_rows, duration_ms, error_message
        FROM pipeline_run ORDER BY rowid
        """
    )]


def markdown_table(rows: list[dict]) -> list[str]:
    """Render a list of dicts as Markdown table lines."""
    if not rows:
        return ["_none_"]
    columns = list(rows[0].keys())
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        cells = ["" if row[c] is None else str(row[c]).replace("|", "\\|") for c in columns]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def render_markdown(report: ReconciliationReport, runs: list[dict], rejects: list[dict]) -> str:
    """The human-readable reconciliation report."""
    lines = [
        "# Reconciliation report",
        "",
        f"Generated at: {report.generated_at}",
        "",
        f"**Overall status: {report.overall_status}**",
        "",
        "## Checks",
        "",
        "| check | status | failures | details |",
        "|---|---|---|---|",
    ]
    for check in report.checks:
        lines.append(f"| {check.name} | {check.status} | {check.failures} | {check.details} |")
    lines.append("")
    for check in report.checks:
        lines += [f"### {check.name}: {check.status}", "", check.description, ""]
        if check.examples:
            lines += ["Examples:", ""] + markdown_table(check.examples) + [""]
    lines += ["## Table row counts", ""]
    lines += markdown_table([{"table": t, "rows": n} for t, n in report.table_counts.items()])
    lines += ["", "## Rejected and duplicate rows by reason", ""]
    lines += markdown_table(rejects)
    lines += ["", "## Runs", ""]
    lines += markdown_table(runs)
    lines.append("")
    return "\n".join(lines)


def write_report(conn: sqlite3.Connection, report: ReconciliationReport, out_dir: Path) -> list[Path]:
    """Write the reconciliation report and the CSV exports. Returns the paths written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    runs = run_summary(conn)
    rejects = reject_summary(conn)

    md_path = out_dir / "reconciliation_report.md"
    md_path.write_text(render_markdown(report, runs, rejects), encoding="utf-8", newline="\n")

    json_path = out_dir / "reconciliation_report.json"
    payload = {
        "generated_at": report.generated_at,
        "overall_status": report.overall_status,
        "checks": [asdict(check) for check in report.checks],
        "table_counts": report.table_counts,
        "rejects_by_reason": rejects,
        "runs": runs,
    }
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")

    written = [md_path, json_path]
    for file_name, sql in EXPORTS.items():
        path = out_dir / file_name
        export_csv(conn, sql, path)
        written.append(path)
    return written
