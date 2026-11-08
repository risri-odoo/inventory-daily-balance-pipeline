# Architecture

A single-process Python pipeline (standard library only) with SQLite as both storage and target.
There are no services to run: everything is one package, one database file and plain CSV in and out.

## Flow

```mermaid
flowchart TD
    REF[reference/*.csv<br/>dim_sku, dim_location, opening_balance] -->|load-reference<br/>upsert, no-op on reload| DIMS[(dim_sku<br/>dim_location<br/>opening_balance)]
    DIMS -. opening balance changed .-> FULL[full rebuild]

    IN[input/*.csv<br/>one batch per file,<br/>filename order] --> RUN{{run: one file}}
    RUN -->|1. insert RUNNING, commit| PR[(pipeline_run)]
    RUN -->|2. SHA-256 already in processed_file?| SKIP[SKIPPED]
    RUN -->|3. header contract| REJ[REJECTED<br/>missing / duplicate column]
    RUN -->|4. BEGIN| TX

    subgraph TX [one transaction per file]
        direction TB
        V[validate each row<br/>reject codes] -->|invalid| Q[(quarantine_movement)]
        V -->|valid| D{same movement_id + version<br/>already loaded?}
        D -->|identical payload| Q
        D -->|different payload| Q
        D -->|new| E[(movement_event<br/>append-only history)]
        E --> CUR[[v_movement_current<br/>highest version]]
        CUR --> LEG[[v_movement_leg<br/>signed legs; transfer = 2 legs]]
        LEG --> B[incremental rebuild<br/>affected pairs from earliest affected date]
        B --> DIB[(daily_inventory_balance<br/>dense: every pair, every day to horizon)]
        DIB --> OK[mark processed_file,<br/>run SUCCESS, COMMIT]
    end

    TX -.->|any error, or --fail-at| RB[ROLLBACK<br/>run FAILED + error_message]

    DIB --> REC[reconcile<br/>12 PASS / FAIL / WARN checks]
    E --> REC
    Q --> REC
    PR --> REC
    REC --> RPT[report<br/>reconciliation_report.md/.json<br/>+ CSV exports]
    REC --> DASH[dashboard<br/>output/dashboard.html<br/>read-only, offline]
    DIB --> DASH
```

The dashboard (`dashboard.py`) is a read-only view for walkthroughs. It reads the same tables and runs the
same checks as `report`, and writes nothing to the database.

## Module responsibilities

All modules live in `inventory_pipeline/`. Each one does one job and can be read on its own.

| Module | Responsibility | Key functions |
|--------|----------------|---------------|
| `config.py` | Default paths from env vars (`INV_DB_PATH`, `INV_OUTPUT_DIR`) | `default_db_path`, `default_output_dir` |
| `schema.py` | Every `CREATE TABLE / INDEX / VIEW` statement, as commented SQL | `ALL_STATEMENTS`, `TABLE_NAMES` |
| `db.py` | Connection (autocommit), schema creation, explicit transactions | `connect`, `init_db`, `transaction` |
| `reference.py` | Read and upsert the three reference files; raise on bad master data | `load_reference_files`, `fetch_reference_data` |
| `ingest.py` | Read a movement file: SHA-256, header contract, raw rows with line numbers | `read_movement_file`, `check_header`, `file_sha256` |
| `validate.py` | Pure row validation: every rule, every reject code, payload hash | `validate_row`, `quantity_error` |
| `load.py` | Duplicate detection, append to history, current vs. history-only, late flag, affected pairs | `load_rows` |
| `balances.py` | Dense daily balances: pure row builder, per-pair rebuild, incremental and full rebuild | `build_pair_rows`, `rebuild_pair`, `incremental_rebuild`, `full_rebuild` |
| `pipeline.py` | Orchestration of one run: RUNNING → SKIPPED / REJECTED / transaction → SUCCESS / FAILED | `run_file`, `run_input`, `run_load_reference` |
| `reconcile.py` | Independent SQL checks over the finished tables | `run_reconciliation`, `check_*` |
| `report.py` | Reconciliation report (Markdown + JSON) and CSV exports | `write_report` |
| `dashboard.py` | Read-only, offline, single-file HTML dashboard: static sections rendered and escaped in Python, interactive sections drawn by vanilla JS from safely embedded JSON | `write_dashboard`, `collect_data`, `safe_json` |
| `logs.py` | `key=value` log lines to console and `output/logs/pipeline.log` | `setup_logging`, `log_event` |
| `generator.py` | Seeded synthetic data with oracle labels | `generate` |
| `cli.py` | `python -m inventory_pipeline <command>` | `main` |

`scripts/benchmark.py` drives the generator and pipeline to measure performance.

## Dependencies between modules

```mermaid
flowchart LR
    cli --> pipeline --> load --> validate
    pipeline --> ingest
    pipeline --> reference --> validate
    pipeline --> balances
    cli --> reconcile
    cli --> report --> reconcile
    cli --> dashboard --> reconcile
    cli --> generator --> validate
    pipeline --> db --> schema
    load --> ingest
```

## Key design points (details in [design_notes.md](design_notes.md))

- **History is append-only.** `movement_event` is only ever inserted into. Corrections are new versions,
  and the views pick the highest version. Nothing is overwritten, so any past state can be explained.
- **One transaction per file.** A file either lands completely (events, rejects, balances, processed
  marker, run status) or not at all. The `RUNNING` row is committed *before* the transaction, so a
  crash is still visible.
- **Two layers of idempotency.** The file hash skips a byte-identical re-run. The `(movement_id, version)`
  key makes any re-sent row a duplicate, even inside a new file or with `--force`.
- **Balances are derived, never edited by hand.** They can always be rebuilt from history
  (`--full-rebuild`). The incremental path is an optimization, and a test proves it gives the same table.
- **Checks are independent.** `reconcile.py` recomputes in SQL (window functions) rather than calling
  the Python rebuild, so a bug in one shows up against the other.
