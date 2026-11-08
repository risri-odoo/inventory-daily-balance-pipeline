# Claude Code prompt log

Running log of every message sent to Claude Code, appended at the start of each turn
(rule in `CLAUDE.md`). Earlier entries are never edited.

---

## 2026-11-08T06:55:23Z

initial prompt — see prompt_history/00_initial_prompt.md

## 2026-11-08T07:12:18Z

continue

## 2026-11-08T07:23:38Z

Before Phase 3, one fix to A10: compare business_date with the calendar date as written in event_ts (its local date, before converting to UTC), not the UTC date. Example: 2026-02-04T01:00:00+05:30 with business_date 2026-02-04 is valid, but the current code rejects it as FUTURE_DATED because the UTC date is 2026-02-03. Update A10, add that test case, run tests, commit as fix: compare future-dated rule against local event date, push, then continue with Phase 3.

## 2026-11-08T07:51:00Z

continue

## 2026-11-08T09:31:31Z

continue

## 2026-11-08T09:42:11Z

continue

## 2026-11-08T10:51:39Z

continue

## 2026-11-08T11:49:48Z

Phase 8 (optional, additive): a read-only HTML dashboard. Do not change pipeline behaviour.

Goal: help a reviewer SEE the results during a walkthrough. Standard library only, no new dependencies, no server, no CDN or external URLs (it must work offline).

Build:
- `python -m inventory_pipeline dashboard [--db ...] [--out output/dashboard.html]` writes ONE self-contained HTML file (inline CSS and vanilla JS, data embedded as JSON). `demo` also writes it.
- Sections:
  1. Header: overall reconciliation status, row counts, horizon date.
  2. Reconciliation: one card per check (PASS/FAIL/WARN, details, example rows).
  3. Run log: every pipeline_run in insertion order, with status, counts, late, superseding, duration and error.
  4. Rejects: counts by reason, plus a filterable quarantine table (file, line, movement_id, reason, raw_record).
  5. Balance explorer: choose sku + location → a simple inline-SVG line chart of closing_qty over time, plus the daily table (opening, receipt, issue, transfer in/out, adjustment, closing). Negative days are highlighted.
  6. Movement history: type a movement_id → every version, with the current one marked (shows corrections and out-of-order versions).
- Keep it simple and readable: one module `inventory_pipeline/dashboard.py`, plus an HTML template as a string or package file. Small functions, docstrings.
- Security: HTML-escape every value from the data (raw_record can contain anything). Embed JSON safely (escape "</").

Tests (`tests/test_dashboard.py`):
- builds from the demo DB, and the file contains the expected totals (e.g. the 189 final on-hand and the 13 rejects);
- the file contains no http:// or https:// references;
- a raw_record containing `<script>` is escaped;
- it works on an empty database.

Docs: a README section with how to open it, an entry in docs/architecture.md, and a line in AI_USAGE.md's generated-files table. Record any assumptions.

Same rules as before: run the tests, explain in plain English, commit as `feat: add read-only HTML dashboard`, push only if pytest exits 0, then stop.

 ??
