"""Command-line interface: `python -m inventory_pipeline <command>`.

Commands: init, load-reference, run, reconcile, report, demo.
The exit code is non-zero when a run is FAILED or REJECTED, or a reconciliation check FAILS.
"""

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Optional, Sequence

from inventory_pipeline import config, db, pipeline, reconcile, report
from inventory_pipeline.logs import close_logging, log_event, setup_logging

# data/sample next to the package, so `demo` works from any current directory.
DEFAULT_SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample"
DEMO_DB_FILENAME = "demo.db"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with one sub-command per pipeline action."""
    # Options shared by every command, so they can be written after the command name.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path (default: $INV_DB_PATH or <output-dir>/inventory.db)",
    )
    common.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for logs and reports (default: $INV_OUTPUT_DIR or ./output)",
    )

    parser = argparse.ArgumentParser(
        prog="python -m inventory_pipeline",
        description="Daily inventory balance pipeline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", parents=[common], help="Create the database tables and views")

    load_ref = subparsers.add_parser("load-reference", parents=[common], help="Load dim_sku, dim_location, opening_balance")
    load_ref.add_argument("--ref-dir", type=Path, required=True, help="Folder with the three reference CSV files")

    run = subparsers.add_parser("run", parents=[common], help="Process a movement file or a folder of them")
    run.add_argument("--input", type=Path, required=True, help="A movement CSV file, or a folder (*.csv in filename order)")
    run.add_argument("--force", action="store_true", help="Reprocess files even if their SHA-256 was already processed")
    run.add_argument("--full-rebuild", action="store_true", help="Rebuild all balances instead of only the affected pairs")
    run.add_argument("--fail-at", choices=pipeline.FAIL_POINTS, default=None,
                     help="Raise an error inside the transaction on purpose (to test rollback and retry)")

    subparsers.add_parser("reconcile", parents=[common], help="Run the reconciliation checks (exit 1 if any FAIL)")

    report_parser = subparsers.add_parser("report", parents=[common], help="Write the reconciliation report and CSV exports")
    report_parser.add_argument("--out", type=Path, default=None, help="Folder for the report files (default: the output dir)")

    # demo deliberately has no --db option: it always uses (and recreates) <output-dir>/demo.db,
    # so it can never wipe the database used by the other commands.
    demo_common = argparse.ArgumentParser(add_help=False)
    demo_common.add_argument("--output-dir", type=Path, default=None,
                             help="Directory for demo.db, logs and reports (default: $INV_OUTPUT_DIR or ./output)")
    demo = subparsers.add_parser("demo", parents=[demo_common],
                                 help="Fresh demo.db -> sample reference -> sample input -> reconcile -> report")
    demo.add_argument("--sample-dir", type=Path, default=DEFAULT_SAMPLE_DIR,
                      help="Folder with reference/ and input/ (default: data/sample in this repository)")

    return parser


def display_path(path: Path) -> str:
    """A path for log lines: relative to the current directory, so no usernames or machine paths leak."""
    try:
        return Path(path).resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return Path(path).name


def resolve_output_dir(args: argparse.Namespace) -> Path:
    """Pick the output directory: --output-dir, else $INV_OUTPUT_DIR, else ./output."""
    return args.output_dir if args.output_dir is not None else config.default_output_dir()


def resolve_db_path(args: argparse.Namespace) -> Path:
    """Pick the database path: --db, else --output-dir/inventory.db, else the env/default."""
    if args.db is not None:
        return args.db
    if args.output_dir is not None:
        return args.output_dir / config.DEFAULT_DB_FILENAME
    return config.default_db_path()


def open_database(args: argparse.Namespace) -> sqlite3.Connection:
    """Open the database and make sure the schema exists (init is idempotent)."""
    conn = db.connect(resolve_db_path(args))
    db.init_db(conn)
    return conn


def cmd_init(args: argparse.Namespace) -> int:
    """Create the schema in the configured database."""
    db_path = resolve_db_path(args)
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
    finally:
        conn.close()
    print(f"event=init status=SUCCESS db_path={display_path(db_path)}")
    return 0


def cmd_load_reference(args: argparse.Namespace) -> int:
    """Load the reference files."""
    logger = setup_logging(resolve_output_dir(args))
    conn = open_database(args)
    try:
        result = pipeline.run_load_reference(conn, args.ref_dir, logger)
    finally:
        conn.close()
        close_logging()
    return 0 if result.status == pipeline.SUCCESS else 1


def cmd_run(args: argparse.Namespace) -> int:
    """Process movement files."""
    logger = setup_logging(resolve_output_dir(args))
    conn = open_database(args)
    try:
        results = pipeline.run_input(conn, args.input, force=args.force,
                                     full_rebuild=args.full_rebuild, fail_at=args.fail_at, logger=logger)
    finally:
        conn.close()
        close_logging()
    bad = [r for r in results if r.status in (pipeline.FAILED, pipeline.REJECTED)]
    return 1 if bad else 0


def log_reconciliation(logger: logging.Logger, result: reconcile.ReconciliationReport) -> None:
    """One log line per check plus an overall line."""
    for check in result.checks:
        level = logging.ERROR if check.status == reconcile.FAIL else logging.WARNING if check.status == reconcile.WARN else logging.INFO
        log_event(logger, "check", level, name=check.name, status=check.status,
                  failures=check.failures, details=check.details)
    log_event(logger, "reconciliation_finished", status=result.overall_status)


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Run the reconciliation checks and log the results."""
    logger = setup_logging(resolve_output_dir(args))
    conn = open_database(args)
    try:
        result = reconcile.run_reconciliation(conn)
        log_reconciliation(logger, result)
    finally:
        conn.close()
        close_logging()
    return 0 if result.passed else 1


def cmd_report(args: argparse.Namespace) -> int:
    """Run the checks and write the report files."""
    output_dir = resolve_output_dir(args)
    logger = setup_logging(output_dir)
    conn = open_database(args)
    try:
        result = reconcile.run_reconciliation(conn)
        log_reconciliation(logger, result)
        paths = report.write_report(conn, result, args.out or output_dir)
        for path in paths:
            log_event(logger, "report_written", path=display_path(path))
    finally:
        conn.close()
        close_logging()
    return 0 if result.passed else 1


def cmd_demo(args: argparse.Namespace) -> int:
    """Run the whole pipeline on the sample data in a fresh database."""
    output_dir = resolve_output_dir(args)
    sample_dir = Path(args.sample_dir)
    db_path = output_dir / DEMO_DB_FILENAME
    logger = setup_logging(output_dir)
    try:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        log_event(logger, "demo_started", db_path=display_path(db_path), sample_dir=display_path(sample_dir))

        conn = db.connect(db_path)
        try:
            db.init_db(conn)
            ref_result = pipeline.run_load_reference(conn, sample_dir / "reference", logger)
            if ref_result.status != pipeline.SUCCESS:
                return 1
            results = pipeline.run_input(conn, sample_dir / "input", logger=logger)
            result = reconcile.run_reconciliation(conn)
            log_reconciliation(logger, result)
            paths = report.write_report(conn, result, output_dir)
            for path in paths:
                log_event(logger, "report_written", path=display_path(path))
        finally:
            conn.close()

        bad_runs = [r for r in results if r.status in (pipeline.FAILED, pipeline.REJECTED)]
        exit_code = 0 if result.passed and not bad_runs else 1
        log_event(logger, "demo_finished", files=len(results), bad_runs=len(bad_runs),
                  reconciliation=result.overall_status, exit_code=exit_code)
        return exit_code
    finally:
        close_logging()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse the arguments, run the chosen command and return its exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "load-reference":
        return cmd_load_reference(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "reconcile":
        return cmd_reconcile(args)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "demo":
        return cmd_demo(args)
    raise ValueError(f"unknown command: {args.command}")
