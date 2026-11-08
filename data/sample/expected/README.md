# Expected results for the hand-made sample (worked out by hand)

These files are the **independent answer key** for `data/sample/`. They were worked out by hand
from the input files and the business rules. They were **not** produced by running the pipeline.
The end-to-end tests (`tests/test_sample_e2e.py`) compare the pipeline's output against them.

| File | Contents |
|------|----------|
| `expected_daily_balances.csv` | Every row of `daily_inventory_balance` (without `run_id`) after loading both batches |
| `expected_rejects.csv` | Every row of `quarantine_movement`: file, line, movement_id, primary reject reason |
| `expected_run_counts.csv` | The counts each batch's `pipeline_run` row must show |

## Reference data

- SKUs: `SKU-001` Widget, `SKU-002` Gadget, `SKU-003` Gizmo
- Locations: `WH-01` Central Warehouse, `ST-01` Store North, `ST-02` Store South
- Opening balances (on hand at the **end** of 2026-02-28, so 2026-02-28 and earlier is a closed period):

| sku | location | qty |
|-----|----------|----:|
| SKU-001 | WH-01 | 100 |
| SKU-001 | ST-01 | 20 |
| SKU-002 | WH-01 | 50 |

## Batch 1 (`input/batch_01.csv`): 17 data rows

Line numbers count the header as line 1.

| line | movement | what it is | outcome |
|-----:|----------|------------|---------|
| 2 | MV-1001 v1 | RECEIPT SKU-001 @WH-01, 40, 03-01 | accepted (later corrected to 45 by v2) |
| 3 | MV-1002 | ISSUE SKU-001 @WH-01, 15, 03-01 | accepted |
| 4 | MV-1003 | TRANSFER SKU-001 WH-01 → ST-01, 25, 03-02 | accepted (two legs) |
| 5 | MV-1004 | ADJUSTMENT SKU-001 @ST-01, −2, 03-02 | accepted |
| 6 | MV-1005 v1 | RECEIPT SKU-002 @WH-01, 30, 03-01 | accepted (later voided by v3) |
| 7 | MV-1006 | ISSUE SKU-001 @ST-01, 8, 03-02 | accepted |
| 8 | MV-1007 | TRANSFER SKU-002 WH-01 → ST-02, 10, 03-02 | accepted (ST-02 is a new pair) |
| 9 | MV-1008 | ADJUSTMENT SKU-002 @WH-01, +3, 03-02 (event_ts has no timezone, so UTC) | accepted |
| 10 | MV-1002 | exact copy of line 3 | **DUPLICATE_EVENT** (dropped, counted as a duplicate) |
| 11 | MV-1101 | quantity `ten` | **INVALID_QUANTITY** |
| 12 | MV-1102 | blank sku | **MISSING_REQUIRED_FIELD** |
| 13 | MV-1103 | sku `SKU-999` | **UNKNOWN_SKU** |
| 14 | MV-1104 | 12 fields instead of 11 (extra trailing value) | **MALFORMED_ROW** |
| 15 | MV-1105 | TRANSFER WH-01 → WH-01 | **INVALID_TRANSFER** |
| 16 | MV-1106 | movement_type `RETURN` | **INVALID_MOVEMENT_TYPE** |
| 17 | MV-1107 | RECEIPT SKU-001 @WH-01 dated 02-28 = the as_of_date | **BEFORE_OPENING_SNAPSHOT** |
| 18 | MV-1108 | business_date 03-05, but event_ts is 03-02 | **FUTURE_DATED** |

Counts: read 17 = accepted 8 + rejected 8 + duplicate 1. Nothing is late: the database was empty.
After batch 1 the latest business date (the global horizon) is **2026-03-02**.

## Batch 2 (`input/batch_02.csv`): 14 data rows

Late cutoff = the latest business date loaded before this file = 2026-03-02.

| line | movement | what it is | outcome |
|-----:|----------|------------|---------|
| 2 | MV-2001 | RECEIPT SKU-003 @ST-02, 12, 03-03 | accepted (new pair) |
| 3 | MV-2002 | ISSUE SKU-001 @WH-01, 10, 03-03 | accepted |
| 4 | MV-2003 | TRANSFER SKU-001 ST-01 → ST-02, 5, 03-03 | accepted (SKU-001 @ST-02 is a new pair) |
| 5 | MV-2004 | ISSUE SKU-001 @WH-01, 4, **03-01** (backdated) | accepted, **late** |
| 6 | MV-1001 **v2** | correction of line 2 of batch 1: qty 40 → 45, 03-01 | accepted, **late**, **supersedes v1** |
| 7 | MV-1005 **v3** | void (qty 0) of MV-1005, 03-01 | accepted, **late**, **supersedes v1** |
| 8 | MV-1005 **v2** | qty 28, arrives **after** v3 | accepted, **late**, history only (v3 stays current) |
| 9 | MV-2005 | ISSUE SKU-003 @WH-01, 7, 03-03 | accepted, the pair has no stock, so it **goes negative** |
| 10 | MV-2006 | location `WH-99` | **UNKNOWN_LOCATION** |
| 11 | MV-1006 v1 | same key as batch 1 line 7, but qty 9 instead of 8 | **CONFLICTING_DUPLICATE** (first arrival wins) |
| 12 | MV-2007 | RECEIPT qty 0 at version 1 | **INVALID_QUANTITY** |
| 13 | MV-2008 | business_date 2026-02-30 | **INVALID_DATE** |
| 14 | MV-2009 | ADJUSTMENT SKU-002 @ST-02, −1, 03-03 | accepted |
| 15 | MV-2010 | RECEIPT SKU-002 @ST-02, 6, 03-04, event_ts `2026-03-04T01:30:00+05:30` | accepted: the local date (03-04) is not before the business date, even though in UTC it is still 03-03 (A10) |

Counts: read 14 = accepted 10 + rejected 4 + duplicate 0. Late 4 (lines 5–8, all dated 03-01).
Superseding 2 (MV-1001 v2 and MV-1005 v3; MV-1005 v2 does not supersede because v3 is already there).
Final global horizon: **2026-03-04**.

## Current versions that drive the balances

| movement | current | leg(s) |
|----------|---------|--------|
| MV-1001 | v2 | +45 SKU-001 @WH-01 on 03-01 |
| MV-1002 | v1 | −15 SKU-001 @WH-01 on 03-01 |
| MV-1003 | v1 | −25 SKU-001 @WH-01, +25 SKU-001 @ST-01 on 03-02 |
| MV-1004 | v1 | −2 SKU-001 @ST-01 on 03-02 |
| MV-1005 | v3 (void) | 0 SKU-002 @WH-01 on 03-01 |
| MV-1006 | v1 (qty 8) | −8 SKU-001 @ST-01 on 03-02 |
| MV-1007 | v1 | −10 SKU-002 @WH-01, +10 SKU-002 @ST-02 on 03-02 |
| MV-1008 | v1 | +3 SKU-002 @WH-01 on 03-02 |
| MV-2001 | v1 | +12 SKU-003 @ST-02 on 03-03 |
| MV-2002 | v1 | −10 SKU-001 @WH-01 on 03-03 |
| MV-2003 | v1 | −5 SKU-001 @ST-01, +5 SKU-001 @ST-02 on 03-03 |
| MV-2004 | v1 | −4 SKU-001 @WH-01 on 03-01 |
| MV-2005 | v1 | −7 SKU-003 @WH-01 on 03-03 |
| MV-2009 | v1 | −1 SKU-002 @ST-02 on 03-03 |
| MV-2010 | v1 | +6 SKU-002 @ST-02 on 03-04 |

## Daily balances, pair by pair

Each pair starts the day after its opening date (03-01) if it has an opening balance, otherwise on
its first movement date. Every pair then runs, one row per day, to the horizon 03-04.
`closing = opening + receipt − issue + transfer_in − transfer_out + adjustment`.

**SKU-001 @ WH-01** (opening 100)
- 03-01: 100 + 45 (MV-1001 v2) − 15 (MV-1002) − 4 (MV-2004, late) = **126**. So issue = 19, net = +26.
- 03-02: 126 − 25 (MV-1003 transfer out) = **101**
- 03-03: 101 − 10 (MV-2002) = **91**
- 03-04: no movement = **91**

(After batch 1 alone this pair was 100 + 40 − 15 = 125 on 03-01 and 100 on 03-02. Batch 2's late issue
and the v2 correction both change 03-01, so every day from 03-01 is recomputed.)

**SKU-001 @ ST-01** (opening 20)
- 03-01: no movement = **20**
- 03-02: 20 + 25 (transfer in) − 8 (MV-1006) − 2 (MV-1004) = **35**, net +15
- 03-03: 35 − 5 (MV-2003 transfer out) = **30**
- 03-04: **30**

**SKU-001 @ ST-02** (no opening; first movement 03-03)
- 03-03: 0 + 5 (MV-2003 transfer in) = **5**
- 03-04: **5**

**SKU-002 @ WH-01** (opening 50)
- 03-01: 50 + 0 (MV-1005 is voided by v3; the v1 receipt of 30 and the v2 of 28 no longer count) = **50**
- 03-02: 50 − 10 (MV-1007 transfer out) + 3 (MV-1008) = **43**, net −7
- 03-03: **43**
- 03-04: **43**

**SKU-002 @ ST-02** (no opening; first movement 03-02)
- 03-02: 0 + 10 (MV-1007 transfer in) = **10**
- 03-03: 10 − 1 (MV-2009) = **9**
- 03-04: 9 + 6 (MV-2010) = **15**

**SKU-003 @ ST-02** (no opening; first movement 03-03)
- 03-03: 0 + 12 (MV-2001) = **12**
- 03-04: **12**

**SKU-003 @ WH-01** (no opening; first movement 03-03)
- 03-03: 0 − 7 (MV-2005) = **−7** (negative: a data-quality WARN, not a reject)
- 03-04: **−7**

No other pairs exist: every other movement for SKU-002 @ST-01, SKU-003 @ST-01 and WH-99 was rejected.

Total: 4 + 4 + 2 + 4 + 3 + 2 + 2 = **21 balance rows**. 2 of them are negative (SKU-003 @WH-01).

## Totals cross-check

| component | from the ledger | sum of the balance rows |
|-----------|-----------------|-------------------------|
| receipts | 45 + 0 + 12 + 6 = 63 | 45 + 12 + 6 = 63 |
| issues | 15 + 4 + 8 + 10 + 7 = 44 | 19 + 8 + 10 + 7 = 44 |
| transfer in | 25 + 10 + 5 = 40 | 25 + 5 + 10 = 40 |
| transfer out | 25 + 10 + 5 = 40 | 25 + 5 + 10 = 40 |
| adjustments | −2 + 3 − 1 = 0 | −2 + 3 − 1 = 0 |

Final on-hand across all pairs on 03-04: 91 + 30 + 5 + 43 + 15 + 12 − 7 = **189**
= openings 170 + receipts 63 − issues 44 + adjustments 0 (transfers cancel out). ✔

## Edge-case files (`edge_cases/`)

| File | Expected outcome |
|------|------------------|
| `header_only.csv` | SUCCESS, empty batch (0 rows) |
| `zero_bytes.csv` | SUCCESS, empty batch (0 rows) |
| `missing_required_column.csv` | REJECTED (no `quantity` column); nothing loaded |
| `duplicate_column.csv` | REJECTED (`sku` appears twice); nothing loaded |
| `extra_and_reordered_columns.csv` | SUCCESS with a schema warning for `batch_note`. Columns are matched by name, so MV-3003 (RECEIPT 7 SKU-001 @WH-01 on 03-04) loads, and SKU-001 @WH-01 on 03-04 becomes 91 + 7 = **98** |
