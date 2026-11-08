"""Unit tests for file reading and the header contract."""

from pathlib import Path

from inventory_pipeline import ingest

HEADER = "movement_id,version,movement_type,sku,location_id,to_location_id,quantity,business_date,event_ts,reason_code,source_system"
ROW = "MV-1,1,RECEIPT,SKU-001,WH-A,,10,2026-02-10,2026-02-10T09:00:00Z,,ERP"


def write(tmp_path: Path, text: str, name: str = "batch.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


# --- header contract -----------------------------------------------------------


def test_full_header_is_valid_without_warnings() -> None:
    check = ingest.check_header(HEADER.split(","))
    assert check.is_valid
    assert check.warnings() == []


def test_optional_columns_may_be_absent() -> None:
    check = ingest.check_header(ingest.REQUIRED_COLUMNS)
    assert check.is_valid


def test_missing_required_column_rejects_file() -> None:
    columns = [c for c in HEADER.split(",") if c != "quantity"]
    check = ingest.check_header(columns)
    assert not check.is_valid
    assert check.missing_columns == ["quantity"]
    assert "quantity" in check.rejection_message()


def test_duplicate_column_rejects_file() -> None:
    check = ingest.check_header(HEADER.split(",") + ["SKU "])   # same name after trim + lower case
    assert not check.is_valid
    assert check.duplicate_columns == ["sku"]


def test_extra_column_is_a_warning_only() -> None:
    check = ingest.check_header(HEADER.split(",") + ["batch_note"])
    assert check.is_valid
    assert check.extra_columns == ["batch_note"]
    assert check.warnings() == ["extra columns ignored: batch_note"]


def test_header_names_are_trimmed_and_case_insensitive() -> None:
    check = ingest.check_header([" Movement_ID ", "MOVEMENT_TYPE", "sku", "location_id", "quantity", "business_date", "event_ts"])
    assert check.is_valid


# --- reading files -------------------------------------------------------------


def test_reads_rows_with_line_numbers_and_raw_text(tmp_path: Path) -> None:
    path = write(tmp_path, f"{HEADER}\n{ROW}\n\nMV-2,1,ISSUE,SKU-001,WH-A,,3,2026-02-10,2026-02-10T10:00:00Z,,ERP\n")

    result = ingest.read_movement_file(path)

    assert [r.source_line for r in result.rows] == [2, 4]   # blank line 3 is skipped
    assert result.rows[0].raw_record == ROW
    assert result.rows[0].fields is not None
    assert result.rows[0].fields["quantity"] == "10"


def test_columns_are_matched_by_name_not_position(tmp_path: Path) -> None:
    path = write(tmp_path, "sku,quantity,movement_id,movement_type,location_id,business_date,event_ts\n"
                           "SKU-001,7,MV-9,RECEIPT,WH-A,2026-02-10,2026-02-10T09:00:00Z\n")
    row = ingest.read_movement_file(path).rows[0]
    assert row.fields is not None
    assert row.fields["movement_id"] == "MV-9"
    assert row.fields["quantity"] == "7"


def test_wrong_field_count_is_malformed(tmp_path: Path) -> None:
    path = write(tmp_path, f"{HEADER}\n{ROW},EXTRA\n")
    row = ingest.read_movement_file(path).rows[0]
    assert row.is_malformed
    header = ingest.read_movement_file(path).header
    assert ingest.best_effort_movement_id(row, header) == "MV-1"


def test_quoted_comma_is_one_field(tmp_path: Path) -> None:
    path = write(tmp_path, f"{HEADER}\nMV-1,1,RECEIPT,SKU-001,WH-A,,10,2026-02-10,2026-02-10T09:00:00Z,\"damaged, recount\",ERP\n")
    row = ingest.read_movement_file(path).rows[0]
    assert row.fields is not None
    assert row.fields["reason_code"] == "damaged, recount"


def test_header_only_file_is_empty_batch(tmp_path: Path) -> None:
    result = ingest.read_movement_file(write(tmp_path, HEADER + "\n"))
    assert result.header.is_valid
    assert result.rows == []


def test_zero_byte_file_is_empty_batch(tmp_path: Path) -> None:
    result = ingest.read_movement_file(write(tmp_path, ""))
    assert result.header.is_valid
    assert result.rows == []


def test_utf8_bom_and_crlf_are_handled(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_bytes(("﻿" + HEADER + "\r\n" + ROW + "\r\n").encode("utf-8"))
    result = ingest.read_movement_file(path)
    assert result.header.is_valid
    assert result.rows[0].raw_record == ROW


# --- hashing and paths ---------------------------------------------------------


def test_file_sha256_is_stable_and_content_based(tmp_path: Path) -> None:
    a = write(tmp_path, "abc", "a.csv")
    b = write(tmp_path, "abc", "b.csv")
    c = write(tmp_path, "abd", "c.csv")
    assert ingest.file_sha256(a) == ingest.file_sha256(b)
    assert ingest.file_sha256(a) != ingest.file_sha256(c)
    assert ingest.file_sha256(a) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_source_file_is_relative_to_input_root(tmp_path: Path) -> None:
    nested = tmp_path / "input" / "2026"
    nested.mkdir(parents=True)
    path = write(nested, "x", "batch_01.csv")
    assert ingest.relative_source_file(path, tmp_path / "input") == "2026/batch_01.csv"


def test_source_file_outside_root_falls_back_to_name(tmp_path: Path) -> None:
    path = write(tmp_path, "x", "batch_01.csv")
    assert ingest.relative_source_file(path, tmp_path / "elsewhere") == "batch_01.csv"
