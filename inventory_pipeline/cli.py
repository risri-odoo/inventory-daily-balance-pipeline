"""Command-line interface: `python -m inventory_pipeline <command>`.

Commands are added phase by phase. Currently available: init, load-reference, run.
The exit code is non-zero when a run is FAILED or REJECTED.
"""

import argparse
import sqlite3
from pathlib import Path
from typing import Optional, Sequence

from inventory_pipeline import config, db, pipeline
from inventory_pipeline.logs import close_logging, setup_logging


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

    return parser


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
    print(f"event=init status=SUCCESS db_path={db_path.as_posix()}")
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse the arguments, run the chosen command and return its exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "load-reference":
        return cmd_load_reference(args)
    if args.command == "run":
        return cmd_run(args)
    raise ValueError(f"unknown command: {args.command}")
