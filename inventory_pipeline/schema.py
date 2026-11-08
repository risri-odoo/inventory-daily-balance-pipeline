"""SQLite schema: every table and view the pipeline uses.

Tables and indexes use IF NOT EXISTS and views are dropped and recreated, so running
`init` twice is safe and never touches data.

Conventions:
- dates are TEXT 'YYYY-MM-DD'; timestamps are TEXT UTC 'YYYY-MM-DDTHH:MM:SSZ'
- every loaded or rejected row carries run_id and source_file (relative to the input root)
"""

# ---------------------------------------------------------------------------
# Reference data (loaded with upserts, so reloading the same files is a no-op)
# ---------------------------------------------------------------------------

DIM_SKU = """
CREATE TABLE IF NOT EXISTS dim_sku (
    sku          TEXT PRIMARY KEY,
    description  TEXT,
    uom          TEXT,
    loaded_at    TEXT NOT NULL
);
"""

DIM_LOCATION = """
CREATE TABLE IF NOT EXISTS dim_location (
    location_id    TEXT PRIMARY KEY,
    location_name  TEXT,
    location_type  TEXT,
    loaded_at      TEXT NOT NULL
);
"""

# quantity = on-hand at the END of as_of_date. Movements dated on or before as_of_date
# are rejected (closed period).
OPENING_BALANCE = """
CREATE TABLE IF NOT EXISTS opening_balance (
    sku          TEXT NOT NULL,
    location_id  TEXT NOT NULL,
    as_of_date   TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    loaded_at    TEXT NOT NULL,
    PRIMARY KEY (sku, location_id)
);
"""

# ---------------------------------------------------------------------------
# Movement history (append-only: rows are inserted, never updated or deleted)
# ---------------------------------------------------------------------------

MOVEMENT_EVENT = """
CREATE TABLE IF NOT EXISTS movement_event (
    movement_id     TEXT NOT NULL,
    version         INTEGER NOT NULL,
    movement_type   TEXT NOT NULL,      -- RECEIPT / ISSUE / TRANSFER / ADJUSTMENT (upper case)
    sku             TEXT NOT NULL,
    location_id     TEXT NOT NULL,
    to_location_id  TEXT,               -- only for TRANSFER
    quantity        INTEGER NOT NULL,   -- as received (sign rules applied in v_movement_leg)
    business_date   TEXT NOT NULL,      -- effective date that drives the balance
    event_ts        TEXT NOT NULL,      -- normalised to UTC 'Z'
    reason_code     TEXT,
    source_system   TEXT,
    payload_hash    TEXT NOT NULL,      -- SHA-256 of the business fields, for duplicate checks
    is_late         INTEGER NOT NULL DEFAULT 0,
    run_id          TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    source_line     INTEGER NOT NULL,
    ingested_at     TEXT NOT NULL,
    PRIMARY KEY (movement_id, version)
);
"""

MOVEMENT_EVENT_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_movement_event_run ON movement_event (run_id);
CREATE INDEX IF NOT EXISTS ix_movement_event_date ON movement_event (business_date);
-- Let the incremental rebuild read only the affected (sku, location) pairs, for both
-- the source leg (location_id) and the destination leg of a transfer (to_location_id).
CREATE INDEX IF NOT EXISTS ix_movement_event_pair ON movement_event (sku, location_id);
CREATE INDEX IF NOT EXISTS ix_movement_event_to_pair ON movement_event (sku, to_location_id);
"""

# The current state of a movement is its highest version ever received,
# regardless of the order in which versions arrived: a row is current when no row
# with the same movement_id has a higher version. Each check is one primary-key lookup,
# and SQLite can push a filter on sku/location into the view (used by the incremental rebuild).
# Views hold no data, so they are dropped and recreated on every init to stay up to date.
V_MOVEMENT_CURRENT = """
DROP VIEW IF EXISTS v_movement_current;
CREATE VIEW v_movement_current AS
SELECT e.*
FROM movement_event AS e
WHERE NOT EXISTS (
    SELECT 1
    FROM movement_event AS newer
    WHERE newer.movement_id = e.movement_id
      AND newer.version > e.version
);
"""

# Signed ledger legs built from the current versions only.
# A TRANSFER becomes two legs: TRANSFER_OUT (-qty at location_id)
# and TRANSFER_IN (+qty at to_location_id).
V_MOVEMENT_LEG = """
DROP VIEW IF EXISTS v_movement_leg;
CREATE VIEW v_movement_leg AS
SELECT
    movement_id,
    version,
    CASE movement_type WHEN 'TRANSFER' THEN 'TRANSFER_OUT' ELSE movement_type END AS leg_type,
    sku,
    location_id,
    business_date,
    CASE movement_type
        WHEN 'RECEIPT'    THEN quantity
        WHEN 'ISSUE'      THEN -quantity
        WHEN 'TRANSFER'   THEN -quantity
        WHEN 'ADJUSTMENT' THEN quantity
    END AS qty_delta
FROM v_movement_current
UNION ALL
SELECT
    movement_id,
    version,
    'TRANSFER_IN' AS leg_type,
    sku,
    to_location_id AS location_id,
    business_date,
    quantity AS qty_delta
FROM v_movement_current
WHERE movement_type = 'TRANSFER';
"""

# ---------------------------------------------------------------------------
# Rejected rows: every reject traces back to its file and line
# ---------------------------------------------------------------------------

QUARANTINE_MOVEMENT = """
CREATE TABLE IF NOT EXISTS quarantine_movement (
    quarantine_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    source_line     INTEGER NOT NULL,
    movement_id     TEXT,               -- may be blank if the row was too broken to read it
    reject_reason   TEXT NOT NULL,      -- primary (first) reject code
    reject_details  TEXT NOT NULL,      -- JSON list of every failure found
    raw_record      TEXT NOT NULL,      -- the original CSV line
    quarantined_at  TEXT NOT NULL
);
"""

QUARANTINE_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_quarantine_run ON quarantine_movement (run_id);
"""

# ---------------------------------------------------------------------------
# Output: dense daily balances (one row per pair per day up to the global horizon)
# ---------------------------------------------------------------------------

# issue_qty and transfer_out_qty are positive magnitudes; adjustment_qty is signed.
# net_change_qty = receipt - issue + transfer_in - transfer_out + adjustment
# closing_qty    = opening_qty + net_change_qty
DAILY_INVENTORY_BALANCE = """
CREATE TABLE IF NOT EXISTS daily_inventory_balance (
    sku               TEXT NOT NULL,
    location_id       TEXT NOT NULL,
    balance_date      TEXT NOT NULL,
    opening_qty       INTEGER NOT NULL,
    receipt_qty       INTEGER NOT NULL,
    issue_qty         INTEGER NOT NULL,
    transfer_in_qty   INTEGER NOT NULL,
    transfer_out_qty  INTEGER NOT NULL,
    adjustment_qty    INTEGER NOT NULL,
    net_change_qty    INTEGER NOT NULL,
    closing_qty       INTEGER NOT NULL,
    run_id            TEXT NOT NULL,
    PRIMARY KEY (sku, location_id, balance_date)
);
"""

# ---------------------------------------------------------------------------
# Observability and idempotency
# ---------------------------------------------------------------------------

PIPELINE_RUN = """
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id                 TEXT PRIMARY KEY,
    source_file            TEXT,
    file_sha256            TEXT,
    status                 TEXT NOT NULL,   -- RUNNING / SUCCESS / SKIPPED / REJECTED / FAILED
    started_at             TEXT NOT NULL,
    ended_at               TEXT,
    duration_ms            INTEGER,
    rows_read              INTEGER NOT NULL DEFAULT 0,
    rows_accepted          INTEGER NOT NULL DEFAULT 0,
    rows_rejected          INTEGER NOT NULL DEFAULT 0,
    rows_duplicate         INTEGER NOT NULL DEFAULT 0,
    rows_late              INTEGER NOT NULL DEFAULT 0,
    rows_superseding       INTEGER NOT NULL DEFAULT 0,
    min_affected_date      TEXT,
    pairs_rebuilt          INTEGER NOT NULL DEFAULT 0,
    balance_rows_written   INTEGER NOT NULL DEFAULT 0,
    negative_balance_rows  INTEGER NOT NULL DEFAULT 0,
    schema_warnings        TEXT,            -- JSON list
    stage_timings_ms       TEXT,            -- JSON object {stage: ms}
    error_message          TEXT
);
"""

# Written only when a file is processed successfully; used to skip re-runs of the same file.
PROCESSED_FILE = """
CREATE TABLE IF NOT EXISTS processed_file (
    file_sha256   TEXT PRIMARY KEY,
    source_file   TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    processed_at  TEXT NOT NULL
);
"""

TABLE_NAMES = [
    "dim_sku",
    "dim_location",
    "opening_balance",
    "movement_event",
    "quarantine_movement",
    "daily_inventory_balance",
    "pipeline_run",
    "processed_file",
]

VIEW_NAMES = [
    "v_movement_current",
    "v_movement_leg",
]

# Order matters: views must be created after the tables they read from.
ALL_STATEMENTS = [
    DIM_SKU,
    DIM_LOCATION,
    OPENING_BALANCE,
    MOVEMENT_EVENT,
    MOVEMENT_EVENT_INDEXES,
    V_MOVEMENT_CURRENT,
    V_MOVEMENT_LEG,
    QUARANTINE_MOVEMENT,
    QUARANTINE_INDEXES,
    DAILY_INVENTORY_BALANCE,
    PIPELINE_RUN,
    PROCESSED_FILE,
]
