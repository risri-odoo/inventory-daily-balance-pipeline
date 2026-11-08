"""Orchestration: process one movement file (or a folder of them) end to end.

Steps for one file (section 7 of the spec):
1. Insert a pipeline_run row with status RUNNING and commit it at once, so even a crash
   leaves a visible trace.
2. If the file's SHA-256 is already in processed_file and --force is not set: SKIPPED.
3. Check the header contract. A missing required column or a duplicate column: REJECTED.
4. In ONE transaction: validate and load rows (events + quarantine), recompute balances,
   mark the file processed, mark the run SUCCESS, COMMIT.
   On any exception: ROLLBACK (nothing from this file remains) and mark the run FAILED with
   the error, so a retry starts clean.

--fail-at after_load | after_rebuild raises an error inside the transaction on purpose,
to demonstrate partial failure and retry.
"""

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from inventory_pipeline import balances, db, ingest, load, reference
from inventory_pipeline.logs import get_logger, log_event

RUNNING = "RUNNING"
SUCCESS = "SUCCESS"
SKIPPED = "SKIPPED"
REJECTED = "REJECTED"
FAILED = "FAILED"

FAIL_AFTER_LOAD = "after_load"
FAIL_AFTER_REBUILD = "after_rebuild"
FAIL_POINTS = [FAIL_AFTER_LOAD, FAIL_AFTER_REBUILD]


class InjectedFailure(RuntimeError):
    """Raised on purpose by --fail-at to test rollback and retry."""


@dataclass
class RunResult:
    """Everything recorded about one run in pipeline_run."""

    run_id: str
    source_file: Optional[str]
    file_sha256: Optional[str]
    started_at: str
    status: str = RUNNING
    ended_at: Optional[str] = None
    duration_ms: Optional[int] = None
    rows_read: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    rows_duplicate: int = 0
    rows_late: int = 0
    rows_superseding: int = 0
    min_affected_date: Optional[str] = None
    pairs_rebuilt: int = 0
    balance_rows_written: int = 0
    negative_balance_rows: int = 0
    schema_warnings: list[str] = field(default_factory=list)
    stage_timings_ms: dict[str, int] = field(default_factory=dict)
    error_message: Optional[str] = None


def new_run_id() -> str:
    """A unique, sortable run id: run-<UTC time>-<8 random hex characters>."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{uuid.uuid4().hex[:8]}"


def insert_running(conn: sqlite3.Connection, result: RunResult) -> None:
    """Insert the RUNNING row. The connection is in autocommit mode, so it is committed at once."""
    conn.execute(
        "INSERT INTO pipeline_run (run_id, source_file, file_sha256, status, started_at) VALUES (?, ?, ?, ?, ?)",
        (result.run_id, result.source_file, result.file_sha256, RUNNING, result.started_at),
    )


def save_run(conn: sqlite3.Connection, result: RunResult) -> None:
    """Write the final state of a run to its pipeline_run row."""
    conn.execute(
        """
        UPDATE pipeline_run SET
            status = ?, ended_at = ?, duration_ms = ?,
            rows_read = ?, rows_accepted = ?, rows_rejected = ?, rows_duplicate = ?,
            rows_late = ?, rows_superseding = ?, min_affected_date = ?,
            pairs_rebuilt = ?, balance_rows_written = ?, negative_balance_rows = ?,
            schema_warnings = ?, stage_timings_ms = ?, error_message = ?
        WHERE run_id = ?
        """,
        (result.status, result.ended_at, result.duration_ms,
         result.rows_read, result.rows_accepted, result.rows_rejected, result.rows_duplicate,
         result.rows_late, result.rows_superseding, result.min_affected_date,
         result.pairs_rebuilt, result.balance_rows_written, result.negative_balance_rows,
         json.dumps(result.schema_warnings), json.dumps(result.stage_timings_ms), result.error_message,
         result.run_id),
    )


def finish(result: RunResult, status: str, clock_start: float, error_message: Optional[str] = None) -> None:
    """Set the end status, end time and duration on the result (not yet saved)."""
    result.status = status
    result.ended_at = db.utc_now_iso()
    result.duration_ms = int((time.perf_counter() - clock_start) * 1000)
    result.error_message = error_message


def is_processed(conn: sqlite3.Connection, file_sha256: str) -> bool:
    """True if a file with exactly these bytes was already processed successfully."""
    row = conn.execute("SELECT 1 FROM processed_file WHERE file_sha256 = ?", (file_sha256,)).fetchone()
    return row is not None


def mark_processed(conn: sqlite3.Connection, result: RunResult) -> None:
    """Record a successfully processed file. Keeps the first run_id if it is already there (--force)."""
    conn.execute(
        """
        INSERT INTO processed_file (file_sha256, source_file, run_id, processed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (file_sha256) DO NOTHING
        """,
        (result.file_sha256, result.source_file, result.run_id, db.utc_now_iso()),
    )


def log_run(logger: logging.Logger, result: RunResult) -> None:
    """One summary log line per run."""
    level = logging.ERROR if result.status in (FAILED, REJECTED) else logging.INFO
    log_event(
        logger, "run_finished", level,
        run_id=result.run_id, source_file=result.source_file, status=result.status,
        rows_read=result.rows_read, rows_accepted=result.rows_accepted, rows_rejected=result.rows_rejected,
        rows_duplicate=result.rows_duplicate, rows_late=result.rows_late, rows_superseding=result.rows_superseding,
        min_affected_date=result.min_affected_date, pairs_rebuilt=result.pairs_rebuilt,
        balance_rows_written=result.balance_rows_written, negative_balance_rows=result.negative_balance_rows,
        duration_ms=result.duration_ms, error=result.error_message,
    )


def run_file(
    conn: sqlite3.Connection,
    path: Path,
    input_root: Path,
    force: bool = False,
    full_rebuild: bool = False,
    fail_at: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> RunResult:
    """Process one movement file and return what happened. Never raises for data problems."""
    logger = logger or get_logger()
    clock_start = time.perf_counter()
    path = Path(path)
    result = RunResult(
        run_id=new_run_id(),
        source_file=ingest.relative_source_file(path, input_root),
        file_sha256=ingest.file_sha256(path),
        started_at=db.utc_now_iso(),
    )

    # Step 1: RUNNING row, committed immediately.
    insert_running(conn, result)
    log_event(logger, "run_started", run_id=result.run_id, source_file=result.source_file,
              file_sha256=result.file_sha256, force=force, full_rebuild=full_rebuild, fail_at=fail_at)

    try:
        # Step 2: skip a file that was already processed.
        if not force and result.file_sha256 is not None and is_processed(conn, result.file_sha256):
            finish(result, SKIPPED, clock_start, "file already processed (same SHA-256); use --force to reprocess")
            save_run(conn, result)
            log_run(logger, result)
            return result

        # Step 3: header contract.
        stage_start = time.perf_counter()
        movement_file = ingest.read_movement_file(path)
        result.stage_timings_ms["read"] = int((time.perf_counter() - stage_start) * 1000)
        result.schema_warnings = movement_file.header.warnings()
        for warning in result.schema_warnings:
            log_event(logger, "schema_warning", logging.WARNING, run_id=result.run_id,
                      source_file=result.source_file, warning=warning)
        if not movement_file.header.is_valid:
            finish(result, REJECTED, clock_start, movement_file.header.rejection_message())
            save_run(conn, result)
            log_run(logger, result)
            return result

        # Step 4: everything else in one transaction.
        with db.transaction(conn):
            stage_start = time.perf_counter()
            reference_data = reference.fetch_reference_data(conn)
            stats = load.load_rows(conn, movement_file, reference_data, result.run_id,
                                   result.source_file or "", result.started_at)
            result.stage_timings_ms["validate_load"] = int((time.perf_counter() - stage_start) * 1000)
            result.rows_read = stats.rows_read
            result.rows_accepted = stats.rows_accepted
            result.rows_rejected = stats.rows_rejected
            result.rows_duplicate = stats.rows_duplicate
            result.rows_late = stats.rows_late
            result.rows_superseding = stats.rows_superseding
            result.min_affected_date = stats.min_affected_date

            if fail_at == FAIL_AFTER_LOAD:
                raise InjectedFailure("injected failure after_load (--fail-at)")

            stage_start = time.perf_counter()
            if full_rebuild:
                rebuild = balances.full_rebuild(conn, result.run_id)
            else:
                rebuild = balances.incremental_rebuild(conn, stats.affected, result.run_id)
            result.pairs_rebuilt = rebuild.pairs_rebuilt
            result.balance_rows_written = rebuild.rows_written
            result.negative_balance_rows = balances.count_negative_rows(conn)
            result.stage_timings_ms["rebuild"] = int((time.perf_counter() - stage_start) * 1000)

            if fail_at == FAIL_AFTER_REBUILD:
                raise InjectedFailure("injected failure after_rebuild (--fail-at)")

            mark_processed(conn, result)
            finish(result, SUCCESS, clock_start)
            save_run(conn, result)
        # COMMIT happened when the `with` block ended without an error.

    except Exception as error:  # noqa: BLE001 - every failure must be recorded, then reported
        # The transaction (if open) was rolled back; record the failure outside it.
        message = f"{type(error).__name__}: {error}"
        result_failed = RunResult(run_id=result.run_id, source_file=result.source_file,
                                  file_sha256=result.file_sha256, started_at=result.started_at,
                                  schema_warnings=result.schema_warnings,
                                  stage_timings_ms=result.stage_timings_ms)
        finish(result_failed, FAILED, clock_start, message)
        save_run(conn, result_failed)
        log_run(logger, result_failed)
        return result_failed

    log_run(logger, result)
    return result


def list_input_files(input_path: Path) -> tuple[list[Path], Path]:
    """Return the CSV files to process (in filename order) and the input root for relative paths."""
    input_path = Path(input_path)
    if input_path.is_dir():
        return sorted(input_path.glob("*.csv"), key=lambda p: p.name), input_path
    if input_path.is_file():
        return [input_path], input_path.parent
    raise FileNotFoundError(f"input not found: {input_path}")


def run_input(
    conn: sqlite3.Connection,
    input_path: Path,
    force: bool = False,
    full_rebuild: bool = False,
    fail_at: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> list[RunResult]:
    """Process a file, or every *.csv in a folder in filename order.

    Processing stops at the first FAILED file so later batches are never loaded ahead of it;
    a REJECTED file (bad header) does not stop the batches after it.
    """
    files, input_root = list_input_files(input_path)
    results = []
    for path in files:
        result = run_file(conn, path, input_root, force=force, full_rebuild=full_rebuild,
                          fail_at=fail_at, logger=logger)
        results.append(result)
        if result.status == FAILED:
            break
    return results


def run_load_reference(conn: sqlite3.Connection, ref_dir: Path, logger: Optional[logging.Logger] = None) -> RunResult:
    """Load the reference files in one transaction, logged as a pipeline_run.

    If any opening balance was inserted or changed, the balances are fully rebuilt, because
    an opening balance changes every day of that pair.
    """
    logger = logger or get_logger()
    clock_start = time.perf_counter()
    result = RunResult(run_id=new_run_id(), source_file="reference", file_sha256=None, started_at=db.utc_now_iso())
    insert_running(conn, result)
    log_event(logger, "run_started", run_id=result.run_id, source_file=result.source_file)
    try:
        with db.transaction(conn):
            stats = reference.load_reference_files(conn, Path(ref_dir), result.started_at)
            log_event(logger, "reference_loaded", run_id=result.run_id, sku_rows=stats.sku_rows,
                      location_rows=stats.location_rows, opening_rows=stats.opening_rows,
                      rows_changed=stats.rows_changed, opening_changed=stats.opening_changed)
            if stats.opening_changed > 0:
                rebuild = balances.full_rebuild(conn, result.run_id)
                result.pairs_rebuilt = rebuild.pairs_rebuilt
                result.balance_rows_written = rebuild.rows_written
                result.negative_balance_rows = balances.count_negative_rows(conn)
            finish(result, SUCCESS, clock_start)
            save_run(conn, result)
    except Exception as error:  # noqa: BLE001
        failed = RunResult(run_id=result.run_id, source_file=result.source_file, file_sha256=None,
                           started_at=result.started_at)
        finish(failed, FAILED, clock_start, f"{type(error).__name__}: {error}")
        save_run(conn, failed)
        log_run(logger, failed)
        return failed
    log_run(logger, result)
    return result
