# Verification: how I know it works

Three independent kinds of evidence:

1. **A hand-computed answer key:** [data/sample/expected/](../data/sample/expected/README.md) has every
   balance row, reject and run count for the sample, worked out by hand with the arithmetic shown, and
   never derived from pipeline output. Tests compare the pipeline against it.
2. **A generator oracle:** the data generator writes per-row expected rejects, per-batch counts and final
   balances *outside* the input folder, computed from its own event list. Tests (3,000 rows) and the
   benchmark (100,000 rows) compare the pipeline against it.
3. **Independent reconciliation checks:** 12 SQL checks recompute the balances (window running sums,
   continuity, totals) without using the Python rebuild code. Every check has a tampering test that
   corrupts the matching data and proves the check turns FAIL.

Evidence files (real command output, see [evidence/README.md](evidence/README.md)):
- [evidence/pytest_output.txt](evidence/pytest_output.txt): `pytest -v` from a fresh clone
- [evidence/demo_output.txt](evidence/demo_output.txt): `python -m inventory_pipeline demo` from a fresh clone
- [evidence/reconciliation_report.md](evidence/reconciliation_report.md) / [.json](evidence/reconciliation_report.json): the demo's report
- [evidence/benchmark_results.json](evidence/benchmark_results.json) and [benchmark.md](benchmark.md): 100k-row run

Test files are in [`tests/`](../tests/). Names below are `file::test`.

## Acceptance criteria (assignment, "Validation Rules / Acceptance Criteria")

| Criterion | Proved by |
|-----------|-----------|
| **Primary keys are checked** | PK constraints in the schema; `(movement_id, version)` duplicates detected: `test_load_pipeline::test_duplicates_within_one_file`, `::test_duplicates_across_files_first_arrival_wins`. Reference keys unique: `reference.check_unique`. Check `current_version_unique`. |
| **Referential integrity is checked** | Validation: `test_validate::test_unknown_sku`, `::test_unknown_location`, `::test_unknown_transfer_destination`; opening balances: `test_load_pipeline::test_bad_reference_file_fails_and_rolls_back`. Check `referential_integrity` + `test_reconcile_report::test_orphan_reference_is_caught`. |
| **Transformation rules are deterministic** | `test_reconcile_report::test_cli_demo_runs_end_to_end_and_is_repeatable` (two demos → same balances); `test_generator::test_same_seed_gives_byte_identical_files`; `test_sample_e2e::test_incremental_equals_full_rebuild`; `test_load_pipeline::test_out_of_order_versions_match_in_order_arrival`. |
| **Source-to-target counts reconcile** | Check `run_counts` (rows_read = accepted + rejected + duplicate, matching table counts per run_id); `test_sample_e2e::test_run_counts_are_right`; `test_generator::test_batch_counts_equal_labels`; `test_reconcile_report::test_run_count_mismatch_is_caught`. |
| **Key aggregates reconcile or explain differences** | Checks `independent_recompute`, `component_totals`, `transfer_balance`, `continuity`, `row_arithmetic`, `pair_coverage`; differences that are expected are explained as WARN (`negative_balances`, `closed_period_legs`). Hand totals in [expected/README.md](../data/sample/expected/README.md#totals-cross-check); `sql/sample_queries.sql` query 10 (189 = 189). |
| **Invalid records are traceable** | Every reject keeps `run_id`, `source_file`, `source_line`, `raw_record`, all failures: `test_sample_e2e::test_every_reject_keeps_its_raw_record`, `test_load_pipeline::test_rows_store_run_id_and_relative_source_file`; check `quarantine_traceability` + `test_reconcile_report::test_untraceable_quarantine_row_is_caught`. |
| **Reruns do not create unintended duplicates** | `test_sample_e2e::test_rerun_is_skipped_with_no_new_rows`, `::test_force_rerun_turns_every_row_into_a_duplicate`, `test_load_pipeline::test_rerun_same_file_is_skipped`, `::test_force_rerun_turns_rows_into_duplicates`. |
| **Tests pass** | [evidence/pytest_output.txt](evidence/pytest_output.txt) |
| **Setup and run instructions work from a clean environment** | Fresh clone into an empty temp folder, following only the README: new venv, `pip install -r requirements.txt`, `demo`, `pytest -v` ([evidence/README.md](evidence/README.md)). |

## Required edge cases / negative scenarios

| Scenario | Proved by |
|----------|-----------|
| Malformed input | `test_ingest::test_wrong_field_count_is_malformed`; sample batch 1 line 14 → `MALFORMED_ROW` (`test_sample_e2e::test_rejects_equal_expected_rejects`) |
| Missing required field | `test_validate::test_each_blank_required_field_is_rejected` (all 7 fields), `::test_whitespace_only_counts_as_blank` |
| Duplicate key / event | `test_load_pipeline::test_duplicates_within_one_file`, `::test_duplicates_across_files_first_arrival_wins`, `::test_exact_duplicate_ignores_formatting_differences` |
| Unexpected schema change | Missing column: `test_sample_e2e::test_bad_header_files_are_rejected`; duplicate column: same test; extra and reordered columns: `::test_extra_and_reordered_columns_load_with_warning` |
| Empty batch | `test_sample_e2e::test_empty_batches_succeed` (header-only and zero-byte), `test_load_pipeline::test_empty_batch_is_success` |
| Partial failure and retry | `test_sample_e2e::test_partial_failure_then_retry`, `test_load_pipeline::test_partial_failure_writes_nothing_then_retry_succeeds` (both `--fail-at` points), `::test_folder_run_stops_at_first_failure`, `test_reconcile_report::test_failed_run_with_nothing_written_passes_run_counts` |
| Late-arriving / out-of-order data | `test_sample_e2e::test_late_batch_recomputes_past_days`, `::test_late_rows_are_flagged`, `::test_out_of_order_batches_give_same_balances`, `test_load_pipeline::test_late_event_recomputes_past_days`, `::test_out_of_order_versions_match_in_order_arrival` |
| Invalid reference key | `test_validate::test_unknown_sku`, `::test_unknown_location`; sample `MV-1103`, `MV-2006` |
| Rerun of the same input | `test_sample_e2e::test_rerun_is_skipped_with_no_new_rows` |

## Step 5 test categories

| Category | Examples |
|----------|----------|
| Happy path | `test_sample_e2e::test_balances_equal_hand_computed_file`, `test_load_pipeline::test_batch_produces_hand_computed_balances` |
| Empty input | `test_sample_e2e::test_empty_batches_succeed`, `test_reconcile_report::test_empty_database_passes` |
| Malformed records | `test_ingest::test_wrong_field_count_is_malformed`, `test_validate::test_non_integer_quantity_fails`, `::test_invalid_business_date`, `::test_invalid_event_ts` |
| Duplicates | see above |
| Nulls | `test_validate::test_each_blank_required_field_is_rejected`, `::test_blank_version_defaults_to_1` |
| Domain edge cases | backdated correction (`test_sample_e2e::test_late_batch_recomputes_past_days`); v3 void before v2 (`::test_versions_current_and_history`); a correction that changes location moves stock (`test_load_pipeline::test_correction_that_changes_location_moves_stock`); closed period on both transfer legs (`test_validate::test_transfer_destination_leg_in_closed_period_is_rejected`); negative stock as WARN (`test_sample_e2e::test_all_checks_pass_and_negative_pair_is_warn`); local-date future check (`test_validate::test_future_dated_uses_local_date_ahead_of_utc`); quantity boundaries (`::test_quantity_at_max_passes`, `::test_quantity_above_max_fails`, `::test_zero_quantity_at_version_2_is_a_void_and_passes`) |

## Step 4: operational metrics

`pipeline_run` stores `run_id`, `started_at`, `ended_at`, `duration_ms`, `rows_read`, `rows_accepted`,
`rows_rejected`, `rows_duplicate`, `rows_late`, `rows_superseding`, `pairs_rebuilt`, `balance_rows_written`,
`negative_balance_rows`, `schema_warnings`, `stage_timings_ms` and `error_message`. Every run logs
`event=run_started` and `event=run_finished` lines with the same fields to the console and
`output/logs/pipeline.log` (see [evidence/demo_output.txt](evidence/demo_output.txt)).
Tests: `test_load_pipeline::test_batch_produces_hand_computed_balances` (stage timings),
`::test_cli_run_exit_codes_and_log_file` (log lines, statuses, exit codes).

## Step 6: idempotency, retry, restart, backfill

Documented in the [README](../README.md#rerun-retry-and-backfill) and
[design_notes.md §6–7](design_notes.md#6-file-hash-idempotency-and-key-based-idempotency-both). Tested as above.

## Step 9: performance

[benchmark.md](benchmark.md): 100,000 rows, time per stage, rows/s, and one optimization measured
(incremental recompute vs. full rebuild, before and after narrowing the ledger read).
At 100k rows, reconciliation is PASS and rejects, batch counts and final balances match the generator oracle
exactly.

## Known gaps in the evidence

- Run on Windows 11 only. The code uses `pathlib`, LF line endings are pinned by `.gitattributes`, and no
  shell scripts are needed, but no Linux run was captured as evidence.
- Python 3.13 only. The code avoids newer syntax and targets 3.10+, but 3.10 was not run.
- Benchmark timings come from one laptop run, so they will vary. The counts and correctness results are
  deterministic.
