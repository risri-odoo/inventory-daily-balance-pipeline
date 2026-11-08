# Walkthrough (interview preparation)

## The flow in simple words

1. **Reference data first.** `load-reference` loads the SKU list, the location list and the opening stock
   per (SKU, location) as of a date. Loading the same files again changes nothing.
2. **Each movement file is one batch,** processed in filename order. For each file:
   - Write a `pipeline_run` row saying RUNNING, and commit it straight away, so even a crash leaves a trace.
   - If these exact bytes were already processed (same SHA-256), stop: SKIPPED.
   - Check the header. A missing required column or a repeated column rejects the whole file.
   - Start **one transaction**. Read every row:
     - broken rows go to `quarantine_movement` with their file, line, original text and every rule they broke;
     - a row whose (movement_id, version) was already loaded is a duplicate: dropped if identical, rejected
       if different (the first arrival wins);
     - good rows are appended to `movement_event`, which is never updated.
   - Work out which (SKU, location) pairs changed and from which date, and recompute only those days.
   - Mark the file processed, set the run to SUCCESS, and COMMIT. On any error: ROLLBACK, set FAILED.
3. **Balances** are one row per (SKU, location, day), with no gaps, up to the latest business date
   seen. They are always derived from history, so they can be rebuilt at any time.
4. **Reconcile** recomputes everything independently in SQL and reports PASS / FAIL / WARN.
   **Report** writes the results plus CSV exports.

## One record traced from CSV to balance

Take `data/sample/input/batch_01.csv` line 4:

```
MV-1003,1,TRANSFER,SKU-001,WH-01,ST-01,25,2026-03-02,2026-03-02T07:15:00Z,REPLENISH,WMS
```

1. **ingest** (`read_movement_file`): the header matches the contract, and the row has 11 fields, so it
   becomes a dict, with `source_line = 4` and `raw_record` = the text above.
2. **validate** (`validate_row`), in this order:
   - required fields present;
   - `TRANSFER` is a valid type;
   - version 1;
   - quantity 25 is a whole number > 0;
   - the date is real;
   - the timestamp parses as UTC;
   - the SKU and both locations exist;
   - the destination differs from the source;
   - not future-dated (03-02 ≤ 03-02);
   - not in a closed period (SKU-001@WH-01 and SKU-001@ST-01 both closed up to 02-28, and 03-02 is later).

   The result is a `Movement` with a `payload_hash`.
3. **load** (`load_rows`):
   - (MV-1003, 1) is not in `movement_event` yet, so it is inserted with `run_id`,
     `source_file = batch_01.csv`, `source_line = 4`.
   - It is the first version, so both pairs it touches are marked affected from 03-02:
     `(SKU-001, WH-01)` and `(SKU-001, ST-01)`.
4. **views:** `v_movement_current` includes it (no higher version). `v_movement_leg` turns it into two legs:
   `TRANSFER_OUT −25 @ WH-01` and `TRANSFER_IN +25 @ ST-01`.
5. **balances** (`incremental_rebuild` → `rebuild_pair` → `build_pair_rows`): for SKU-001@ST-01, the
   02-28 opening balance is 20.
   - 03-01: 20 → 20 (no movement).
   - 03-02: opening 20, transfer_in 25, issue 8 (MV-1006), adjustment −2 (MV-1004), so net +15 and
     closing **35**.
6. **reconcile:**
   - `independent_recompute`: opening 20 + running sum of the legs on 03-02 (25 − 8 − 2) = 35. PASS.
   - `transfer_balance`: the 25 out of WH-01 equals the 25 into ST-01.

## How a backdated correction flows through

Batch 2 has three rows dated 03-01, although batch 1 already went up to 03-02:
- line 5, `MV-2004`: a late issue of 4 at SKU-001@WH-01;
- line 6, `MV-1001` v2: corrects the batch-1 receipt from 40 to 45;
- lines 7–8, `MV-1005` v3 (a void, qty 0) arrives **before** `MV-1005` v2 (qty 28).

1. **Late flag.** Before batch 2, the max loaded business date was 03-02. Every row dated 03-01 gets
   `is_late = 1` (4 rows, so `rows_late = 4`).
2. **Corrections.**
   - `MV-1001` v2 has a higher version than the stored v1, so it becomes current (`rows_superseding += 1`).
     The pair is affected from 03-01, both for the new version's leg and for the replaced v1's leg.
   - `MV-1005` v3 also becomes current, replacing v1.
   - When v2 arrives afterwards, a higher version (v3) already exists, so v2 is stored as history only and
     changes no balances. v1 and v2 stay in `movement_event`: nothing is overwritten.
3. **Recompute.**
   - SKU-001@WH-01 is rebuilt from 03-01: 100 + 45 − 15 − 4 = **126** (it was 125 after batch 1).
   - Every later day is shifted: 03-02 is 101, not 100.
   - SKU-002@WH-01 is rebuilt from 03-01: the receipt of 30 no longer counts (voided), so 50 instead of 80.
   - Pairs that nothing touched are only extended to the new horizon (03-04).
4. **Evidence.** `test_sample_e2e::test_late_batch_recomputes_past_days` checks 125 becomes 126 and the
   following days. `::test_versions_current_and_history` checks v3 is current and v1, v2 and v3 are all
   stored.

## The 10 questions a reviewer is most likely to ask

1. **Why SQLite and not Spark, DuckDB or pandas?**
   It needs zero setup, has real transactions, real SQL with window functions for the checks, and the code
   stays readable. It is single-writer and single-core: 100k rows load in about 22 s, which is fine for this
   size. The SQL ports to DuckDB, and partitioning by date is the next step at scale.
2. **How do you make reruns safe?**
   Two layers. A file hash skips identical files; the (movement_id, version) key turns any re-sent row into
   a counted duplicate. Plus one transaction per file, so a run is all or nothing. `--force` is safe: every
   row becomes a duplicate and the balances don't change.
3. **What happens if it crashes halfway?**
   The transaction rolls back, so no events, rejects, balances or processed marker are left. The RUNNING row
   was committed first, and is updated to FAILED with the error. Rerunning the same command loads the file
   cleanly. Shown with `--fail-at`, and the `run_counts` check verifies a FAILED run left zero rows.
4. **How do late and backdated events work?**
   They are flagged `is_late`. The rebuild starts at the earliest affected date for each affected pair, and
   all days after it are recomputed. See the backdated correction above.
5. **Why versions rather than reversal entries?**
   That's the source contract. It works in any arrival order (highest version wins), and the history is
   append-only. A correction can even change the location: the old version's legs are marked affected too,
   so stock moves between pairs.
6. **How do you know incremental gives the right answer?**
   The full rebuild uses the same per-pair function, and tests compare the two after corrections, location
   changes, late data and out-of-order versions. They are compared on sample, generated and 100k data.
   The SQL checks recompute independently.
7. **Why is negative stock only a warning?**
   The issue really happened. A negative balance means some *other* event is missing or late. Rejecting the
   issue would hide a real movement, while a WARN surfaces it. The late receipt, when it arrives, fixes the
   history.
8. **How do you know the expected results are right and not just copied from the output?**
   The sample answer key was worked out by hand, with the arithmetic in `data/sample/expected/README.md`.
   The generator's oracle comes from its own event list, not pipeline code. And every reconciliation check
   has a test that corrupts data and proves the check fails.
9. **What did you measure for performance, and what would you do next?**
   The first benchmark showed incremental was only 1.1× faster than full, because both read the whole
   ledger. The view was rewritten to `NOT EXISTS`, and the incremental path now reads only affected pairs
   (with indexes). Result: 3.2× faster, and the whole load 48 s → 22 s. Next: bulk key lookups instead of
   per-row queries, a date filter on the ledger read, DuckDB, monthly partitions.
10. **What are the limitations?**
    - Single writer.
    - One record per line (no multi-line CSV values).
    - Reference rows are never deleted.
    - Opening-balance changes trigger a full rebuild.
    - Only tested on Windows / Python 3.13.
    - Generator corrections only increase quantities, so negative-stock cases come from the hand-made
      sample, not the generator.

## The parts most important to understand

1. **`load.load_rows`** ([load.py](../inventory_pipeline/load.py)): validation → duplicate check →
   insert → current or history-only → which pairs are affected. Most correctness questions lead here.
2. **`balances.rebuild_pair` and `incremental_rebuild`** ([balances.py](../inventory_pipeline/balances.py)):
   - where a pair starts;
   - why the day before `from_date` supplies the opening;
   - why rows before the start get deleted;
   - how an unaffected pair is extended.
3. **`pipeline.run_file`** ([pipeline.py](../inventory_pipeline/pipeline.py)): the order of RUNNING →
   SKIPPED → header → transaction, and what is committed when.
4. **The two views** in [schema.py](../inventory_pipeline/schema.py): `v_movement_current` (highest version
   via `NOT EXISTS`) and `v_movement_leg` (signs, and two legs for a transfer).
5. **`check_independent_recompute` and `check_continuity`** ([reconcile.py](../inventory_pipeline/reconcile.py)):
   the window-function SQL.
6. **The sample answer key** ([data/sample/expected/README.md](../data/sample/expected/README.md)): be able
   to recompute SKU-001@WH-01 and SKU-002@WH-01 on paper.

## A 5-minute live demo script

```
python -m inventory_pipeline demo                      # sample end to end, exit 0, one WARN
python -m inventory_pipeline run --input data/sample/input --output-dir output --db output/demo.db
#   both files SKIPPED (same SHA-256)
python -m inventory_pipeline run --input data/sample/input --force --db output/demo.db
#   every row is a duplicate or a reject again; balances unchanged
python -m inventory_pipeline reconcile --db output/demo.db   # all PASS + negative WARN
```

Then open `output/reconciliation_report.md`, and run queries 5 and 6 from `sql/sample_queries.sql`
(the trace of a rejected row, and a movement's version history).
