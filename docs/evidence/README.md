# Evidence

Real outputs of real commands. Nothing here was typed by hand.

## Fresh-clone run (pytest, demo, reconciliation report, sample output)

Captured on 2026-11-09 from a **fresh clone** of the GitHub repository at commit `e7f5c6e` (the final code,
including the Phase 8 dashboard), into an empty temporary folder, following only the README. This replaces
an earlier capture from commit `a062124` (171 tests, before the dashboard), which is still in the git history.

```
git clone https://github.com/risri-odoo/inventory-daily-balance-pipeline.git
cd inventory-daily-balance-pipeline
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m inventory_pipeline demo      > demo_output.txt   (exit code 0)
.venv\Scripts\python -m pytest -v                     > pytest_output.txt (exit code 0)
```

| File | What it is |
|------|------------|
| [pytest_output.txt](pytest_output.txt) | `pytest -v`: **179 passed** |
| [demo_output.txt](demo_output.txt) | Console output of `demo`: every run, every check, files written, `exit_code=0` |
| [reconciliation_report.md](reconciliation_report.md) / [.json](reconciliation_report.json) | The report written by `demo`: 11 checks PASS, `negative_balances` WARN (the deliberate SKU-003 @ WH-01 pair) |
| [sample_output/daily_inventory_balance.csv](sample_output/daily_inventory_balance.csv) | The 21 balance rows for the sample, identical to the hand-computed [expected file](../../data/sample/expected/expected_daily_balances.csv) apart from the `run_id` column |
| [sample_output/quarantine_movement.csv](sample_output/quarantine_movement.csv) | The 13 quarantined rows, with reasons, details and original lines |
| [sample_output/pipeline_run.csv](sample_output/pipeline_run.csv) | The run log of the demo (reference load + 2 batches) |
| [sample_output/dashboard.html](sample_output/dashboard.html) | The offline dashboard written by `demo` (27 KB, no external URLs). Download it and open it in a browser; it shows on-hand 189 and 13 quarantined rows |

**Edits made to these files** (and only these):
- In `pytest_output.txt`, the absolute path of the temporary clone folder (which contains the Windows user
  name) was replaced by `<clone>` in 2 places (pytest's `platform` and `rootdir` header lines).
- Line endings were normalised to LF.

Machine: Windows 11, Python 3.13.7, SQLite 3.50.4.

## Benchmark (100,000 rows)

| File | What it is |
|------|------------|
| [benchmark_baseline.json](benchmark_baseline.json) | First 100k run (pipeline code as of commit `23b0a40`), before the incremental-read optimization |
| [benchmark_results.json](benchmark_results.json) | 100k run after the optimization (commit `87d9b1a`). [../benchmark.md](../benchmark.md) is generated from it |

Both were produced by `python scripts/benchmark.py` (seed 42) on the same laptop. They contain CPU and OS
details only.
