"""Reference data: load dim_sku, dim_location and opening_balance from CSV with upserts.

Upserts only change a row when a value actually differs, so reloading the same files
is a no-op (no rows change, loaded_at stays the same). Rows missing from a newer file are
kept, not deleted.

Reference files are trusted master data: any problem (missing file or column, bad value,
duplicate key, unknown SKU/location in opening_balance) raises ReferenceDataError and the
whole load is rolled back, rather than quarantining individual rows.
"""

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from inventory_pipeline.validate import ReferenceData, parse_business_date, parse_quantity

DIM_SKU_FILE = "dim_sku.csv"
DIM_LOCATION_FILE = "dim_location.csv"
OPENING_BALANCE_FILE = "opening_balance.csv"


class ReferenceDataError(ValueError):
    """A reference file is missing or contains invalid data."""


@dataclass
class ReferenceLoadStats:
    """What a reference load read and changed."""

    sku_rows: int
    location_rows: int
    opening_rows: int
    rows_changed: int          # inserted or updated rows across all three tables
    opening_changed: int       # inserted or updated opening_balance rows (triggers a full rebuild)


def read_reference_csv(path: Path, required_columns: list[str]) -> list[dict[str, str]]:
    """Read a reference CSV into a list of {column: trimmed value} dicts."""
    if not path.exists():
        raise ReferenceDataError(f"reference file not found: {path.name}")
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = [name.strip().lower() for name in (reader.fieldnames or [])]
        missing = [name for name in required_columns if name not in columns]
        if missing:
            raise ReferenceDataError(f"{path.name}: missing columns {', '.join(missing)}")
        rows = []
        for raw in reader:
            row = {str(key).strip().lower(): (value or "").strip() for key, value in raw.items() if key is not None}
            if any(row.values()):
                rows.append(row)
    return rows


def check_unique(rows: list[dict[str, str]], key_columns: list[str], file_name: str) -> None:
    """Raise if a key is blank or appears twice in the file."""
    seen: set[tuple[str, ...]] = set()
    for line_index, row in enumerate(rows, start=2):
        key = tuple(row[column] for column in key_columns)
        if any(part == "" for part in key):
            raise ReferenceDataError(f"{file_name} line {line_index}: blank key {key_columns}")
        if key in seen:
            raise ReferenceDataError(f"{file_name} line {line_index}: duplicate key {key}")
        seen.add(key)


def upsert_dim_sku(conn: sqlite3.Connection, rows: list[dict[str, str]], loaded_at: str) -> None:
    """Insert new SKUs; update description/uom only if they changed."""
    conn.executemany(
        """
        INSERT INTO dim_sku (sku, description, uom, loaded_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (sku) DO UPDATE SET
            description = excluded.description,
            uom = excluded.uom,
            loaded_at = excluded.loaded_at
        WHERE dim_sku.description IS NOT excluded.description
           OR dim_sku.uom IS NOT excluded.uom
        """,
        [(r["sku"], r.get("description") or None, r.get("uom") or None, loaded_at) for r in rows],
    )


def upsert_dim_location(conn: sqlite3.Connection, rows: list[dict[str, str]], loaded_at: str) -> None:
    """Insert new locations; update name/type only if they changed."""
    conn.executemany(
        """
        INSERT INTO dim_location (location_id, location_name, location_type, loaded_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (location_id) DO UPDATE SET
            location_name = excluded.location_name,
            location_type = excluded.location_type,
            loaded_at = excluded.loaded_at
        WHERE dim_location.location_name IS NOT excluded.location_name
           OR dim_location.location_type IS NOT excluded.location_type
        """,
        [(r["location_id"], r.get("location_name") or None, r.get("location_type") or None, loaded_at) for r in rows],
    )


def parse_opening_rows(rows: list[dict[str, str]], known_skus: set[str], known_locations: set[str]) -> list[tuple[str, str, str, int]]:
    """Check opening balance rows and return (sku, location_id, as_of_date, quantity) tuples."""
    parsed = []
    for line_index, row in enumerate(rows, start=2):
        where = f"{OPENING_BALANCE_FILE} line {line_index}"
        if row["sku"] not in known_skus:
            raise ReferenceDataError(f"{where}: unknown sku '{row['sku']}'")
        if row["location_id"] not in known_locations:
            raise ReferenceDataError(f"{where}: unknown location_id '{row['location_id']}'")
        as_of_date = parse_business_date(row["as_of_date"])
        if as_of_date is None:
            raise ReferenceDataError(f"{where}: invalid as_of_date '{row['as_of_date']}'")
        quantity = parse_quantity(row["quantity"])
        if quantity is None:
            raise ReferenceDataError(f"{where}: quantity must be a whole number, got '{row['quantity']}'")
        parsed.append((row["sku"], row["location_id"], as_of_date, quantity))
    return parsed


def upsert_opening_balance(conn: sqlite3.Connection, rows: list[tuple[str, str, str, int]], loaded_at: str) -> None:
    """Insert new opening balances; update a pair only if its date or quantity changed."""
    conn.executemany(
        """
        INSERT INTO opening_balance (sku, location_id, as_of_date, quantity, loaded_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (sku, location_id) DO UPDATE SET
            as_of_date = excluded.as_of_date,
            quantity = excluded.quantity,
            loaded_at = excluded.loaded_at
        WHERE opening_balance.as_of_date IS NOT excluded.as_of_date
           OR opening_balance.quantity IS NOT excluded.quantity
        """,
        [row + (loaded_at,) for row in rows],
    )


def load_reference_files(conn: sqlite3.Connection, ref_dir: Path, loaded_at: str) -> ReferenceLoadStats:
    """Read and upsert all three reference files. Call inside a transaction."""
    ref_dir = Path(ref_dir)
    sku_rows = read_reference_csv(ref_dir / DIM_SKU_FILE, ["sku", "description", "uom"])
    location_rows = read_reference_csv(ref_dir / DIM_LOCATION_FILE, ["location_id", "location_name", "location_type"])
    opening_raw = read_reference_csv(ref_dir / OPENING_BALANCE_FILE, ["sku", "location_id", "as_of_date", "quantity"])
    check_unique(sku_rows, ["sku"], DIM_SKU_FILE)
    check_unique(location_rows, ["location_id"], DIM_LOCATION_FILE)
    check_unique(opening_raw, ["sku", "location_id"], OPENING_BALANCE_FILE)

    changes_before = conn.total_changes
    upsert_dim_sku(conn, sku_rows, loaded_at)
    upsert_dim_location(conn, location_rows, loaded_at)
    dims_changed = conn.total_changes - changes_before

    # Check opening balances against everything now in the dimension tables.
    reference = fetch_reference_data(conn)
    opening_rows = parse_opening_rows(opening_raw, reference.skus, reference.locations)
    changes_before = conn.total_changes
    upsert_opening_balance(conn, opening_rows, loaded_at)
    opening_changed = conn.total_changes - changes_before

    return ReferenceLoadStats(
        sku_rows=len(sku_rows),
        location_rows=len(location_rows),
        opening_rows=len(opening_rows),
        rows_changed=dims_changed + opening_changed,
        opening_changed=opening_changed,
    )


def fetch_reference_data(conn: sqlite3.Connection) -> ReferenceData:
    """Read the current reference data from the database, for validation."""
    skus = {row[0] for row in conn.execute("SELECT sku FROM dim_sku")}
    locations = {row[0] for row in conn.execute("SELECT location_id FROM dim_location")}
    opening_dates = {
        (row[0], row[1]): row[2]
        for row in conn.execute("SELECT sku, location_id, as_of_date FROM opening_balance")
    }
    return ReferenceData(skus=skus, locations=locations, opening_dates=opening_dates)
