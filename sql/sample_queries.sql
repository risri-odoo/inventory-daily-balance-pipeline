-- Sample queries against the pipeline database.
-- Run after `python -m inventory_pipeline demo`: open output/demo.db in any SQLite tool
-- (for example the sqlite3 shell or DB Browser for SQLite) and run the queries one at a time.

-- 1. Closing stock per SKU and location on the latest day (the global horizon).
SELECT sku, location_id, balance_date, closing_qty
FROM daily_inventory_balance
WHERE balance_date = (SELECT MAX(balance_date) FROM daily_inventory_balance)
ORDER BY sku, location_id;

-- 2. One pair's daily history, with every movement component.
SELECT balance_date, opening_qty, receipt_qty, issue_qty, transfer_in_qty, transfer_out_qty,
       adjustment_qty, net_change_qty, closing_qty
FROM daily_inventory_balance
WHERE sku = 'SKU-001' AND location_id = 'WH-01'
ORDER BY balance_date;

-- 3. Source-to-target reconciliation per run: rows read vs. rows that landed in each table.
SELECT r.run_id, r.source_file, r.status, r.rows_read,
       (SELECT COUNT(*) FROM movement_event e WHERE e.run_id = r.run_id) AS events_loaded,
       (SELECT COUNT(*) FROM quarantine_movement q WHERE q.run_id = r.run_id
          AND q.reject_reason <> 'DUPLICATE_EVENT') AS rejected,
       (SELECT COUNT(*) FROM quarantine_movement q WHERE q.run_id = r.run_id
          AND q.reject_reason = 'DUPLICATE_EVENT') AS duplicates
FROM pipeline_run AS r
ORDER BY r.rowid;

-- 4. Rejected rows by reason, with where they came from.
SELECT reject_reason, COUNT(*) AS row_count, GROUP_CONCAT(source_file || ':' || source_line, ', ') AS locations
FROM quarantine_movement
GROUP BY reject_reason
ORDER BY row_count DESC, reject_reason;

-- 5. Full trace of one rejected row: the original line and every rule it broke.
SELECT run_id, source_file, source_line, movement_id, reject_reason, reject_details, raw_record
FROM quarantine_movement
WHERE movement_id = 'MV-1006';

-- 6. Version history of a corrected movement, and which version is current.
SELECT e.movement_id, e.version, e.quantity, e.business_date, e.is_late, e.source_file, e.source_line,
       CASE WHEN c.version IS NOT NULL THEN 'current' ELSE 'history' END AS state
FROM movement_event AS e
LEFT JOIN v_movement_current AS c ON c.movement_id = e.movement_id AND c.version = e.version
WHERE e.movement_id IN ('MV-1001', 'MV-1005')
ORDER BY e.movement_id, e.version;

-- 7. Late-arriving rows (business date before what had already been loaded).
SELECT movement_id, version, movement_type, sku, location_id, business_date, source_file
FROM movement_event
WHERE is_late = 1
ORDER BY business_date, movement_id;

-- 8. Signed ledger legs (a transfer is two legs) and their total per leg type.
SELECT leg_type, COUNT(*) AS legs, SUM(qty_delta) AS total_delta
FROM v_movement_leg
GROUP BY leg_type
ORDER BY leg_type;

-- 9. Data-quality warning: negative closing balances.
SELECT sku, location_id, balance_date, closing_qty
FROM daily_inventory_balance
WHERE closing_qty < 0
ORDER BY sku, location_id, balance_date;

-- 10. Total on hand on the latest day = openings + receipts - issues + adjustments
--     (transfers cancel out across locations).
SELECT
    (SELECT SUM(closing_qty) FROM daily_inventory_balance
      WHERE balance_date = (SELECT MAX(balance_date) FROM daily_inventory_balance)) AS on_hand_latest_day,
    (SELECT SUM(quantity) FROM opening_balance)
      + (SELECT COALESCE(SUM(receipt_qty - issue_qty + adjustment_qty), 0) FROM daily_inventory_balance) AS from_components;
