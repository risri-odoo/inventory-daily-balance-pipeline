"""End-to-end tests on the hand-made sample in data/sample/.

The expected results in data/sample/expected/ were worked out by hand (see its README.md),
so these tests compare the pipeline against an independent answer key.
"""

import csv
import shutil
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from conftest import balances, closing, count
from inventory_pipeline import balances as bal
from inventory_pipeline import cli, db, pipeline, reconcile

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "data" / "sample"
EXPECTED = SAMPLE / "expected"
EDGE = SAMPLE / "edge_cases"


def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def expected_balances() -> list[tuple]:
    rows = []
    for r in read_csv(EXPECTED / "expected_daily_balances.csv"):
        numbers = [int(r[c]) for c in ("opening_qty", "receipt_qty", "issue_qty", "transfer_in_qty",
                                       "transfer_out_qty", "adjustment_qty", "net_change_qty", "closing_qty")]
        rows.append((r["sku"], r["location_id"], r["balance_date"], *numbers))
    return sorted(rows)


def new_db(tmp_path: Path, name: str = "sample.db") -> sqlite3.Connection:
    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    assert pipeline.run_load_reference(conn, SAMPLE / "reference").status == pipeline.SUCCESS
    return conn


@pytest.fixture
def sample_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A fresh database with the sample reference data and both sample batches loaded."""
    conn = new_db(tmp_path)
    results = pipeline.run_input(conn, SAMPLE / "input")
    assert [r.status for r in results] == ["SUCCESS", "SUCCESS"]
    yield conn
    conn.close()


# --- correctness against the hand-computed answer key -----------------------------------


def test_balances_equal_hand_computed_file(sample_db: sqlite3.Connection) -> None:
    assert balances(sample_db) == expected_balances()


def test_rejects_equal_expected_rejects(sample_db: sqlite3.Connection) -> None:
    actual = [
        {k: str(v) for k, v in dict(row).items()}
        for row in sample_db.execute(
            "SELECT source_file, source_line, movement_id, reject_reason FROM quarantine_movement ORDER BY source_file, source_line"
        )
    ]
    assert actual == read_csv(EXPECTED / "expected_rejects.csv")


def test_every_reject_keeps_its_raw_record(sample_db: sqlite3.Connection) -> None:
    for source_file, source_line, raw_record in sample_db.execute(
        "SELECT source_file, source_line, raw_record FROM quarantine_movement"
    ):
        lines = (SAMPLE / "input" / source_file).read_text(encoding="utf-8").splitlines()
        assert lines[source_line - 1] == raw_record


def test_run_counts_are_right(sample_db: sqlite3.Connection) -> None:
    columns = ["source_file", "status", "rows_read", "rows_accepted", "rows_rejected", "rows_duplicate", "rows_late", "rows_superseding"]
    actual = [
        {c: str(row[c]) for c in columns}
        for row in sample_db.execute(
            f"SELECT {', '.join(columns)} FROM pipeline_run WHERE source_file LIKE 'batch_%' ORDER BY started_at, run_id"
        )
    ]
    assert actual == read_csv(EXPECTED / "expected_run_counts.csv")


def test_all_checks_pass_and_negative_pair_is_warn(sample_db: sqlite3.Connection) -> None:
    result = reconcile.run_reconciliation(sample_db)
    statuses = {c.name: c.status for c in result.checks}

    assert statuses.pop("negative_balances") == "WARN"
    assert set(statuses.values()) == {"PASS"}
    negative = next(c for c in result.checks if c.name == "negative_balances")
    assert negative.failures == 2
    assert {(e["sku"], e["location_id"]) for e in negative.examples} == {("SKU-003", "WH-01")}
    assert result.passed


def test_versions_current_and_history(sample_db: sqlite3.Connection) -> None:
    current = dict(sample_db.execute("SELECT movement_id, version FROM v_movement_current WHERE movement_id IN ('MV-1001', 'MV-1005')").fetchall())
    assert current == {"MV-1001": 2, "MV-1005": 3}       # v3 void stays current although v2 arrived later
    versions = [r[0] for r in sample_db.execute("SELECT version FROM movement_event WHERE movement_id = 'MV-1005' ORDER BY version")]
    assert versions == [1, 2, 3]                          # history is never overwritten


def test_late_rows_are_flagged(sample_db: sqlite3.Connection) -> None:
    late = sorted(tuple(r) for r in sample_db.execute("SELECT movement_id, version FROM movement_event WHERE is_late = 1"))
    assert late == [("MV-1001", 2), ("MV-1005", 2), ("MV-1005", 3), ("MV-2004", 1)]


def test_late_batch_recomputes_past_days(tmp_path: Path) -> None:
    conn = new_db(tmp_path)
    pipeline.run_file(conn, SAMPLE / "input" / "batch_01.csv", SAMPLE / "input")
    assert closing(conn, "SKU-001", "WH-01") == {"2026-03-01": 125, "2026-03-02": 100}   # 100 + 40 - 15

    pipeline.run_file(conn, SAMPLE / "input" / "batch_02.csv", SAMPLE / "input")
    assert closing(conn, "SKU-001", "WH-01") == {"2026-03-01": 126, "2026-03-02": 101, "2026-03-03": 91, "2026-03-04": 91}
    conn.close()


# --- idempotency -------------------------------------------------------------------


def test_rerun_is_skipped_with_no_new_rows(sample_db: sqlite3.Connection) -> None:
    before = (count(sample_db, "movement_event"), count(sample_db, "quarantine_movement"), balances(sample_db))

    results = pipeline.run_input(sample_db, SAMPLE / "input")

    assert [r.status for r in results] == ["SKIPPED", "SKIPPED"]
    assert (count(sample_db, "movement_event"), count(sample_db, "quarantine_movement"), balances(sample_db)) == before


def test_force_rerun_turns_every_row_into_a_duplicate(sample_db: sqlite3.Connection) -> None:
    before_events = count(sample_db, "movement_event")

    results = pipeline.run_input(sample_db, SAMPLE / "input", force=True)

    # Batch 1: the 8 accepted rows and the duplicate are all duplicates now; the 8 bad rows are rejected again.
    # Batch 2: the 10 accepted rows are duplicates; the 4 bad rows (incl. the conflict) are rejected again.
    assert [(r.status, r.rows_accepted, r.rows_rejected, r.rows_duplicate) for r in results] == [
        ("SUCCESS", 0, 8, 9),
        ("SUCCESS", 0, 4, 10),
    ]
    assert count(sample_db, "movement_event") == before_events
    assert balances(sample_db) == expected_balances()
    assert reconcile.run_reconciliation(sample_db).passed


def test_incremental_equals_full_rebuild(sample_db: sqlite3.Connection) -> None:
    incremental = balances(sample_db)
    with db.transaction(sample_db):
        bal.full_rebuild(sample_db, "full")
    assert balances(sample_db) == incremental == expected_balances()


def test_full_rebuild_flag_per_batch_gives_same_answer(tmp_path: Path) -> None:
    conn = new_db(tmp_path)
    results = pipeline.run_input(conn, SAMPLE / "input", full_rebuild=True)
    assert [r.status for r in results] == ["SUCCESS", "SUCCESS"]
    assert balances(conn) == expected_balances()
    conn.close()


def test_out_of_order_batches_give_same_balances(tmp_path: Path) -> None:
    """Loading batch 2 before batch 1 changes which rows are late, but not the final balances."""
    conn = new_db(tmp_path)
    pipeline.run_file(conn, SAMPLE / "input" / "batch_02.csv", SAMPLE / "input")
    pipeline.run_file(conn, SAMPLE / "input" / "batch_01.csv", SAMPLE / "input")
    rows = balances(conn)
    # The conflicting MV-1006 (qty 9) now arrives first and wins, so ST-01 differs by 1 on 03-02.
    # Everything else must match the answer key.
    expected = [r for r in expected_balances() if (r[0], r[1]) != ("SKU-001", "ST-01")]
    assert [r for r in rows if (r[0], r[1]) != ("SKU-001", "ST-01")] == expected
    assert closing(conn, "SKU-001", "ST-01") == {"2026-03-01": 20, "2026-03-02": 34, "2026-03-03": 29, "2026-03-04": 29}
    assert reconcile.run_reconciliation(conn).passed
    conn.close()


# --- partial failure and retry on the sample ----------------------------------------------


@pytest.mark.parametrize("fail_at", pipeline.FAIL_POINTS)
def test_partial_failure_then_retry(tmp_path: Path, fail_at: str) -> None:
    conn = new_db(tmp_path)
    pipeline.run_file(conn, SAMPLE / "input" / "batch_01.csv", SAMPLE / "input")
    snapshot = (count(conn, "movement_event"), count(conn, "quarantine_movement"), count(conn, "processed_file"), balances(conn))

    failed = pipeline.run_file(conn, SAMPLE / "input" / "batch_02.csv", SAMPLE / "input", fail_at=fail_at)
    assert failed.status == pipeline.FAILED
    assert (count(conn, "movement_event"), count(conn, "quarantine_movement"), count(conn, "processed_file"), balances(conn)) == snapshot

    retry = pipeline.run_file(conn, SAMPLE / "input" / "batch_02.csv", SAMPLE / "input")
    assert retry.status == pipeline.SUCCESS
    assert balances(conn) == expected_balances()
    assert reconcile.run_reconciliation(conn).passed       # includes: the FAILED run left no rows
    conn.close()


# --- edge-case files -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["header_only.csv", "zero_bytes.csv"])
def test_empty_batches_succeed(sample_db: sqlite3.Connection, name: str) -> None:
    before = balances(sample_db)
    result = pipeline.run_file(sample_db, EDGE / name, EDGE)
    assert (result.status, result.rows_read) == ("SUCCESS", 0)
    assert balances(sample_db) == before


def test_zero_bytes_file_really_is_empty() -> None:
    assert (EDGE / "zero_bytes.csv").stat().st_size == 0


@pytest.mark.parametrize("name, missing_text", [
    ("missing_required_column.csv", "missing required columns: quantity"),
    ("duplicate_column.csv", "duplicate column names: sku"),
])
def test_bad_header_files_are_rejected(sample_db: sqlite3.Connection, name: str, missing_text: str) -> None:
    before = (count(sample_db, "movement_event"), count(sample_db, "quarantine_movement"))
    result = pipeline.run_file(sample_db, EDGE / name, EDGE)
    assert result.status == pipeline.REJECTED
    assert missing_text in (result.error_message or "")
    assert (count(sample_db, "movement_event"), count(sample_db, "quarantine_movement")) == before


def test_extra_and_reordered_columns_load_with_warning(sample_db: sqlite3.Connection) -> None:
    result = pipeline.run_file(sample_db, EDGE / "extra_and_reordered_columns.csv", EDGE)
    assert result.status == pipeline.SUCCESS
    assert result.schema_warnings == ["extra columns ignored: batch_note"]
    assert closing(sample_db, "SKU-001", "WH-01")["2026-03-04"] == 98
    assert reconcile.run_reconciliation(sample_db).passed


# --- the demo command on the real sample ------------------------------------------------


def test_demo_on_real_sample(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert cli.main(["demo", "--output-dir", str(out)]) == 0
    exported = read_csv(out / "daily_inventory_balance.csv")
    assert len(exported) == 21
    report = (out / "reconciliation_report.md").read_text(encoding="utf-8")
    assert "**Overall status: WARN**" in report
    shutil.rmtree(out)
