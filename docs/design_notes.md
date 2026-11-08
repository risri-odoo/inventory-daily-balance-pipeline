# Design notes: decisions and trade-offs

The brief asks for correctness, clear design and evidence of testing over unnecessary complexity.
Each decision below follows from that. Smaller judgement calls are listed in
[assumptions_and_deviations.md](assumptions_and_deviations.md).

## 1. SQLite + standard-library Python, not Spark, DuckDB or pandas

**Chosen:** `sqlite3` for storage and as the target, plain Python for the logic, and pytest as the only
third-party package.

- **For:** zero setup (no server, no JVM, nothing beyond Python itself), runs the same on Windows and Linux,
  real transactions (needed for all-or-nothing loads), real SQL for the reconciliation checks
  (window functions), and every line is readable by a junior engineer.
- **Against:** single writer, single core, row-at-a-time Python validation. Measured: 100,000 rows load in
  about 22 s on a laptop ([benchmark.md](benchmark.md)). That is fine for a daily batch of this size, and
  not fine for billions of rows.
- **Path to scale:** the SQL (views, window functions) ports almost unchanged to DuckDB. Validation could
  become vectorised (Polars or DuckDB expressions). Balances could be partitioned by month so a backdated
  correction only rewrites recent partitions. Spark is only worth its operational cost at a much larger scale.

## 2. Dense daily grid, not sparse (only days with movements)

**Chosen:** one row per pair per day, from the pair's start to the global horizon, carrying the balance forward.

- **For:** "what was on hand at X on date D?" is a plain lookup with no "find the last movement before D"
  logic for consumers. Continuity checks (no gaps, opening = yesterday's closing) become possible.
  BI tools and joins to calendars just work.
- **Against:** storage grows with pairs × days, even when nothing moves (500 pairs × 30 days = 15,000 rows
  in the benchmark, against 100,000 movements). For 1M pairs × 5 years this would be about 1.8 billion rows.
- **At larger scale:** store sparse change rows and expose a dense *view*, or keep dense rows only for a
  recent window and month-end snapshots before that.

## 3. Version-based corrections, not reversal entries

**Chosen:** a correction is a new row with the same `movement_id` and a higher `version`. The current state
is the highest version ever received. A void is a new version with quantity 0. History is append-only.

- **For:** matches the source contract (`version` column). It's order-independent: if v3 arrives before v2,
  v3 stays current and v2 is kept as history only (tested). Nothing is overwritten, so every past state can be
  explained. A correction can change anything (quantity, date, even location), and the old version's legs
  are simply no longer counted.
- **Against:** "current" has to be computed (`v_movement_current`) rather than read directly, and a
  correction's effect is implicit (the difference between two versions) instead of an explicit
  reversal row. An accounting ledger often prefers reversal and re-post entries for auditability.
- **Mitigation:** the full history and `rows_superseding` per run make every correction visible, and
  `sql/sample_queries.sql` (query 6) shows a movement's versions side by side.

## 4. Incremental recompute, with full rebuild as the reference

**Chosen:** each run collects the earliest affected date per (sku, location). That covers the new current
version's legs **and** the legs of the version it replaced, so a correction that moves stock between
locations fixes both. Each affected pair is rebuilt from that date to the horizon; earlier days are kept.
Other pairs are extended when the horizon moves forward. `--full-rebuild` rebuilds everything from
history.

- **For:** a small late batch rewrites only what changed. Measured at 100k rows: incremental rebuild 288 ms
  vs. 919 ms for a full rebuild (3.2×). Identical results are verified by tests on hand-made, generated and
  benchmark data.
- **Against:** more logic, so more chances of a bug (missing an affected pair, or a stale row past the start).
- **Mitigation:** the full rebuild uses the same per-pair function. Tests compare incremental with full after
  corrections, location changes, late data and out-of-order versions. The independent SQL checks
  (`independent_recompute`, `continuity`, `pair_coverage`) run after every load in `report` / `demo`.
- **Lesson recorded in the benchmark:** the first version read the whole ledger even in incremental mode,
  and was only 1.1× faster. Reading only the affected pairs (a filterable view plus two indexes) fixed it.
  The before and after numbers are both kept as evidence.

## 5. Negative stock is a warning, not a reject

**Chosen:** an issue that takes stock below zero is accepted. The `negative_balances` check reports WARN with
a count and examples, and `pipeline_run.negative_balance_rows` tracks it per run.

- **Why:** the issue really happened (goods left the building). A negative balance almost always means a
  *different* event is missing or late: a receipt not yet sent, a transfer recorded at the wrong location.
  Rejecting the issue would hide a real movement and make the balance wrong in a different way. When the
  missing receipt arrives (as late data), the backdated recompute fixes the history automatically.
- **Trade-off:** consumers can see negative numbers. They are labelled (WARN in the report) instead of
  silently clamped to zero.

## 6. File-hash idempotency and key-based idempotency (both)

**Chosen:**
1. **File level:** the SHA-256 of a file's bytes goes in `processed_file`, written in the same transaction as
   the data. A re-run of the same bytes is `SKIPPED` without even being parsed.
2. **Row level:** `(movement_id, version)` is the primary key of `movement_event`. Any row seen before is a
   `DUPLICATE_EVENT` (identical payload) or a `CONFLICTING_DUPLICATE` (different payload); the first
   arrival wins.

- **Why both:** the file hash is cheap and catches the common case (a scheduler re-runs yesterday's file),
  but it is blind to the same rows arriving in a *different* file (a re-export, a renamed file with one extra
  row, an overlapping extract). The key check catches those, plus duplicates *within* one file. It also makes
  `--force` safe: re-processing a file on purpose turns every previously loaded row into a counted duplicate,
  and balances don't change (tested).
- **Why the payload hash:** it separates harmless resends from real conflicts. A conflict is surfaced
  (rejected, with a pointer to the first arrival in `reject_details`) instead of silently overwriting data.

## 7. Transactions, retries and backfills

- **One transaction per file:** the `RUNNING` row is committed first (a crash is still visible). Everything
  else (events, quarantine, balances, `processed_file`, `SUCCESS`) commits together. On any exception the
  transaction rolls back and the run is marked `FAILED` with the error.
- **Retry** = run the same command again. The failed file was never marked processed, so it is picked up and
  loads cleanly. `--fail-at after_load|after_rebuild` demonstrates this, and tests prove that a failed run
  leaves zero rows behind.
- **Folder runs stop at the first FAILED file,** so batches are never applied ahead of an earlier failed one.
  A `REJECTED` file (bad header) doesn't block later files: it needs fixing at the source, not a retry.
- **Backfill / late data:** just load the file. Rows dated before the current horizon are flagged `is_late`,
  and the recompute starts from the earliest affected date. A changed opening balance triggers a full rebuild.
- **Rebuild from scratch:** delete the database, `load-reference`, then `run` the input folder again.
  Results are deterministic, as the demo-twice test shows.

## 8. Validation design

- **Pure function** (`validate_row`) with no database access, so every rule is unit-tested in isolation.
- **All failures recorded,** with the first one as primary, in the spec's fixed order, so the primary reason
  is predictable. Checks that depend on an already-failed value are skipped (for example, a non-numeric
  quantity is not also range-checked), to avoid confusing cascades.
- **The future-dated rule** compares `business_date` with the **local** calendar date written in `event_ts`.
  An event recorded at 01:00 in India (+05:30) on 4 March is a 4 March event, even though it is still 3 March
  in UTC.

## What was deliberately left out

- **Deleting reference rows** (upsert only). Master-data retirement would need effective dating.
- **Concurrency:** one writer at a time is assumed (SQLite).
- **Multi-line quoted CSV values:** one record per line keeps `source_line` and `raw_record` exact.
- **Timezone-aware business dates per location:** `business_date` is taken as given.
- **A web UI or scheduler:** the CLI's exit codes are designed to be driven by cron, Airflow and similar tools.
