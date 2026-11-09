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

## 2026-11-08T12:01:09Z

can u investigate what all parts are left from here ?? -->

1. Complete the prompt history, from the Guidelines (point 03) and FAQ Q6: “Include all prompts submitted to every AI tool you used.”

Copy 01_planning_and_review_chat.md (this chat’s prompts) into your repo’s prompt_history/ folder.
In Claude Code, run /export and save the file into prompt_history/.
Fill in the TODO (candidate) in prompt_history/README.md: list both tools (this Claude desktop chat for planning and review, Claude Code for the build) and what each was used for.

2. Fill in your sections of AI_USAGE.md, from the Guidelines (point 03): “explain where AI accelerated your work, where you relied heavily on it, and what you personally reviewed, changed, or validated.”

There are 6 TODO (candidate) sections. Write them in your own words, honestly. For example, say that a separate Claude chat did the deep reviews, and that you asked for the A10 timezone fix.
Right now those sections are empty placeholders, and a reviewer would notice straight away.

3. Commit and push those changes yourself, from the Guidelines (point 02), “meaningful commits”. Use a message like docs: add AI usage disclosure and prompt history. This also puts commits you made into the history.

Strongly recommended (it’s about the interview)

4. The 5-minute paper check. Recompute SKU-001 @ WH-01 by hand (100 → 126 → 101 → 91). Then you can truthfully write “I verified it” in AI_USAGE.md, so do this before step 2.

5. Read docs/walkthrough.md and practise the 10 questions. The Guidelines say: “Be ready to walk us through your solution.”

Then, only if time allows

6. Phase 8, the dashboard. It’s optional and not graded, and it’s nice for the demo.

Before submitting

7. Check the deadline (72 hours from when they sent the project), and send the link from your recruitment email (FAQ Q3).

The order I’d go in: 4 → 1 → 2 → 3, then 5, then the dashboard if you have time, then submit. Steps 1 to 4 take about an hour.

## 2026-11-08T12:08:40Z

give a project structure and also the details of all the phases ??

## 2026-11-09T09:37:05Z

redo evidence
