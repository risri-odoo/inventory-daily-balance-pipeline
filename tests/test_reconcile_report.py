"""Reconciliation checks, report files and the demo command.

Each tamper test corrupts one thing directly in the database and checks that the right
reconciliation check turns FAIL, so we know the checks can actually catch problems.
"""

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from conftest import BatchWriter, mv, write_reference
from inventory_pipeline import cli, pipeline, reconcile, report

ROWS = [
    mv("M1", "RECEIPT", "S1", "A", 10, "2026-02-01"),
    mv("M2", "TRANSFER", "S1", "A", 5, "2026-02-02", to_location="B"),
    mv("M3", "ADJUSTMENT", "S1", "B", -1, "2026-02-03"),
    mv("M4", "RECEIPT", "S2", "C", 4, "2026-02-02"),
    "M5,1,RECEIPT,NOPE,A,,1,2026-02-01,2026-02-01T00:00:00Z,,X",   # rejected: UNKNOWN_SKU
    mv("M1", "RECEIPT", "S1", "A", 10, "2026-02-01"),              # DUPLICATE_EVENT
]


@pytest.fixture
def loaded(conn: sqlite3.Connection, write_batch: BatchWriter) -> sqlite3.Connection:
    path = write_batch("b1.csv", ROWS)
    assert pipeline.run_file(conn, path, path.parent).status == pipeline.SUCCESS
    return conn


def statuses(conn: sqlite3.Connection) -> dict[str, str]:
    return {check.name: check.status for check in reconcile.run_reconciliation(conn).checks}


def test_clean_load_passes_every_check(loaded: sqlite3.Connection) -> None:
    result = reconcile.run_reconciliation(loaded)
    assert {c.name: c.status for c in result.checks} == {name: "PASS" for name in statuses(loaded)}
    assert result.overall_status == "PASS" and result.passed
    assert len(result.checks) == len(reconcile.ALL_CHECKS)


def test_empty_database_passes(conn: sqlite3.Connection) -> None:
    assert reconcile.run_reconciliation(conn).overall_status == "PASS"


def test_negative_balance_is_warn_not_fail(loaded: sqlite3.Connection, write_batch: BatchWriter) -> None:
    path = write_batch("b2.csv", [mv("M9", "ISSUE", "S2", "A", 3, "2026-02-03")])
    pipeline.run_file(loaded, path, path.parent)

    result = reconcile.run_reconciliation(loaded)
    negative = next(c for c in result.checks if c.name == "negative_balances")

    assert negative.status == "WARN" and negative.failures == 1
    assert negative.examples[0]["closing_qty"] == -3
    assert result.overall_status == "WARN" and result.passed


def test_failed_run_with_nothing_written_passes_run_counts(loaded: sqlite3.Connection, write_batch: BatchWriter) -> None:
    path = write_batch("b2.csv", [mv("M9", "RECEIPT", "S1", "A", 1, "2026-02-03")])
    assert pipeline.run_file(loaded, path, path.parent, fail_at="after_rebuild").status == pipeline.FAILED
    assert statuses(loaded)["run_counts"] == "PASS"


# --- tamper tests: each check must be able to FAIL ------------------------------------


def test_wrong_closing_is_caught(loaded: sqlite3.Connection) -> None:
    loaded.execute("UPDATE daily_inventory_balance SET closing_qty = closing_qty + 1 WHERE sku='S1' AND location_id='A' AND balance_date='2026-02-02'")
    result = statuses(loaded)
    assert result["independent_recompute"] == "FAIL"
    assert result["row_arithmetic"] == "FAIL"
    assert result["continuity"] == "FAIL"   # next day's opening no longer matches


def test_missing_day_is_caught(loaded: sqlite3.Connection) -> None:
    loaded.execute("DELETE FROM daily_inventory_balance WHERE sku='S1' AND location_id='B' AND balance_date='2026-02-02'")
    result = statuses(loaded)
    assert result["continuity"] == "FAIL"
    assert result["component_totals"] == "FAIL"
    assert result["transfer_balance"] == "FAIL"


def test_wrong_first_opening_is_caught(loaded: sqlite3.Connection) -> None:
    loaded.execute("UPDATE daily_inventory_balance SET opening_qty = 0, closing_qty = 10, net_change_qty = 10 WHERE sku='S1' AND location_id='A' AND balance_date='2026-02-01'")
    assert statuses(loaded)["continuity"] == "FAIL"


def test_pair_not_reaching_horizon_is_caught(loaded: sqlite3.Connection) -> None:
    loaded.execute("DELETE FROM daily_inventory_balance WHERE sku='S2' AND location_id='C' AND balance_date='2026-02-03'")
    assert statuses(loaded)["pair_coverage"] == "FAIL"


def test_unexpected_pair_is_caught(loaded: sqlite3.Connection) -> None:
    loaded.execute("INSERT INTO daily_inventory_balance VALUES ('S2','B','2026-02-03',0,0,0,0,0,0,0,0,'x')")
    assert statuses(loaded)["pair_coverage"] == "FAIL"


def test_run_count_mismatch_is_caught(loaded: sqlite3.Connection) -> None:
    loaded.execute("UPDATE pipeline_run SET rows_accepted = rows_accepted + 1 WHERE source_file = 'b1.csv'")
    assert statuses(loaded)["run_counts"] == "FAIL"


def test_orphan_reference_is_caught(loaded: sqlite3.Connection) -> None:
    loaded.execute("DELETE FROM dim_location WHERE location_id = 'C'")
    assert statuses(loaded)["referential_integrity"] == "FAIL"


def test_untraceable_quarantine_row_is_caught(loaded: sqlite3.Connection) -> None:
    loaded.execute("UPDATE quarantine_movement SET raw_record = '' WHERE reject_reason = 'UNKNOWN_SKU'")
    assert statuses(loaded)["quarantine_traceability"] == "FAIL"


def test_closed_period_leg_is_warned(tmp_path: Path, loaded: sqlite3.Connection) -> None:
    # Move S1@A's opening date past its movements: those legs are now inside the closed period.
    (tmp_path / "reference" / "opening_balance.csv").write_text(
        "sku,location_id,as_of_date,quantity\nS1,A,2026-02-02,100\nS1,B,2026-01-31,20\n")
    pipeline.run_load_reference(loaded, tmp_path / "reference")
    result = statuses(loaded)
    assert result["closed_period_legs"] == "WARN"
    assert result["independent_recompute"] == "PASS"


# --- report files ------------------------------------------------------------------


def test_report_writes_markdown_json_and_csv(tmp_path: Path, loaded: sqlite3.Connection) -> None:
    out = tmp_path / "report"
    result = reconcile.run_reconciliation(loaded)

    paths = report.write_report(loaded, result, out)

    assert sorted(p.name for p in paths) == sorted([
        "reconciliation_report.md", "reconciliation_report.json",
        "daily_inventory_balance.csv", "quarantine_movement.csv", "pipeline_run.csv",
    ])
    data = json.loads((out / "reconciliation_report.json").read_text(encoding="utf-8"))
    assert data["overall_status"] == "PASS"
    assert {r["reject_reason"]: r["row_count"] for r in data["rejects_by_reason"]} == {"DUPLICATE_EVENT": 1, "UNKNOWN_SKU": 1}
    markdown = (out / "reconciliation_report.md").read_text(encoding="utf-8")
    assert "**Overall status: PASS**" in markdown and "| independent_recompute | PASS |" in markdown
    with open(out / "daily_inventory_balance.csv", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == loaded.execute("SELECT COUNT(*) FROM daily_inventory_balance").fetchone()[0]
    assert {"sku", "location_id", "balance_date", "closing_qty", "run_id"} <= set(rows[0])


# --- CLI: reconcile / report / demo -------------------------------------------------


def make_sample(root: Path, batch_rows: list[str]) -> Path:
    write_reference(root / "reference")
    (root / "input").mkdir(parents=True)
    header = "movement_id,version,movement_type,sku,location_id,to_location_id,quantity,business_date,event_ts,reason_code,source_system"
    (root / "input" / "batch_01.csv").write_text(header + "\n" + "\n".join(batch_rows) + "\n")
    return root


def test_cli_reconcile_and_report_exit_codes(tmp_path: Path) -> None:
    out = tmp_path / "out"
    sample = make_sample(tmp_path / "sample", ROWS)
    assert cli.main(["load-reference", "--ref-dir", str(sample / "reference"), "--output-dir", str(out)]) == 0
    assert cli.main(["run", "--input", str(sample / "input"), "--output-dir", str(out)]) == 0
    assert cli.main(["reconcile", "--output-dir", str(out)]) == 0
    assert cli.main(["report", "--output-dir", str(out), "--out", str(tmp_path / "rep")]) == 0
    assert (tmp_path / "rep" / "reconciliation_report.md").exists()

    conn = sqlite3.connect(out / "inventory.db")
    conn.execute("UPDATE daily_inventory_balance SET closing_qty = closing_qty + 1")
    conn.commit()
    conn.close()
    assert cli.main(["reconcile", "--output-dir", str(out)]) == 1


def test_cli_demo_runs_end_to_end_and_is_repeatable(tmp_path: Path) -> None:
    out = tmp_path / "out"
    sample = make_sample(tmp_path / "sample", ROWS)

    assert cli.main(["demo", "--sample-dir", str(sample), "--output-dir", str(out)]) == 0
    first = (out / "daily_inventory_balance.csv").read_text(encoding="utf-8")
    assert cli.main(["demo", "--sample-dir", str(sample), "--output-dir", str(out)]) == 0   # fresh DB again

    assert (out / "demo.db").exists()
    assert (out / "reconciliation_report.json").exists()
    runs = list(csv.DictReader(open(out / "pipeline_run.csv", encoding="utf-8", newline="")))
    assert [r["status"] for r in runs] == ["SUCCESS", "SUCCESS"]   # reference + batch: no SKIPPED, so the DB was fresh
    second = (out / "daily_inventory_balance.csv").read_text(encoding="utf-8")
    strip_run = lambda text: [line.rsplit(",", 1)[0] for line in text.splitlines()]   # noqa: E731
    assert strip_run(first) == strip_run(second)


def test_cli_demo_fails_on_bad_reconciliation_input(tmp_path: Path) -> None:
    out = tmp_path / "out"
    sample = make_sample(tmp_path / "sample", ROWS)
    (sample / "input" / "batch_02.csv").write_text("movement_id,sku\nX,S1\n")   # REJECTED file
    assert cli.main(["demo", "--sample-dir", str(sample), "--output-dir", str(out)]) == 1
