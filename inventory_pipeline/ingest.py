"""Reading movement files: hashing, the header contract and splitting into raw rows.

This module only reads and splits; it does not judge row content (that is validate.py).

Reading rules:
- Files are read as UTF-8 (a leading byte-order mark is ignored).
- One record per physical line. Fully blank lines are skipped and not counted.
- Line 1 is the header. Data rows keep their physical line number as source_line,
  and their original text as raw_record, so every reject can be traced back.
- Header names are matched case-insensitively after trimming spaces.
"""

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REQUIRED_COLUMNS = [
    "movement_id",
    "movement_type",
    "sku",
    "location_id",
    "quantity",
    "business_date",
    "event_ts",
]

OPTIONAL_COLUMNS = [
    "version",
    "to_location_id",
    "reason_code",
    "source_system",
]

KNOWN_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


@dataclass
class HeaderCheck:
    """Result of checking a file's header against the column contract."""

    columns: list[str]                                   # normalised header names, in file order
    missing_columns: list[str] = field(default_factory=list)
    duplicate_columns: list[str] = field(default_factory=list)
    extra_columns: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """A file is rejected if a required column is missing or a column name repeats."""
        return not self.missing_columns and not self.duplicate_columns

    def rejection_message(self) -> str:
        """Human-readable reason why the file was rejected (empty if it is valid)."""
        parts = []
        if self.missing_columns:
            parts.append("missing required columns: " + ", ".join(self.missing_columns))
        if self.duplicate_columns:
            parts.append("duplicate column names: " + ", ".join(self.duplicate_columns))
        return "; ".join(parts)

    def warnings(self) -> list[str]:
        """Schema warnings that do not reject the file (extra columns are ignored)."""
        if self.extra_columns:
            return ["extra columns ignored: " + ", ".join(self.extra_columns)]
        return []


@dataclass
class RawRow:
    """One data line of a movement file, before validation."""

    source_line: int                    # physical line number in the file (header is line 1)
    raw_record: str                     # the original line text, without the line ending
    values: list[str]                   # the parsed CSV values, in file order
    fields: Optional[dict[str, str]]    # column name -> trimmed value; None if field count is wrong

    @property
    def is_malformed(self) -> bool:
        """True when the row's field count does not match the header."""
        return self.fields is None


@dataclass
class MovementFile:
    """A movement file split into its header and raw data rows."""

    header: HeaderCheck
    rows: list[RawRow]


def file_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's bytes (used to skip re-runs of the same file)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_source_file(path: Path, input_root: Path) -> str:
    """Return the file path relative to the input root, with forward slashes.

    Storing relative paths keeps usernames and machine paths out of the database and evidence.
    If the file is not under the root, only the file name is used.
    """
    try:
        return Path(path).resolve().relative_to(Path(input_root).resolve()).as_posix()
    except ValueError:
        return Path(path).name


def normalise_column_name(name: str) -> str:
    """Trim spaces and lower-case a header name, so ' SKU ' matches 'sku'."""
    return name.strip().lower()


def check_header(raw_header: list[str]) -> HeaderCheck:
    """Compare a header row with the column contract."""
    columns = [normalise_column_name(name) for name in raw_header]

    duplicates: list[str] = []
    seen: set[str] = set()
    for name in columns:
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)

    missing = [name for name in REQUIRED_COLUMNS if name not in seen]
    extra = [name for name in columns if name not in KNOWN_COLUMNS]
    return HeaderCheck(
        columns=columns,
        missing_columns=missing,
        duplicate_columns=duplicates,
        extra_columns=extra,
    )


def parse_csv_line(line: str) -> list[str]:
    """Parse one CSV line (handles quoted values containing commas)."""
    return next(csv.reader([line]))


def read_movement_file(path: Path) -> MovementFile:
    """Read a movement file into a header check and raw rows.

    A zero-byte or header-only file returns an empty row list. A zero-byte file has no header
    at all, so it is treated as an empty batch with a valid (empty-contract) header rather than
    as a file with missing columns.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        lines = handle.read().splitlines()

    # Skip leading blank lines to find the header.
    line_number = 0
    while line_number < len(lines) and lines[line_number].strip() == "":
        line_number += 1
    if line_number == len(lines):
        # Zero-byte or blank file: an empty batch.
        return MovementFile(header=HeaderCheck(columns=list(KNOWN_COLUMNS)), rows=[])

    header = check_header(parse_csv_line(lines[line_number]))

    rows: list[RawRow] = []
    for index in range(line_number + 1, len(lines)):
        text = lines[index]
        if text.strip() == "":
            continue
        values = parse_csv_line(text)
        fields: Optional[dict[str, str]] = None
        if len(values) == len(header.columns):
            fields = {name: value.strip() for name, value in zip(header.columns, values)}
        rows.append(RawRow(source_line=index + 1, raw_record=text, values=values, fields=fields))

    return MovementFile(header=header, rows=rows)


def best_effort_movement_id(row: RawRow, header: HeaderCheck) -> Optional[str]:
    """Return the row's movement_id if it can be found, even for a malformed row.

    For a malformed row we still try the movement_id column position, which helps tracing.
    """
    if row.fields is not None:
        return row.fields.get("movement_id") or None
    if "movement_id" in header.columns:
        position = header.columns.index("movement_id")
        if position < len(row.values):
            return row.values[position].strip() or None
    return None
