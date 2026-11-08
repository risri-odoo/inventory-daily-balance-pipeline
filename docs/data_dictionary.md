# Data dictionary

Conventions:
- **Names:** `snake_case` tables and columns. `dim_` = reference dimension, `v_` = view.
- **Dates and times:** dates are `TEXT 'YYYY-MM-DD'`. Timestamps are `TEXT` UTC `'YYYY-MM-DDTHH:MM:SSZ'`.
- **Lineage:** every loaded or rejected row carries `run_id` and `source_file`. `source_file` is relative
  to the input root, so no machine paths are stored.
- **IDs:** `run_id` = `run-<UTC yyyymmddThhmmssZ>-<8 random hex>`. Business keys
  (`movement_id`, `sku`, `location_id`) come from the source and are kept as given.

Schema source: [`inventory_pipeline/schema.py`](../inventory_pipeline/schema.py).

## Source files

### Reference (`reference/`)

| File | Column | Type | Null | Rule |
|------|--------|------|------|------|
| `dim_sku.csv` | `sku` | text | no | primary key, unique |
| | `description`, `uom` | text | yes | |
| `dim_location.csv` | `location_id` | text | no | primary key, unique |
| | `location_name`, `location_type` | text | yes | |
| `opening_balance.csv` | `sku` | text | no | must be in `dim_sku` |
| | `location_id` | text | no | must be in `dim_location`; PK = (`sku`, `location_id`) |
| | `as_of_date` | date | no | on-hand is at the **end** of this date; it and earlier dates are closed |
| | `quantity` | integer | no | whole number |

Any problem in a reference file fails the whole reference load, which is then rolled back.

### Movements (`input/*.csv`), matched by header name

| Column | Required | Type | Rule (reject code if broken) |
|--------|----------|------|------------------------------|
| `movement_id` | yes | text | natural key (`MISSING_REQUIRED_FIELD`) |
| `version` | no, default 1 | integer ≥ 1 | higher = correction (`INVALID_VERSION`) |
| `movement_type` | yes | text | RECEIPT / ISSUE / TRANSFER / ADJUSTMENT, any case (`INVALID_MOVEMENT_TYPE`) |
| `sku` | yes | text | in `dim_sku` (`UNKNOWN_SKU`) |
| `location_id` | yes | text | in `dim_location` (`UNKNOWN_LOCATION`) |
| `to_location_id` | TRANSFER only | text | in `dim_location`, ≠ `location_id`, blank otherwise (`UNKNOWN_LOCATION`, `INVALID_TRANSFER`) |
| `quantity` | yes | integer | \|q\| ≤ 1,000,000. RECEIPT/ISSUE/TRANSFER > 0, ADJUSTMENT ≠ 0, 0 allowed only for version > 1 (`INVALID_QUANTITY`) |
| `business_date` | yes | date | real date (`INVALID_DATE`); after the pair's `as_of_date` (`BEFORE_OPENING_SNAPSHOT`); not after the local date of `event_ts` (`FUTURE_DATED`) |
| `event_ts` | yes | timestamp | ISO-8601 date and time; no timezone = UTC (`INVALID_TIMESTAMP`) |
| `reason_code`, `source_system` | no | text | passed through |

File-level rules: a missing required column or a duplicate column name means `REJECTED`. Extra columns
are ignored, with a schema warning. A header-only or zero-byte file is an empty `SUCCESS`. A wrong field
count is `MALFORMED_ROW`. Duplicate keys give `DUPLICATE_EVENT` (identical) or `CONFLICTING_DUPLICATE`
(different payload).

## Tables

### `dim_sku`, `dim_location`

The reference columns above, plus `loaded_at` (UTC). Upserted: a row only changes when a value differs.

### `opening_balance`

| Column | Type | Key | Notes |
|--------|------|-----|-------|
| `sku`, `location_id` | text | PK | |
| `as_of_date` | date | | closed period up to and including this date |
| `quantity` | integer | | on hand at the end of `as_of_date` |
| `loaded_at` | timestamp | | |

Any insert or change here triggers a full balance rebuild.

### `movement_event`: append-only history

| Column | Type | Key / null | Notes |
|--------|------|------------|-------|
| `movement_id` | text | PK part | |
| `version` | integer | PK part | |
| `movement_type` | text | not null | upper case |
| `sku`, `location_id` | text | not null | |
| `to_location_id` | text | null unless TRANSFER | |
| `quantity` | integer | not null | as received; signs are applied in `v_movement_leg` |
| `business_date` | date | not null | drives the balance |
| `event_ts` | timestamp | not null | normalised to UTC |
| `reason_code`, `source_system` | text | null | |
| `payload_hash` | text | not null | SHA-256 of the normalised business fields; tells exact from conflicting duplicates |
| `is_late` | 0/1 | not null | `business_date` < max `business_date` loaded before this file |
| `run_id`, `source_file`, `source_line` | | not null | lineage |
| `ingested_at` | timestamp | not null | the run's start time |

Indexes: `run_id`, `business_date`, (`sku`, `location_id`), (`sku`, `to_location_id`).
Rows are never updated or deleted.

### `v_movement_current` (view)

The current state of each movement: the row with no higher `version` for the same `movement_id`.
Arrival order doesn't matter: if v3 arrives before v2, v3 stays current.

### `v_movement_leg` (view)

Signed ledger legs from current versions only.

| leg_type | location | qty_delta |
|----------|----------|-----------|
| RECEIPT | `location_id` | +quantity |
| ISSUE | `location_id` | −quantity |
| ADJUSTMENT | `location_id` | quantity as given (signed) |
| TRANSFER_OUT | `location_id` | −quantity |
| TRANSFER_IN | `to_location_id` | +quantity |

A void (quantity 0) gives a leg with `qty_delta` 0.

### `quarantine_movement`

| Column | Type | Notes |
|--------|------|-------|
| `quarantine_id` | integer PK | autoincrement |
| `run_id`, `source_file`, `source_line` | | where the row came from (line 1 = header) |
| `movement_id` | text, null | best effort, even for malformed rows |
| `reject_reason` | text | primary (first) failure |
| `reject_details` | JSON text | every failure: `[{"code": ..., "message": ...}]`; duplicates name the run, file and line of the first arrival |
| `raw_record` | text | the original CSV line, unchanged |
| `quarantined_at` | timestamp | |

`DUPLICATE_EVENT` rows are stored here as well, but are counted as duplicates rather than rejects.

### `daily_inventory_balance`: the output

Grain: one row per (`sku`, `location_id`, `balance_date`), **dense**. A pair starts on `as_of_date + 1`
(if it has an opening balance) or on its first movement date. Every pair continues to the global horizon
(max `business_date` loaded), and days without movements carry the balance forward.

| Column | Type | Meaning |
|--------|------|---------|
| `sku`, `location_id`, `balance_date` | PK | |
| `opening_qty` | integer | previous day's closing; on the first day, the opening balance or 0 |
| `receipt_qty` | integer | sum of receipts (positive) |
| `issue_qty` | integer | sum of issues, **as a positive amount** |
| `transfer_in_qty` | integer | positive |
| `transfer_out_qty` | integer | **as a positive amount** |
| `adjustment_qty` | integer | signed |
| `net_change_qty` | integer | `receipt − issue + transfer_in − transfer_out + adjustment` |
| `closing_qty` | integer | `opening_qty + net_change_qty`; can be negative (a data-quality WARN) |
| `run_id` | text | the run that last wrote this row |

### `pipeline_run`: one row per run (file or reference load)

| Column | Notes |
|--------|-------|
| `run_id` (PK), `source_file`, `file_sha256` | `source_file = 'reference'` for reference loads |
| `status` | RUNNING → SUCCESS / SKIPPED / REJECTED / FAILED |
| `started_at`, `ended_at`, `duration_ms` | |
| `rows_read`, `rows_accepted`, `rows_rejected`, `rows_duplicate` | `read = accepted + rejected + duplicate` |
| `rows_late`, `rows_superseding` | accepted rows that were late / became the new current version |
| `min_affected_date`, `pairs_rebuilt`, `balance_rows_written` | what the rebuild touched |
| `negative_balance_rows` | negative closing rows in the whole table after the run |
| `schema_warnings` | JSON list (e.g. extra columns) |
| `stage_timings_ms` | JSON `{"read", "validate_load", "rebuild"}` |
| `error_message` | why a run was REJECTED / FAILED / SKIPPED |

A FAILED run keeps zero counts, because everything it did was rolled back.

### `processed_file`

`file_sha256` (PK), `source_file`, `run_id`, `processed_at`. Written only by a successful run, in the
same transaction. Used to skip a byte-identical file on re-run.

## Derived fields

| Field | Derived from |
|-------|--------------|
| `payload_hash` | normalised movement_type, sku, location_id, to_location_id, quantity, business_date, event_ts (UTC), reason_code, source_system |
| `is_late` | `business_date` vs. max `business_date` in `movement_event` before the file |
| `qty_delta`, `leg_type` | `movement_type` + `quantity` (see `v_movement_leg`) |
| all balance columns | `v_movement_leg` + `opening_balance` |
