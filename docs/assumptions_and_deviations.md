# Assumptions and deviations

Every assumption made where the spec was silent, and every deviation from the spec, is recorded here.

## Assumptions

| # | Area | Assumption | Why |
|---|------|------------|-----|
| A1 | Balance components | In `daily_inventory_balance`, `issue_qty` and `transfer_out_qty` are stored as positive magnitudes; `adjustment_qty` is signed. So `net_change_qty = receipt_qty - issue_qty + transfer_in_qty - transfer_out_qty + adjustment_qty`. | Positive magnitudes read naturally in reports ("issued 5"); adjustments can go either way so they keep their sign. |
| A2 | Default paths | `INV_DB_PATH` defaults to `output/inventory.db` and `INV_OUTPUT_DIR` to `output`, both relative to the current directory. | Keeps all generated artefacts in one git-ignored folder. |
| A3 | Foreign keys | The tables do not declare SQLite foreign keys. Validation rejects unknown SKUs and locations, and a reconciliation check confirms there are no orphans. | Keeps loads simple and makes the orphan check an explicit, visible data-quality control. |

## Deviations

None so far.
