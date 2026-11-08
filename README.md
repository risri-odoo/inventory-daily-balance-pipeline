# Inventory Daily Balance Pipeline

Turns inventory movement events (receipts, issues, transfers, adjustments) into reconciled
daily inventory balances, handling late-arriving and backdated corrections.

> **Work in progress.** Built in phases during a 72-hour take-home; see the commit history.

## Current state

- `python -m inventory_pipeline init` creates the SQLite database with every table and view.
- `pytest -q` runs the test suite.
