"""Database helpers: open a connection, create the schema, run a transaction."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from inventory_pipeline import schema


def utc_now_iso() -> str:
    """Return the current UTC time as 'YYYY-MM-DDTHH:MM:SSZ'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database at db_path.

    The connection is in autocommit mode (isolation_level=None): nothing is wrapped in a
    transaction implicitly. Code that needs atomicity uses `transaction()` explicitly, which
    makes the BEGIN / COMMIT / ROLLBACK points easy to see.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create every table, index and view if it does not exist yet. Safe to run repeatedly."""
    for statement in schema.ALL_STATEMENTS:
        conn.executescript(statement)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside BEGIN ... COMMIT; on any exception, ROLLBACK and re-raise."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def list_objects(conn: sqlite3.Connection, object_type: str) -> list[str]:
    """Return the names of all objects of a type ('table' or 'view'), sorted."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%' ORDER BY name",
        (object_type,),
    ).fetchall()
    return [row["name"] for row in rows]
