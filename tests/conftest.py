"""Shared test fixtures: a small reference data set, a fresh database, and a batch-file writer."""

import sqlite3
from pathlib import Path
from typing import Callable, Iterator

import pytest

from inventory_pipeline import db, pipeline

HEADER = "movement_id,version,movement_type,sku,location_id,to_location_id,quantity,business_date,event_ts,reason_code,source_system"

# Opening balances: S1@A = 100 and S1@B = 20 at the end of 2026-01-31. S2 has no opening balance.
REFERENCE_FILES = {
    "dim_sku.csv": "sku,description,uom\nS1,Widget,EA\nS2,Gadget,EA\n",
    "dim_location.csv": "location_id,location_name,location_type\nA,Warehouse A,WAREHOUSE\nB,Store B,STORE\nC,Store C,STORE\n",
    "opening_balance.csv": "sku,location_id,as_of_date,quantity\nS1,A,2026-01-31,100\nS1,B,2026-01-31,20\n",
}


def write_reference(ref_dir: Path, files: dict[str, str] = REFERENCE_FILES) -> Path:
    ref_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (ref_dir / name).write_bytes(text.encode("utf-8"))
    return ref_dir


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A fresh database with the schema and the reference data loaded."""
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    result = pipeline.run_load_reference(connection, write_reference(tmp_path / "reference"))
    assert result.status == pipeline.SUCCESS, result.error_message
    yield connection
    connection.close()


@pytest.fixture
def input_dir(tmp_path: Path) -> Path:
    path = tmp_path / "input"
    path.mkdir()
    return path


BatchWriter = Callable[[str, list[str]], Path]


@pytest.fixture
def write_batch(input_dir: Path) -> BatchWriter:
    """write_batch('b1.csv', [row, row, ...]) writes a movement file with the standard header."""
    def _write(name: str, rows: list[str], header: str = HEADER) -> Path:
        path = input_dir / name
        path.write_bytes(("\n".join([header] + rows) + "\n").encode("utf-8"))
        return path
    return _write


def mv(movement_id: str, movement_type: str, sku: str, location: str, qty: int, day: str,
       version: int = 1, to_location: str = "") -> str:
    """One movement CSV line; event_ts is the business date at 12:00 UTC."""
    return f"{movement_id},{version},{movement_type},{sku},{location},{to_location},{qty},{day},{day}T12:00:00Z,,TEST"


def balances(connection: sqlite3.Connection) -> list[tuple]:
    """All balance rows without run_id, sorted, for comparing tables."""
    return [tuple(row) for row in connection.execute(
        """
        SELECT sku, location_id, balance_date, opening_qty, receipt_qty, issue_qty, transfer_in_qty,
               transfer_out_qty, adjustment_qty, net_change_qty, closing_qty
        FROM daily_inventory_balance ORDER BY sku, location_id, balance_date
        """
    )]


def closing(connection: sqlite3.Connection, sku: str, location: str) -> dict[str, int]:
    """{balance_date: closing_qty} for one pair."""
    return {row[0]: row[1] for row in connection.execute(
        "SELECT balance_date, closing_qty FROM daily_inventory_balance WHERE sku = ? AND location_id = ? ORDER BY balance_date",
        (sku, location),
    )}


def count(connection: sqlite3.Connection, table: str) -> int:
    return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
