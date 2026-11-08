"""Event loading: validate each row, detect duplicates, insert into history, track what changed.

For every data row of a file, in order:
1. A malformed row, or one that fails validation, goes to quarantine_movement.
2. Duplicate check on (movement_id, version) against everything already in movement_event.
   Rows are inserted as we go (inside the run's transaction), so this also catches a
   duplicate of an earlier row in the same file. The first arrival wins:
   - same payload_hash      -> DUPLICATE_EVENT        (dropped, counted as duplicate)
   - different payload_hash -> CONFLICTING_DUPLICATE  (rejected)
   Both are written to quarantine_movement so every row stays traceable.
3. Otherwise the row is appended to movement_event (history is never updated or deleted).
   - It becomes the CURRENT version if its version is higher than any version seen so far.
     Then its ledger legs, and the legs of the version it replaces, are marked as affected.
   - If a higher version already exists (versions arrived out of order), it is history only
     and affects no balances.
   - It is LATE if its business_date is before the max business_date loaded before this file.
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from inventory_pipeline import validate
from inventory_pipeline.ingest import HeaderCheck, MovementFile, RawRow, best_effort_movement_id
from inventory_pipeline.validate import Movement, ReferenceData, RejectError, ValidationResult

Pair = tuple[str, str]   # (sku, location_id)


@dataclass
class LoadStats:
    """Counts for one file, plus the earliest affected date per (sku, location) pair."""

    rows_read: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    rows_duplicate: int = 0
    rows_late: int = 0
    rows_superseding: int = 0
    affected: dict[Pair, str] = field(default_factory=dict)

    @property
    def min_affected_date(self) -> Optional[str]:
        return min(self.affected.values()) if self.affected else None


def movement_legs(movement_type: str, sku: str, location_id: str, to_location_id: Optional[str]) -> list[Pair]:
    """The (sku, location) pairs a movement touches: two for a TRANSFER, one otherwise."""
    pairs = [(sku, location_id)]
    if movement_type == "TRANSFER" and to_location_id:
        pairs.append((sku, to_location_id))
    return pairs


def mark_affected(affected: dict[Pair, str], pair: Pair, business_date: str) -> None:
    """Remember the earliest affected date for a pair."""
    if pair not in affected or business_date < affected[pair]:
        affected[pair] = business_date


def max_loaded_business_date(conn: sqlite3.Connection) -> Optional[str]:
    """The latest business_date in movement_event (used for late detection and as the horizon)."""
    return conn.execute("SELECT MAX(business_date) FROM movement_event").fetchone()[0]


def find_event(conn: sqlite3.Connection, movement_id: str, version: int) -> Optional[sqlite3.Row]:
    """Return the stored event with this key, if any."""
    return conn.execute(
        "SELECT payload_hash, run_id, source_file, source_line FROM movement_event WHERE movement_id = ? AND version = ?",
        (movement_id, version),
    ).fetchone()


def find_current_version(conn: sqlite3.Connection, movement_id: str) -> Optional[sqlite3.Row]:
    """Return the highest stored version of a movement, if any."""
    return conn.execute(
        """
        SELECT version, movement_type, sku, location_id, to_location_id, business_date
        FROM movement_event
        WHERE movement_id = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (movement_id,),
    ).fetchone()


def insert_quarantine(
    conn: sqlite3.Connection,
    run_id: str,
    source_file: str,
    row: RawRow,
    movement_id: Optional[str],
    result: ValidationResult,
    quarantined_at: str,
) -> None:
    """Write one rejected (or duplicate) row to quarantine_movement."""
    conn.execute(
        """
        INSERT INTO quarantine_movement
            (run_id, source_file, source_line, movement_id, reject_reason, reject_details, raw_record, quarantined_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, source_file, row.source_line, movement_id, result.primary_reason,
         result.details_json(), row.raw_record, quarantined_at),
    )


def insert_event(
    conn: sqlite3.Connection,
    movement: Movement,
    is_late: bool,
    run_id: str,
    source_file: str,
    source_line: int,
    ingested_at: str,
) -> None:
    """Append one accepted movement to movement_event."""
    conn.execute(
        """
        INSERT INTO movement_event
            (movement_id, version, movement_type, sku, location_id, to_location_id, quantity,
             business_date, event_ts, reason_code, source_system, payload_hash, is_late,
             run_id, source_file, source_line, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (movement.movement_id, movement.version, movement.movement_type, movement.sku,
         movement.location_id, movement.to_location_id, movement.quantity, movement.business_date,
         movement.event_ts, movement.reason_code, movement.source_system, movement.payload_hash,
         1 if is_late else 0, run_id, source_file, source_line, ingested_at),
    )


def duplicate_result(movement: Movement, existing: sqlite3.Row) -> ValidationResult:
    """Build the DUPLICATE_EVENT or CONFLICTING_DUPLICATE result for a repeated key."""
    first_seen = f"first loaded by run {existing['run_id']} from {existing['source_file']} line {existing['source_line']}"
    key = f"({movement.movement_id}, v{movement.version})"
    if existing["payload_hash"] == movement.payload_hash:
        error = RejectError(validate.DUPLICATE_EVENT, f"identical copy of {key}, {first_seen}")
    else:
        error = RejectError(validate.CONFLICTING_DUPLICATE, f"different payload for {key}, {first_seen}")
    return ValidationResult(errors=[error])


def load_rows(
    conn: sqlite3.Connection,
    movement_file: MovementFile,
    reference: ReferenceData,
    run_id: str,
    source_file: str,
    loaded_at: str,
) -> LoadStats:
    """Validate and load every row of a file. Call inside the run's transaction."""
    stats = LoadStats()
    header: HeaderCheck = movement_file.header
    late_cutoff = max_loaded_business_date(conn)

    for row in movement_file.rows:
        stats.rows_read += 1
        movement_id = best_effort_movement_id(row, header)

        # 1. Structure and business-rule validation.
        if row.fields is None:
            result = validate.malformed_row_result(len(header.columns), len(row.values))
        else:
            result = validate.validate_row(row.fields, reference)
        if not result.is_valid or result.movement is None:
            insert_quarantine(conn, run_id, source_file, row, movement_id, result, loaded_at)
            stats.rows_rejected += 1
            continue
        movement = result.movement

        # 2. Duplicate check on (movement_id, version); the first arrival wins.
        existing = find_event(conn, movement.movement_id, movement.version)
        if existing is not None:
            dup = duplicate_result(movement, existing)
            insert_quarantine(conn, run_id, source_file, row, movement_id, dup, loaded_at)
            if dup.primary_reason == validate.DUPLICATE_EVENT:
                stats.rows_duplicate += 1
            else:
                stats.rows_rejected += 1
            continue

        # 3. Accept: decide current vs. history-only before inserting.
        previous = find_current_version(conn, movement.movement_id)
        is_late = late_cutoff is not None and movement.business_date < late_cutoff
        insert_event(conn, movement, is_late, run_id, source_file, row.source_line, loaded_at)
        stats.rows_accepted += 1
        if is_late:
            stats.rows_late += 1

        if previous is None or movement.version > previous["version"]:
            # The new row is now the current version: its legs change balances...
            for pair in movement_legs(movement.movement_type, movement.sku, movement.location_id, movement.to_location_id):
                mark_affected(stats.affected, pair, movement.business_date)
            # ...and so do the legs of the version it replaces (they no longer count).
            if previous is not None:
                stats.rows_superseding += 1
                for pair in movement_legs(previous["movement_type"], previous["sku"], previous["location_id"], previous["to_location_id"]):
                    mark_affected(stats.affected, pair, previous["business_date"])
        # else: an older version arriving after a newer one is history only.

    return stats
