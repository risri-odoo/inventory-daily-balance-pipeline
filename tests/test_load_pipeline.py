"""Loading, duplicates, versions, balances, idempotency, transactions and fault injection.

Reference data (see conftest.py): S1@A opens at 100 and S1@B at 20 (end of 2026-01-31);
S2 has no opening balance; locations A, B, C.
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import BatchWriter, balances, closing, count, mv, write_reference
from inventory_pipeline import balances as bal
from inventory_pipeline import cli, db, pipeline

BATCH_1 = [
    mv("M1", "RECEIPT", "S1", "A", 10, "2026-02-01"),
    mv("M2", "ISSUE", "S1", "A", 3, "2026-02-02"),
    mv("M3", "TRANSFER", "S1", "A", 5, "2026-02-02", to_location="B"),
    mv("M4", "ADJUSTMENT", "S1", "A", -2, "2026-02-03"),
]


def run(conn: sqlite3.Connection, path: Path, **kwargs: object) -> pipeline.RunResult:
    return pipeline.run_file(conn, path, path.parent, **kwargs)  # type: ignore[arg-type]


def full_rebuild_copy(conn: sqlite3.Connection, tmp_path: Path) -> list[tuple]:
    """Copy the database, run a full rebuild on the copy, and return its balances."""
    copy = db.connect(tmp_path / "copy.db")
    conn.backup(copy)
    with db.transaction(copy):
        bal.full_rebuild(copy, "full")
    rows = balances(copy)
    copy.close()
    return rows


# --- happy path ------------------------------------------------------------------


def test_batch_produces_hand_computed_balances(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    result = run(conn, write_batch("b1.csv", BATCH_1))

    assert result.status == pipeline.SUCCESS
    assert (result.rows_read, result.rows_accepted, result.rows_rejected, result.rows_duplicate) == (4, 4, 0, 0)
    # S1@A: 100 +10 (02-01) = 110; -3 issue -5 transfer out (02-02) = 102; -2 adjustment (02-03) = 100
    assert closing(conn, "S1", "A") == {"2026-02-01": 110, "2026-02-02": 102, "2026-02-03": 100}
    # S1@B: 20, +5 transfer in on 02-02 = 25, carried forward to the horizon 02-03
    assert closing(conn, "S1", "B") == {"2026-02-01": 20, "2026-02-02": 25, "2026-02-03": 25}
    assert count(conn, "processed_file") == 1
    row = conn.execute("SELECT status, rows_read, stage_timings_ms FROM pipeline_run WHERE run_id = ?", (result.run_id,)).fetchone()
    assert row["status"] == "SUCCESS" and row["rows_read"] == 4
    assert set(json.loads(row["stage_timings_ms"])) == {"read", "validate_load", "rebuild"}


def test_transfer_creates_two_legs(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    run(conn, write_batch("b1.csv", BATCH_1))
    legs = conn.execute("SELECT leg_type, location_id, qty_delta FROM v_movement_leg WHERE movement_id = 'M3' ORDER BY leg_type").fetchall()
    assert [tuple(leg) for leg in legs] == [("TRANSFER_IN", "B", 5), ("TRANSFER_OUT", "A", -5)]


def test_rows_store_run_id_and_relative_source_file(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    result = run(conn, write_batch("b1.csv", BATCH_1 + ["BAD,1,RECEIPT,NOPE,A,,1,2026-02-01,2026-02-01T00:00:00Z,,X"]))
    event = conn.execute("SELECT run_id, source_file, source_line FROM movement_event WHERE movement_id = 'M1'").fetchone()
    reject = conn.execute("SELECT run_id, source_file, source_line, reject_reason, raw_record FROM quarantine_movement").fetchone()
    assert tuple(event) == (result.run_id, "b1.csv", 2)
    assert (reject["run_id"], reject["source_file"], reject["source_line"], reject["reject_reason"]) == (result.run_id, "b1.csv", 6, "UNKNOWN_SKU")
    assert reject["raw_record"].startswith("BAD,1,RECEIPT,NOPE")


# --- duplicates ------------------------------------------------------------------


def test_duplicates_within_one_file(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    rows = [
        mv("M1", "RECEIPT", "S1", "A", 10, "2026-02-01"),
        mv("M1", "RECEIPT", "S1", "A", 10, "2026-02-01"),   # exact copy -> DUPLICATE_EVENT
        mv("M1", "RECEIPT", "S1", "A", 99, "2026-02-01"),   # same key, other qty -> CONFLICTING_DUPLICATE
    ]
    result = run(conn, write_batch("b1.csv", rows))

    assert (result.rows_read, result.rows_accepted, result.rows_rejected, result.rows_duplicate) == (3, 1, 1, 1)
    reasons = [r[0] for r in conn.execute("SELECT reject_reason FROM quarantine_movement ORDER BY source_line")]
    assert reasons == ["DUPLICATE_EVENT", "CONFLICTING_DUPLICATE"]
    assert closing(conn, "S1", "A")["2026-02-01"] == 110      # first arrival wins


def test_duplicates_across_files_first_arrival_wins(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    run(conn, write_batch("b1.csv", [mv("M1", "RECEIPT", "S1", "A", 10, "2026-02-01")]))
    result = run(conn, write_batch("b2.csv", [
        mv("M1", "RECEIPT", "S1", "A", 10, "2026-02-01"),
        mv("M1", "RECEIPT", "S1", "A", 50, "2026-02-01"),
    ]))
    assert (result.rows_duplicate, result.rows_rejected) == (1, 1)
    details = conn.execute("SELECT reject_details FROM quarantine_movement WHERE reject_reason = 'CONFLICTING_DUPLICATE'").fetchone()[0]
    assert "b1.csv line 2" in details
    assert closing(conn, "S1", "A")["2026-02-01"] == 110


def test_exact_duplicate_ignores_formatting_differences(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    rows = [
        "M1,1,RECEIPT,S1,A,,10,2026-02-01,2026-02-01T12:00:00Z,,TEST",
        "M1,,receipt,S1,A,,+10,2026-02-01,2026-02-01 12:00:00,,TEST",
    ]
    result = run(conn, write_batch("b1.csv", rows))
    assert (result.rows_accepted, result.rows_duplicate, result.rows_rejected) == (1, 1, 0)


# --- versions and corrections ---------------------------------------------------------


def test_correction_replaces_earlier_version(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    run(conn, write_batch("b1.csv", [mv("M1", "RECEIPT", "S1", "A", 10, "2026-02-01")]))
    result = run(conn, write_batch("b2.csv", [mv("M1", "RECEIPT", "S1", "A", 7, "2026-02-01", version=2)]))

    assert result.rows_superseding == 1
    assert closing(conn, "S1", "A")["2026-02-01"] == 107
    assert count(conn, "movement_event") == 2                 # history kept
    assert conn.execute("SELECT version FROM v_movement_current WHERE movement_id = 'M1'").fetchone()[0] == 2


def test_out_of_order_versions_match_in_order_arrival(tmp_path: Path, conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    v1 = mv("M1", "RECEIPT", "S1", "A", 10, "2026-02-01")
    v2 = mv("M1", "RECEIPT", "S1", "A", 7, "2026-02-01", version=2)
    v3_void = mv("M1", "RECEIPT", "S1", "A", 0, "2026-02-01", version=3)

    # Arrival order v1, v3, v2: v3 stays current, v2 goes to history only.
    run(conn, write_batch("b1.csv", [v1]))
    run(conn, write_batch("b2.csv", [v3_void]))
    late_v2 = run(conn, write_batch("b3.csv", [v2]))
    assert late_v2.rows_accepted == 1 and late_v2.rows_superseding == 0
    assert conn.execute("SELECT version FROM v_movement_current WHERE movement_id = 'M1'").fetchone()[0] == 3
    assert closing(conn, "S1", "A")["2026-02-01"] == 100      # voided

    # Same events in order v1, v2, v3 in a second database: identical balances.
    other = db.connect(tmp_path / "other.db")
    db.init_db(other)
    pipeline.run_load_reference(other, write_reference(tmp_path / "ref2"))
    other_dir = tmp_path / "other_input"
    other_dir.mkdir()
    for name, row in [("b1.csv", v1), ("b2.csv", v2), ("b3.csv", v3_void)]:
        path = other_dir / name
        path.write_text(f"{'movement_id,version,movement_type,sku,location_id,to_location_id,quantity,business_date,event_ts,reason_code,source_system'}\n{row}\n")
        pipeline.run_file(other, path, other_dir)
    assert balances(other) == balances(conn)
    other.close()


def test_correction_that_changes_location_moves_stock(tmp_path: Path, conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    run(conn, write_batch("b1.csv", [mv("M1", "RECEIPT", "S2", "A", 10, "2026-02-01"),
                                     mv("M2", "RECEIPT", "S2", "C", 1, "2026-02-02")]))
    assert closing(conn, "S2", "A") == {"2026-02-01": 10, "2026-02-02": 10}

    run(conn, write_batch("b2.csv", [mv("M1", "RECEIPT", "S2", "B", 10, "2026-02-01", version=2)]))

    assert closing(conn, "S2", "A") == {}                     # its only movement moved away
    assert closing(conn, "S2", "B") == {"2026-02-01": 10, "2026-02-02": 10}
    assert balances(conn) == full_rebuild_copy(conn, tmp_path)


# --- late data and incremental recompute ----------------------------------------------


def test_late_event_recomputes_past_days(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    run(conn, write_batch("b1.csv", BATCH_1))
    late = run(conn, write_batch("b2.csv", [mv("M9", "ISSUE", "S1", "A", 4, "2026-02-01")]))

    assert late.rows_late == 1 and late.min_affected_date == "2026-02-01"
    assert conn.execute("SELECT is_late FROM movement_event WHERE movement_id = 'M9'").fetchone()[0] == 1
    assert closing(conn, "S1", "A") == {"2026-02-01": 106, "2026-02-02": 98, "2026-02-03": 96}
    assert closing(conn, "S1", "B") == {"2026-02-01": 20, "2026-02-02": 25, "2026-02-03": 25}   # untouched


def test_horizon_advance_extends_unaffected_pairs(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    run(conn, write_batch("b1.csv", BATCH_1))
    result = run(conn, write_batch("b2.csv", [mv("M9", "RECEIPT", "S2", "C", 1, "2026-02-05")]))

    assert result.rows_late == 0
    assert closing(conn, "S1", "A")["2026-02-05"] == 100
    assert closing(conn, "S1", "B")["2026-02-05"] == 25
    assert closing(conn, "S2", "C") == {"2026-02-05": 1}      # starts at its first movement


def test_incremental_equals_full_rebuild(tmp_path: Path, conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    run(conn, write_batch("b1.csv", BATCH_1))
    run(conn, write_batch("b2.csv", [
        mv("M5", "RECEIPT", "S2", "C", 8, "2026-02-04"),
        mv("M6", "TRANSFER", "S2", "C", 3, "2026-02-05", to_location="A"),
        mv("M1", "RECEIPT", "S1", "A", 12, "2026-02-01", version=2),
    ]))
    run(conn, write_batch("b3.csv", [
        mv("M7", "ISSUE", "S1", "B", 30, "2026-02-02"),           # late, goes negative
        mv("M6", "TRANSFER", "S2", "C", 3, "2026-02-03", version=2, to_location="B"),
    ]))
    assert balances(conn) == full_rebuild_copy(conn, tmp_path)


def test_full_rebuild_flag_gives_same_result(tmp_path: Path, conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    run(conn, write_batch("b1.csv", BATCH_1))
    result = run(conn, write_batch("b2.csv", [mv("M9", "ISSUE", "S1", "A", 4, "2026-02-01")]), full_rebuild=True)
    assert result.status == pipeline.SUCCESS
    assert closing(conn, "S1", "A") == {"2026-02-01": 106, "2026-02-02": 98, "2026-02-03": 96}


def test_negative_balance_is_counted_not_rejected(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    result = run(conn, write_batch("b1.csv", [mv("M1", "ISSUE", "S2", "C", 5, "2026-02-01")]))
    assert result.rows_accepted == 1
    assert result.negative_balance_rows == 1
    assert closing(conn, "S2", "C") == {"2026-02-01": -5}


# --- idempotency -------------------------------------------------------------------


def test_rerun_same_file_is_skipped(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    path = write_batch("b1.csv", BATCH_1)
    run(conn, path)
    before = (count(conn, "movement_event"), count(conn, "quarantine_movement"), balances(conn))

    again = run(conn, path)

    assert again.status == pipeline.SKIPPED
    assert (count(conn, "movement_event"), count(conn, "quarantine_movement"), balances(conn)) == before


def test_force_rerun_turns_rows_into_duplicates(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    path = write_batch("b1.csv", BATCH_1)
    run(conn, path)
    before = balances(conn)

    forced = run(conn, path, force=True)

    assert forced.status == pipeline.SUCCESS
    assert (forced.rows_read, forced.rows_accepted, forced.rows_duplicate) == (4, 0, 4)
    assert count(conn, "movement_event") == 4
    assert balances(conn) == before


# --- file-level rules ----------------------------------------------------------------


def test_missing_required_column_rejects_file(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    header = "movement_id,version,movement_type,sku,location_id,to_location_id,business_date,event_ts"
    result = run(conn, write_batch("bad.csv", ["M1,1,RECEIPT,S1,A,,2026-02-01,2026-02-01T00:00:00Z"], header=header))

    assert result.status == pipeline.REJECTED
    assert "quantity" in (result.error_message or "")
    assert count(conn, "movement_event") == 0 and count(conn, "processed_file") == 0


def test_extra_column_warns_and_still_loads(conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    header = "batch_note,sku,quantity,movement_id,movement_type,location_id,business_date,event_ts"
    result = run(conn, write_batch("extra.csv", ["hello,S1,10,M1,RECEIPT,A,2026-02-01,2026-02-01T00:00:00Z"], header=header))

    assert result.status == pipeline.SUCCESS
    assert result.schema_warnings == ["extra columns ignored: batch_note"]
    stored = conn.execute("SELECT schema_warnings FROM pipeline_run WHERE run_id = ?", (result.run_id,)).fetchone()[0]
    assert json.loads(stored) == ["extra columns ignored: batch_note"]
    assert closing(conn, "S1", "A")["2026-02-01"] == 110


@pytest.mark.parametrize("content", [b"", b"movement_id,version,movement_type,sku,location_id,to_location_id,quantity,business_date,event_ts\n"])
def test_empty_batch_is_success(conn: sqlite3.Connection, input_dir: Path, content: bytes) -> None:
    path = input_dir / "empty.csv"
    path.write_bytes(content)
    result = run(conn, path)
    assert result.status == pipeline.SUCCESS
    assert result.rows_read == 0


# --- transactions and fault injection -------------------------------------------------


@pytest.mark.parametrize("fail_at", pipeline.FAIL_POINTS)
def test_partial_failure_writes_nothing_then_retry_succeeds(conn: sqlite3.Connection, write_batch: BatchWriter, fail_at: str) -> None:
    run(conn, write_batch("b1.csv", BATCH_1))
    before = (count(conn, "movement_event"), count(conn, "quarantine_movement"), count(conn, "processed_file"), balances(conn))
    path = write_batch("b2.csv", [mv("M9", "ISSUE", "S1", "A", 4, "2026-02-01"), "broken,row"])

    failed = run(conn, path, fail_at=fail_at)

    assert failed.status == pipeline.FAILED
    assert "InjectedFailure" in (failed.error_message or "")
    assert (count(conn, "movement_event"), count(conn, "quarantine_movement"), count(conn, "processed_file"), balances(conn)) == before
    stored = conn.execute("SELECT status, error_message, rows_read FROM pipeline_run WHERE run_id = ?", (failed.run_id,)).fetchone()
    assert stored["status"] == "FAILED" and "after" in stored["error_message"] and stored["rows_read"] == 0

    retry = run(conn, path)

    assert retry.status == pipeline.SUCCESS
    assert (retry.rows_accepted, retry.rows_rejected) == (1, 1)
    assert closing(conn, "S1", "A")["2026-02-01"] == 106


def test_folder_run_stops_at_first_failure(conn: sqlite3.Connection, write_batch: BatchWriter, input_dir: Path) -> None:
    write_batch("b1.csv", BATCH_1)
    write_batch("b2.csv", [mv("M9", "ISSUE", "S1", "A", 4, "2026-02-01")])
    results = pipeline.run_input(conn, input_dir, fail_at="after_load")
    assert [r.status for r in results] == ["FAILED"]
    assert count(conn, "movement_event") == 0


# --- reference data -----------------------------------------------------------------


def test_reference_reload_is_a_noop(tmp_path: Path, conn: sqlite3.Connection) -> None:
    before = conn.execute("SELECT * FROM opening_balance ORDER BY sku, location_id").fetchall()
    result = pipeline.run_load_reference(conn, tmp_path / "reference")
    after = conn.execute("SELECT * FROM opening_balance ORDER BY sku, location_id").fetchall()
    assert result.status == pipeline.SUCCESS
    assert result.pairs_rebuilt == 0                          # no change, no rebuild
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_changed_opening_balance_triggers_full_rebuild(tmp_path: Path, conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    run(conn, write_batch("b1.csv", BATCH_1))
    ref_dir = tmp_path / "reference"
    (ref_dir / "opening_balance.csv").write_text("sku,location_id,as_of_date,quantity\nS1,A,2026-01-31,500\nS1,B,2026-01-31,20\n")

    result = pipeline.run_load_reference(conn, ref_dir)

    assert result.pairs_rebuilt > 0
    assert closing(conn, "S1", "A") == {"2026-02-01": 510, "2026-02-02": 502, "2026-02-03": 500}


def test_bad_reference_file_fails_and_rolls_back(tmp_path: Path, conn: sqlite3.Connection) -> None:
    bad = dict(write_reference.__defaults__[0])  # type: ignore[index]
    bad["opening_balance.csv"] = "sku,location_id,as_of_date,quantity\nS1,ZZZ,2026-01-31,5\n"
    ref_dir = write_reference(tmp_path / "bad_ref", bad)
    before = count(conn, "opening_balance")

    result = pipeline.run_load_reference(conn, ref_dir)

    assert result.status == pipeline.FAILED
    assert "unknown location_id" in (result.error_message or "")
    assert count(conn, "opening_balance") == before


# --- CLI and logging ----------------------------------------------------------------


def test_cli_run_exit_codes_and_log_file(tmp_path: Path) -> None:
    out = tmp_path / "out"
    ref_dir = write_reference(tmp_path / "reference")
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    (input_dir / "b1.csv").write_text("movement_id,movement_type,sku,location_id,quantity,business_date,event_ts\n"
                                      "M1,RECEIPT,S1,A,10,2026-02-01,2026-02-01T00:00:00Z\n")
    (input_dir / "b2.csv").write_text("movement_id,sku\nM2,S1\n")     # missing columns -> REJECTED

    assert cli.main(["load-reference", "--ref-dir", str(ref_dir), "--output-dir", str(out)]) == 0
    assert cli.main(["run", "--input", str(input_dir / "b1.csv"), "--output-dir", str(out)]) == 0
    assert cli.main(["run", "--input", str(input_dir), "--output-dir", str(out)]) == 1   # b1 skipped, b2 rejected

    log_text = (out / "logs" / "pipeline.log").read_text(encoding="utf-8")
    assert "event=run_finished" in log_text and "run_id=run-" in log_text
    assert "status=SKIPPED" in log_text and "status=REJECTED" in log_text
    shutil.rmtree(out)    # the log file must have been released (matters on Windows)
