# Reconciliation report

Generated at: 2026-11-08T10:58:08Z

**Overall status: WARN**

## Checks

| check | status | failures | details |
|---|---|---|---|
| run_counts | PASS | 0 | 3 runs checked; rows read by successful runs = 31; rows in tables = 31 |
| independent_recompute | PASS | 0 | 13 (pair, movement date) points compared |
| continuity | PASS | 0 |  |
| row_arithmetic | PASS | 0 | 21 balance rows checked |
| component_totals | PASS | 0 | ledger totals: receipt=63, issue=44, transfer_in=40, transfer_out=40, adjustment=0 |
| transfer_balance | PASS | 0 | transfer_in=40, transfer_out=40 |
| pair_coverage | PASS | 0 | 7 pairs; global horizon 2026-03-04 |
| current_version_unique | PASS | 0 | 15 distinct movements |
| referential_integrity | PASS | 0 |  |
| quarantine_traceability | PASS | 0 | 13 quarantine rows checked |
| closed_period_legs | PASS | 0 |  |
| negative_balances | WARN | 2 | 2 negative rows across 1 pairs |

### run_counts: PASS

Per run: rows_read = accepted + rejected + duplicate, matching movement_event and quarantine_movement counts for that run_id.

### independent_recompute: PASS

Opening balance + SQL window cumulative sum of leg deltas equals closing_qty on every movement date.

### continuity: PASS

Opening = previous day's closing with no date gaps; first opening = opening balance (or 0).

### row_arithmetic: PASS

closing = opening + net_change, and net_change = receipt - issue + transfer_in - transfer_out + adjustment, on every row.

### component_totals: PASS

Receipt, issue, transfer-in, transfer-out and adjustment totals in daily_inventory_balance equal the ledger.

### transfer_balance: PASS

Total transfer_in_qty equals total transfer_out_qty.

### pair_coverage: PASS

Every pair runs from its start date (as_of_date + 1, or first movement) to the global horizon; no unexpected pairs.

### current_version_unique: PASS

Exactly one current version per movement_id.

### referential_integrity: PASS

No orphan sku or location values in movement_event, daily_inventory_balance or opening_balance.

### quarantine_traceability: PASS

Every quarantine row has source_file, source_line, raw_record and a reject_reason.

### closed_period_legs: PASS

No accepted leg is dated on or before its pair's opening as_of_date (can only happen if an opening balance changed after loading).

### negative_balances: WARN

Closing balances below zero (data-quality warning: stock issued that was never received).

Examples:

| sku | location_id | balance_date | closing_qty |
|---|---|---|---|
| SKU-003 | WH-01 | 2026-03-03 | -7 |
| SKU-003 | WH-01 | 2026-03-04 | -7 |

## Table row counts

| table | rows |
|---|---|
| dim_sku | 3 |
| dim_location | 3 |
| opening_balance | 3 |
| movement_event | 18 |
| quarantine_movement | 13 |
| daily_inventory_balance | 21 |
| pipeline_run | 3 |
| processed_file | 2 |

## Rejected and duplicate rows by reason

| reject_reason | row_count |
|---|---|
| BEFORE_OPENING_SNAPSHOT | 1 |
| CONFLICTING_DUPLICATE | 1 |
| DUPLICATE_EVENT | 1 |
| FUTURE_DATED | 1 |
| INVALID_DATE | 1 |
| INVALID_MOVEMENT_TYPE | 1 |
| INVALID_QUANTITY | 2 |
| INVALID_TRANSFER | 1 |
| MALFORMED_ROW | 1 |
| MISSING_REQUIRED_FIELD | 1 |
| UNKNOWN_LOCATION | 1 |
| UNKNOWN_SKU | 1 |

## Runs

| run_id | source_file | status | rows_read | rows_accepted | rows_rejected | rows_duplicate | rows_late | rows_superseding | min_affected_date | pairs_rebuilt | balance_rows_written | negative_balance_rows | duration_ms | error_message |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| run-20261108T105808Z-399f5453 | reference | SUCCESS | 0 | 0 | 0 | 0 | 0 | 0 |  | 3 | 0 | 0 | 44 |  |
| run-20261108T105808Z-2145111d | batch_01.csv | SUCCESS | 17 | 8 | 8 | 1 | 0 | 0 | 2026-03-01 | 4 | 7 | 0 | 18 |  |
| run-20261108T105808Z-1092996f | batch_02.csv | SUCCESS | 14 | 10 | 4 | 0 | 4 | 2 | 2026-03-01 | 7 | 18 | 2 | 18 |  |
