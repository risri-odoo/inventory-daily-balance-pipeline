"""Unit tests for row validation: one test (or more) per rule and reject code."""

import json
from typing import Optional

import pytest

from inventory_pipeline import validate as v

REFERENCE = v.ReferenceData(
    skus={"SKU-001", "SKU-002"},
    locations={"WH-A", "WH-B"},
    opening_dates={("SKU-001", "WH-A"): "2026-01-31", ("SKU-001", "WH-B"): "2026-02-05"},
)


def make_fields(**overrides: str) -> dict[str, str]:
    """A valid RECEIPT row; pass keyword arguments to change single fields."""
    fields = {
        "movement_id": "MV-1",
        "version": "1",
        "movement_type": "RECEIPT",
        "sku": "SKU-001",
        "location_id": "WH-A",
        "to_location_id": "",
        "quantity": "10",
        "business_date": "2026-02-10",
        "event_ts": "2026-02-10T09:00:00Z",
        "reason_code": "",
        "source_system": "ERP",
    }
    fields.update(overrides)
    return fields


def codes(result: v.ValidationResult) -> list[str]:
    return [error.code for error in result.errors]


def primary(**overrides: str) -> Optional[str]:
    return v.validate_row(make_fields(**overrides), REFERENCE).primary_reason


# --- happy path ---------------------------------------------------------------


def test_valid_receipt_is_normalised() -> None:
    result = v.validate_row(make_fields(movement_type="receipt", event_ts="2026-02-10 09:00:00"), REFERENCE)

    assert result.is_valid
    movement = result.movement
    assert movement is not None
    assert movement.movement_type == "RECEIPT"           # case-insensitive, stored upper case
    assert movement.version == 1
    assert movement.quantity == 10
    assert movement.event_ts == "2026-02-10T09:00:00Z"   # no timezone means UTC
    assert movement.to_location_id is None
    assert movement.reason_code is None                  # blank optional -> NULL
    assert len(movement.payload_hash) == 64


def test_blank_version_defaults_to_1() -> None:
    result = v.validate_row(make_fields(version=""), REFERENCE)
    assert result.movement is not None and result.movement.version == 1


def test_missing_version_column_defaults_to_1() -> None:
    fields = make_fields()
    del fields["version"]
    result = v.validate_row(fields, REFERENCE)
    assert result.movement is not None and result.movement.version == 1


def test_valid_transfer_keeps_destination() -> None:
    result = v.validate_row(
        make_fields(movement_type="TRANSFER", location_id="WH-A", to_location_id="WH-B", business_date="2026-02-10"),
        REFERENCE,
    )
    assert result.is_valid
    assert result.movement is not None and result.movement.to_location_id == "WH-B"


def test_event_ts_with_offset_is_converted_to_utc() -> None:
    result = v.validate_row(make_fields(event_ts="2026-02-10T09:30:00+05:30"), REFERENCE)
    assert result.movement is not None and result.movement.event_ts == "2026-02-10T04:00:00Z"


# --- MISSING_REQUIRED_FIELD (nulls) --------------------------------------------


@pytest.mark.parametrize("field_name", v.REQUIRED_FIELDS)
def test_each_blank_required_field_is_rejected(field_name: str) -> None:
    result = v.validate_row(make_fields(**{field_name: ""}), REFERENCE)
    assert result.primary_reason == v.MISSING_REQUIRED_FIELD
    assert field_name in result.errors[0].message


def test_whitespace_only_counts_as_blank() -> None:
    assert primary(sku="   ") == v.MISSING_REQUIRED_FIELD


# --- INVALID_MOVEMENT_TYPE -----------------------------------------------------


def test_unknown_movement_type() -> None:
    assert primary(movement_type="RETURN") == v.INVALID_MOVEMENT_TYPE


@pytest.mark.parametrize("text", ["receipt", "Issue", "ADJUSTMENT"])
def test_movement_type_is_case_insensitive(text: str) -> None:
    assert primary(movement_type=text) is None


# --- INVALID_VERSION -----------------------------------------------------------


@pytest.mark.parametrize("text", ["0", "-1", "1.5", "abc", "v2"])
def test_invalid_version(text: str) -> None:
    assert primary(version=text) == v.INVALID_VERSION


# --- INVALID_QUANTITY (boundaries and sign rules) -------------------------------


def test_quantity_at_max_passes() -> None:
    assert primary(quantity="1000000") is None


def test_quantity_above_max_fails() -> None:
    assert primary(quantity="1000001") == v.INVALID_QUANTITY


def test_negative_adjustment_at_max_passes() -> None:
    assert primary(movement_type="ADJUSTMENT", quantity="-1000000") is None


def test_negative_adjustment_above_max_fails() -> None:
    assert primary(movement_type="ADJUSTMENT", quantity="-1000001") == v.INVALID_QUANTITY


@pytest.mark.parametrize("text", ["1.5", "abc", "1e3", "1,000", "10 units"])
def test_non_integer_quantity_fails(text: str) -> None:
    assert primary(quantity=text) == v.INVALID_QUANTITY


@pytest.mark.parametrize("movement_type", ["RECEIPT", "ISSUE"])
def test_negative_quantity_fails_for_receipt_and_issue(movement_type: str) -> None:
    assert primary(movement_type=movement_type, quantity="-5") == v.INVALID_QUANTITY


def test_negative_quantity_fails_for_transfer() -> None:
    assert primary(movement_type="TRANSFER", to_location_id="WH-B", quantity="-5") == v.INVALID_QUANTITY


def test_negative_adjustment_passes() -> None:
    result = v.validate_row(make_fields(movement_type="ADJUSTMENT", quantity="-3"), REFERENCE)
    assert result.movement is not None and result.movement.quantity == -3


@pytest.mark.parametrize("movement_type", ["RECEIPT", "ISSUE", "ADJUSTMENT"])
def test_zero_quantity_at_version_1_fails(movement_type: str) -> None:
    assert primary(movement_type=movement_type, quantity="0", version="1") == v.INVALID_QUANTITY


@pytest.mark.parametrize("movement_type", ["RECEIPT", "ISSUE", "ADJUSTMENT"])
def test_zero_quantity_at_version_2_is_a_void_and_passes(movement_type: str) -> None:
    assert primary(movement_type=movement_type, quantity="0", version="2") is None


def test_zero_quantity_blank_version_fails() -> None:
    assert primary(quantity="0", version="") == v.INVALID_QUANTITY


# --- INVALID_DATE --------------------------------------------------------------


@pytest.mark.parametrize("text", ["2026-02-30", "2026-13-01", "10/02/2026", "2026-2-1", "20260210"])
def test_invalid_business_date(text: str) -> None:
    assert primary(business_date=text) == v.INVALID_DATE


def test_leap_day_is_valid_in_leap_year() -> None:
    assert primary(business_date="2028-02-29", event_ts="2028-02-29T10:00:00Z") is None


# --- INVALID_TIMESTAMP ---------------------------------------------------------


@pytest.mark.parametrize("text", ["yesterday", "2026-02-10", "2026-02-10T25:00:00Z", "10:00:00"])
def test_invalid_event_ts(text: str) -> None:
    assert primary(event_ts=text) == v.INVALID_TIMESTAMP


# --- UNKNOWN_SKU / UNKNOWN_LOCATION ---------------------------------------------


def test_unknown_sku() -> None:
    assert primary(sku="SKU-999") == v.UNKNOWN_SKU


def test_unknown_location() -> None:
    assert primary(location_id="WH-Z") == v.UNKNOWN_LOCATION


def test_unknown_transfer_destination() -> None:
    assert primary(movement_type="TRANSFER", to_location_id="WH-Z") == v.UNKNOWN_LOCATION


# --- INVALID_TRANSFER ----------------------------------------------------------


def test_transfer_without_destination() -> None:
    assert primary(movement_type="TRANSFER", to_location_id="") == v.INVALID_TRANSFER


def test_transfer_to_same_location() -> None:
    assert primary(movement_type="TRANSFER", location_id="WH-A", to_location_id="WH-A") == v.INVALID_TRANSFER


def test_destination_on_non_transfer() -> None:
    assert primary(movement_type="RECEIPT", to_location_id="WH-B") == v.INVALID_TRANSFER


# --- FUTURE_DATED --------------------------------------------------------------


def test_business_date_after_event_date_is_future_dated() -> None:
    assert primary(business_date="2026-02-11", event_ts="2026-02-10T23:59:59Z") == v.FUTURE_DATED


def test_business_date_equal_to_event_date_passes() -> None:
    assert primary(business_date="2026-02-10", event_ts="2026-02-10T00:00:00Z") is None


def test_future_dated_uses_utc_date_of_event() -> None:
    # 2026-02-11 01:00 at +05:00 is 2026-02-10 20:00 UTC, so business_date 2026-02-11 is in the future.
    assert primary(business_date="2026-02-11", event_ts="2026-02-11T01:00:00+05:00") == v.FUTURE_DATED


# --- BEFORE_OPENING_SNAPSHOT -----------------------------------------------------


def test_movement_on_opening_date_is_rejected() -> None:
    assert primary(business_date="2026-01-31", event_ts="2026-02-01T08:00:00Z") == v.BEFORE_OPENING_SNAPSHOT


def test_movement_day_after_opening_date_passes() -> None:
    assert primary(business_date="2026-02-01", event_ts="2026-02-01T08:00:00Z") is None


def test_pair_without_opening_balance_has_no_closed_period() -> None:
    assert primary(sku="SKU-002", business_date="2020-01-01", event_ts="2020-01-01T08:00:00Z") is None


def test_transfer_destination_leg_in_closed_period_is_rejected() -> None:
    # WH-A closed up to 2026-01-31 (open on 02-03), WH-B closed up to 2026-02-05 (closed on 02-03).
    result = v.validate_row(
        make_fields(movement_type="TRANSFER", to_location_id="WH-B",
                    business_date="2026-02-03", event_ts="2026-02-03T08:00:00Z"),
        REFERENCE,
    )
    assert result.primary_reason == v.BEFORE_OPENING_SNAPSHOT
    assert "WH-B" in result.errors[0].message


# --- multiple failures, details and helpers ---------------------------------------


def test_all_failures_are_collected_and_first_is_primary() -> None:
    result = v.validate_row(make_fields(movement_type="BOGUS", quantity="1.5", sku="SKU-999"), REFERENCE)

    assert codes(result) == [v.INVALID_MOVEMENT_TYPE, v.INVALID_QUANTITY, v.UNKNOWN_SKU]
    assert result.primary_reason == v.INVALID_MOVEMENT_TYPE
    details = json.loads(result.details_json())
    assert [d["code"] for d in details] == codes(result)


def test_malformed_row_result() -> None:
    result = v.malformed_row_result(expected_fields=11, actual_fields=12)
    assert result.primary_reason == v.MALFORMED_ROW
    assert "expected 11 fields, got 12" in result.errors[0].message


def test_payload_hash_ignores_formatting_but_not_content() -> None:
    a = v.validate_row(make_fields(movement_type="receipt", event_ts="2026-02-10T09:00:00Z"), REFERENCE).movement
    b = v.validate_row(make_fields(movement_type="RECEIPT", event_ts="2026-02-10 09:00:00"), REFERENCE).movement
    c = v.validate_row(make_fields(quantity="11"), REFERENCE).movement
    assert a is not None and b is not None and c is not None
    assert a.payload_hash == b.payload_hash     # same meaning, same hash
    assert a.payload_hash != c.payload_hash     # different quantity, different hash


def test_reject_code_list_matches_spec() -> None:
    assert v.REJECT_CODES == [
        "MALFORMED_ROW", "MISSING_REQUIRED_FIELD", "INVALID_MOVEMENT_TYPE", "INVALID_VERSION",
        "INVALID_QUANTITY", "INVALID_DATE", "INVALID_TIMESTAMP", "UNKNOWN_SKU", "UNKNOWN_LOCATION",
        "INVALID_TRANSFER", "FUTURE_DATED", "BEFORE_OPENING_SNAPSHOT", "DUPLICATE_EVENT",
        "CONFLICTING_DUPLICATE",
    ]
