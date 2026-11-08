"""Command-line interface: `python -m inventory_pipeline <command>`.

Commands are added phase by phase. Currently available: init.
"""

import argparse
from pathlib import Path
from typing import Optional, Sequence

from inventory_pipeline import config, db


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

    return parser


def resolve_db_path(args: argparse.Namespace) -> Path:
    """Pick the database path: --db, else --output-dir/inventory.db, else the env/default."""
    if args.db is not None:
        return args.db
    if args.output_dir is not None:
        return args.output_dir / config.DEFAULT_DB_FILENAME
    return config.default_db_path()


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse the arguments, run the chosen command and return its exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    raise ValueError(f"unknown command: {args.command}")
