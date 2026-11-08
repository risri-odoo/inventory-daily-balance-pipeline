"""Row validation: turn one raw row into a clean movement, or a list of reject reasons.

`validate_row` is a pure function (no database access). It checks every rule and collects
every failure, in the fixed order of REJECT_CODES; the first failure is the primary reason.

Duplicate checks (DUPLICATE_EVENT / CONFLICTING_DUPLICATE) need the database and the earlier
rows of the same file, so they are done by the loader, using the payload_hash computed here.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

# Reject codes, in the order the checks run.
MALFORMED_ROW = "MALFORMED_ROW"
MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
INVALID_MOVEMENT_TYPE = "INVALID_MOVEMENT_TYPE"
INVALID_VERSION = "INVALID_VERSION"
INVALID_QUANTITY = "INVALID_QUANTITY"
INVALID_DATE = "INVALID_DATE"
INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
UNKNOWN_SKU = "UNKNOWN_SKU"
UNKNOWN_LOCATION = "UNKNOWN_LOCATION"
INVALID_TRANSFER = "INVALID_TRANSFER"
FUTURE_DATED = "FUTURE_DATED"
BEFORE_OPENING_SNAPSHOT = "BEFORE_OPENING_SNAPSHOT"
DUPLICATE_EVENT = "DUPLICATE_EVENT"
CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE"

REJECT_CODES = [
    MALFORMED_ROW,
    MISSING_REQUIRED_FIELD,
    INVALID_MOVEMENT_TYPE,
    INVALID_VERSION,
    INVALID_QUANTITY,
    INVALID_DATE,
    INVALID_TIMESTAMP,
    UNKNOWN_SKU,
    UNKNOWN_LOCATION,
    INVALID_TRANSFER,
    FUTURE_DATED,
    BEFORE_OPENING_SNAPSHOT,
    DUPLICATE_EVENT,
    CONFLICTING_DUPLICATE,
]

MOVEMENT_TYPES = ["RECEIPT", "ISSUE", "TRANSFER", "ADJUSTMENT"]
MAX_ABS_QUANTITY = 1_000_000

REQUIRED_FIELDS = ["movement_id", "movement_type", "sku", "location_id", "quantity", "business_date", "event_ts"]

INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
POSITIVE_INTEGER_PATTERN = re.compile(r"^\d+$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A timestamp must have a date AND a time; the timezone part is optional.
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


@dataclass
class ReferenceData:
    """The reference data a row is checked against."""

    skus: set[str]
    locations: set[str]
    opening_dates: dict[tuple[str, str], str]   # (sku, location_id) -> as_of_date 'YYYY-MM-DD'


@dataclass
class Movement:
    """A movement row that passed validation, with normalised values."""

    movement_id: str
    version: int
    movement_type: str              # upper case
    sku: str
    location_id: str
    to_location_id: Optional[str]   # only for TRANSFER
    quantity: int
    business_date: str              # 'YYYY-MM-DD'
    event_ts: str                   # UTC, 'YYYY-MM-DDTHH:MM:SSZ'
    reason_code: Optional[str]
    source_system: Optional[str]
    payload_hash: str


@dataclass
class RejectError:
    """One rule failure."""

    code: str
    message: str


@dataclass
class ValidationResult:
    """Outcome of validating one row: either a movement, or at least one error."""

    movement: Optional[Movement] = None
    errors: list[RejectError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def primary_reason(self) -> Optional[str]:
        """The first failure is the primary reject reason."""
        return self.errors[0].code if self.errors else None

    def details_json(self) -> str:
        """Every failure as a JSON list, stored in quarantine_movement.reject_details."""
        return json.dumps([{"code": e.code, "message": e.message} for e in self.errors])


# ---------------------------------------------------------------------------
# Small parsers. Each returns None when the text is not valid.
# ---------------------------------------------------------------------------


def parse_version(text: str) -> Optional[int]:
    """Blank means version 1. Otherwise it must be a whole number >= 1."""
    if text == "":
        return 1
    if not POSITIVE_INTEGER_PATTERN.match(text):
        return None
    value = int(text)
    return value if value >= 1 else None


def parse_quantity(text: str) -> Optional[int]:
    """A whole number, optionally signed. '1.5', '1e3' and '1,000' are not accepted."""
    if not INTEGER_PATTERN.match(text):
        return None
    return int(text)


def parse_business_date(text: str) -> Optional[str]:
    """A real calendar date in 'YYYY-MM-DD' form (so 2026-02-30 is rejected)."""
    if not DATE_PATTERN.match(text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def parse_event_ts(text: str) -> Optional[datetime]:
    """An ISO-8601 date-time. 'Z' or an offset is honoured; no timezone means UTC.

    Returns an aware datetime that KEEPS the offset written in the source, so its .date()
    is the local calendar date (used by the future-dated rule). Use format_utc() to store it.
    """
    if not TIMESTAMP_PATTERN.match(text):
        return None
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_utc(value: datetime) -> str:
    """Convert an aware datetime to UTC and format it as 'YYYY-MM-DDTHH:MM:SSZ'.

    Fractional seconds are kept only if present.
    """
    value = value.astimezone(timezone.utc)
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_payload_hash(movement_values: list[object]) -> str:
    """SHA-256 of the normalised business values, used to tell exact from conflicting duplicates."""
    canonical = json.dumps(movement_values, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The quantity sign rules
# ---------------------------------------------------------------------------


def quantity_error(movement_type: Optional[str], quantity: int, version: int) -> Optional[str]:
    """Return an error message if the quantity breaks the range or sign rules, else None.

    - abs(quantity) <= 1,000,000 for every type
    - RECEIPT / ISSUE / TRANSFER need quantity > 0; ADJUSTMENT needs quantity != 0
    - exception: quantity 0 is allowed when version > 1 (a void / cancel)
    """
    if abs(quantity) > MAX_ABS_QUANTITY:
        return f"abs(quantity) {abs(quantity)} exceeds {MAX_ABS_QUANTITY}"
    if quantity == 0:
        if version > 1:
            return None
        return "quantity 0 is only allowed as a void (version > 1)"
    if movement_type in ("RECEIPT", "ISSUE", "TRANSFER") and quantity < 0:
        return f"{movement_type} quantity must be positive, got {quantity}"
    return None


# ---------------------------------------------------------------------------
# The main check
# ---------------------------------------------------------------------------


def validate_row(fields: dict[str, str], reference: ReferenceData) -> ValidationResult:
    """Validate one row's fields (column -> trimmed text) against every rule.

    Checks run in the order of REJECT_CODES and all failures are collected.
    A check is skipped when the value it depends on is already known to be bad,
    so one broken value does not cause a cascade of confusing errors.
    """
    errors: list[RejectError] = []

    def get(name: str) -> str:
        return (fields.get(name) or "").strip()

    # MISSING_REQUIRED_FIELD
    missing = [name for name in REQUIRED_FIELDS if get(name) == ""]
    if missing:
        errors.append(RejectError(MISSING_REQUIRED_FIELD, "blank required fields: " + ", ".join(missing)))

    # INVALID_MOVEMENT_TYPE
    movement_type: Optional[str] = None
    if get("movement_type") != "":
        movement_type = get("movement_type").upper()
        if movement_type not in MOVEMENT_TYPES:
            errors.append(RejectError(INVALID_MOVEMENT_TYPE, f"unknown movement_type '{get('movement_type')}'"))
            movement_type = None

    # INVALID_VERSION
    version = parse_version(get("version"))
    if version is None:
        errors.append(RejectError(INVALID_VERSION, f"version must be a whole number >= 1, got '{get('version')}'"))

    # INVALID_QUANTITY
    quantity: Optional[int] = None
    if get("quantity") != "":
        quantity = parse_quantity(get("quantity"))
        if quantity is None:
            errors.append(RejectError(INVALID_QUANTITY, f"quantity must be a whole number, got '{get('quantity')}'"))
        else:
            # If the version is invalid, judge the quantity as if it were version 1.
            problem = quantity_error(movement_type, quantity, version or 1)
            if problem:
                errors.append(RejectError(INVALID_QUANTITY, problem))

    # INVALID_DATE
    business_date: Optional[str] = None
    if get("business_date") != "":
        business_date = parse_business_date(get("business_date"))
        if business_date is None:
            errors.append(RejectError(INVALID_DATE, f"business_date must be a real YYYY-MM-DD date, got '{get('business_date')}'"))

    # INVALID_TIMESTAMP
    event_ts: Optional[datetime] = None
    if get("event_ts") != "":
        event_ts = parse_event_ts(get("event_ts"))
        if event_ts is None:
            errors.append(RejectError(INVALID_TIMESTAMP, f"event_ts must be an ISO-8601 date-time, got '{get('event_ts')}'"))

    # UNKNOWN_SKU
    sku = get("sku")
    if sku != "" and sku not in reference.skus:
        errors.append(RejectError(UNKNOWN_SKU, f"sku '{sku}' not in dim_sku"))

    # UNKNOWN_LOCATION (both the source location and, if given, the destination)
    location_id = get("location_id")
    to_location_id = get("to_location_id")
    unknown = [loc for loc in (location_id, to_location_id) if loc != "" and loc not in reference.locations]
    if unknown:
        errors.append(RejectError(UNKNOWN_LOCATION, "location not in dim_location: " + ", ".join(unknown)))

    # INVALID_TRANSFER
    if movement_type == "TRANSFER":
        if to_location_id == "":
            errors.append(RejectError(INVALID_TRANSFER, "TRANSFER needs a to_location_id"))
        elif to_location_id == location_id:
            errors.append(RejectError(INVALID_TRANSFER, f"TRANSFER to the same location '{location_id}'"))
    elif movement_type is not None and to_location_id != "":
        errors.append(RejectError(INVALID_TRANSFER, f"to_location_id must be blank for {movement_type}"))

    # FUTURE_DATED: the business date cannot be after the day the source recorded the event.
    # We compare with the calendar date as written in event_ts (its local date, before any
    # conversion to UTC): 2026-02-04T01:00:00+05:30 is 4 February where it was recorded.
    if business_date is not None and event_ts is not None:
        event_local_date = event_ts.date().isoformat()
        if business_date > event_local_date:
            errors.append(RejectError(FUTURE_DATED, f"business_date {business_date} is after event_ts local date {event_local_date}"))

    # BEFORE_OPENING_SNAPSHOT: the period up to as_of_date is closed, for both legs of a transfer.
    if business_date is not None and sku != "":
        legs = [location_id]
        if movement_type == "TRANSFER" and to_location_id != "":
            legs.append(to_location_id)
        closed = []
        for loc in legs:
            as_of_date = reference.opening_dates.get((sku, loc))
            if as_of_date is not None and business_date <= as_of_date:
                closed.append(f"{sku}@{loc} (as_of_date {as_of_date})")
        if closed:
            errors.append(RejectError(BEFORE_OPENING_SNAPSHOT, f"business_date {business_date} is in a closed period for " + ", ".join(closed)))

    if errors:
        return ValidationResult(errors=errors)

    # Every value is known to be good here; the asserts document that for type checkers.
    assert movement_type is not None and version is not None and quantity is not None
    assert business_date is not None and event_ts is not None

    to_location = to_location_id if movement_type == "TRANSFER" else None
    reason_code = get("reason_code") or None
    source_system = get("source_system") or None
    event_ts_text = format_utc(event_ts)
    payload_hash = compute_payload_hash([
        movement_type, sku, location_id, to_location, quantity,
        business_date, event_ts_text, reason_code, source_system,
    ])
    movement = Movement(
        movement_id=get("movement_id"),
        version=version,
        movement_type=movement_type,
        sku=sku,
        location_id=location_id,
        to_location_id=to_location,
        quantity=quantity,
        business_date=business_date,
        event_ts=event_ts_text,
        reason_code=reason_code,
        source_system=source_system,
        payload_hash=payload_hash,
    )
    return ValidationResult(movement=movement)


def malformed_row_result(expected_fields: int, actual_fields: int) -> ValidationResult:
    """The result for a row whose field count does not match the header."""
    return ValidationResult(errors=[RejectError(
        MALFORMED_ROW, f"expected {expected_fields} fields, got {actual_fields}"
    )])
