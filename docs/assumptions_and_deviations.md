# Assumptions and deviations

Every assumption made where the spec was silent, and every deviation from the spec, is recorded here.

## Assumptions

| # | Area | Assumption | Why |
|---|------|------------|-----|
| A1 | Balance components | In `daily_inventory_balance`, `issue_qty` and `transfer_out_qty` are stored as positive magnitudes; `adjustment_qty` is signed. So `net_change_qty = receipt_qty - issue_qty + transfer_in_qty - transfer_out_qty + adjustment_qty`. | Positive magnitudes read naturally in reports ("issued 5"); adjustments can go either way so they keep their sign. |
| A2 | Default paths | `INV_DB_PATH` defaults to `output/inventory.db` and `INV_OUTPUT_DIR` to `output`, both relative to the current directory. | Keeps all generated artefacts in one git-ignored folder. |
| A3 | Foreign keys | The tables do not declare SQLite foreign keys. Validation rejects unknown SKUs and locations, and a reconciliation check confirms there are no orphans. | Keeps loads simple and makes the orphan check an explicit, visible data-quality control. |
| A4 | File format | Files are UTF-8 (a BOM is ignored), comma-separated, with **one record per physical line**. Quoted values may contain commas but not line breaks. | Lets every row keep its exact `source_line` and original `raw_record` for tracing. Movement feeds rarely contain multi-line values. |
| A5 | Blank lines | Fully blank lines are skipped and not counted in `rows_read`. | Trailing blank lines are common in exported CSVs and carry no data. |
| A6 | Header matching | Header names are matched after trimming spaces and lower-casing (`" SKU "` matches `sku`). A name that repeats after this normalisation counts as a duplicate column. | "Matched by header name" should not depend on cosmetic differences. |
| A7 | Zero-byte file | A zero-byte (or whitespace-only) file has no header; it is treated as a successful empty batch, not as a file with missing columns. | The spec says a zero-byte file is a successful empty batch. |
| A8 | Unknown destination | An unknown `to_location_id` is rejected as `UNKNOWN_LOCATION` (an invalid reference key). A TRANSFER with a blank `to_location_id`, a destination equal to the source, or a destination on a non-transfer row, is `INVALID_TRANSFER`. | Keeps one code per kind of problem: reference errors vs. transfer-shape errors. A blank destination is not `MISSING_REQUIRED_FIELD` because the column is only required for transfers. |
| A9 | Timestamps | `event_ts` must contain a date **and** a time (`YYYY-MM-DDTHH:MM[:SS[.fff]]`, `T` or space). A date alone is `INVALID_TIMESTAMP`. Offsets are converted to UTC; no timezone means UTC. Stored as `YYYY-MM-DDTHH:MM:SSZ`, keeping fractional seconds only if present. | A timestamp without a time is ambiguous. Keeping fractions avoids two different source timestamps collapsing to the same value. |
| A10 | Future-dated check | Compares `business_date` with the **local calendar date as written in `event_ts`** (before converting to UTC). Example: `2026-02-04T01:00:00+05:30` with `business_date` 2026-02-04 is valid, even though the UTC date is 2026-02-03. With no timezone the local date is the UTC date. `event_ts` is still stored in UTC. | The business date is set where the event happens, so the source's own calendar day is the fair comparison. Comparing with the UTC date wrongly rejected same-day events from timezones ahead of UTC. (Changed after Phase 2 at the candidate's request, see the prompt log; the first version used the UTC date.) |
| A11 | Check cascade | A check is skipped when the value it depends on already failed (e.g. a non-numeric quantity is not also range-checked; an unknown movement type skips the sign and transfer checks). If `version` is invalid, the quantity sign rule is applied as if it were version 1. | All real failures are still recorded, without confusing follow-on errors. |
| A12 | Payload hash | `payload_hash` = SHA-256 of the normalised business values: movement_type, sku, location_id, to_location_id, quantity, business_date, event_ts (UTC), reason_code, source_system. The key (`movement_id`, `version`) is not part of it. | Two rows with the same key are exact duplicates when these values match after normalisation (so `receipt` vs `RECEIPT`, or `Z` vs no timezone, are not treated as conflicts). |
| A13 | Number formats | Quantity and version must be plain whole numbers: `1.5`, `1e3`, `1,000` and `1.0` are rejected. A leading `+` is accepted on quantity. | "Integer only" in the spec; refusing to guess at formats is safer for stock data. |

## Deviations

None so far.
