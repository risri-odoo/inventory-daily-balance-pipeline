# Initial prompt sent to Claude Code (session 1)

Sent: 2026-11-08T06:55:23Z (approximate, recorded by Claude Code at the start of the session)

The text below is the first message pasted into Claude Code, verbatim.

---

Claude Code prompt (revised): Daily Inventory Balance Pipeline
Before you paste: do these yourself (about 5 minutes)
Clone the repo: git clone https://github.com/risri-odoo/inventory-daily-balance-pipeline.git. Git will warn that it's an empty repository; that's expected.
Open Claude Code in that cloned folder. Check that pushing works: git push must be able to authenticate, for example through GitHub Desktop, Git Credential Manager, or gh auth login.
Paste everything below the line as your first message.
Keep a copy of the assignment text and of every prompt you send to any AI tool, including the planning chat you used before Claude Code. The submission rules require all prompts.
At the end of every Claude Code session, run /export and save the transcript into prompt_history/. If that command isn't available in your version, copy the conversation manually. The prompt below also makes Claude keep its own running log as a backup.
1. Context
I'm a data engineer (0–3 years of experience) doing a 72-hour take-home for YottaFlex AI. I must build a working, tested, documented data pipeline that turns inventory movement events (receipts, issues, transfers, adjustments) into daily inventory balances. It must reconcile movement totals and handle late-arriving and backdated corrections.

Repository: https://github.com/risri-odoo/inventory-daily-balance-pipeline. It's public and empty, with default branch main, and this folder is its clone.

The full assignment text is in docs/assignment.md. Create that file first and leave a placeholder; I will paste the text in.

How it will be judged. Use this to decide where effort goes:

Assignment weights: correctness and data quality 30%, design and code clarity 20%, testing and edge cases 20%, idempotency/recovery/observability 15%, performance reasoning 10%, docs and reproducibility 5%.
Submission rules (YottaFlex):
Code must run from clear install, configure and verify steps.
The work lives in a public GitHub repo whose meaningful commit history shows how it evolved. Commit timestamps are used to verify the 72-hour window.
Transparent AI use: all prompts are submitted, plus an honest note on where AI accelerated the work, where I relied on it heavily, and what I personally reviewed, changed or validated.
Visible reasoning: problem, assumptions, decisions, trade-offs, testing approach, limitations, and how I know it works.
It must be my individual work, and I must be able to explain it. I will be asked to walk reviewers through the solution.
Only free, locally runnable tools, with no heavy dependencies.
Partial is acceptable if it's stated honestly: what works, what doesn't, deviations, next steps.
No secrets, credentials, personal data, or code I can't publish.
2. Ground rules for working with me
I must understand and own this code. Keep it simple enough for a junior engineer to explain line by line: small typed functions, docstrings, no clever metaprogramming, no unnecessary abstractions. Prefer readable SQL and plain Python.
Work in phases (section 15). At the end of each phase:
run the tests,
give me a short plain-English explanation of what you built and why (5–10 bullets),
list anything I should personally check,
make a git commit with a clear message,
then stop and wait for me to say "continue".
Commits: one per meaningful step, using conventional style such as feat: add row validation with reject codes or test: add idempotency tests. Keep the Co-Authored-By trailer, because it supports honest AI disclosure. Never rewrite history, force-push, or squash.
Pushing: after each phase's commit(s), and only once tests pass, run git push origin main so that GitHub timestamps show steady progress through the 72-hour window. Before every commit, check git status so that nothing from .gitignore (databases, .venv, generated data) gets staged.
If a push fails (for example on authentication), stop and tell me. Don't try workarounds.
Prompt log: create CLAUDE.md containing this rule: "At the start of every turn, append the user's message verbatim, with an ISO timestamp, to prompt_history/claude_code_prompts.md. Do not edit earlier entries." Follow the rule from now on, starting with this message (record it as "initial prompt — see prompt_history/00_initial_prompt.md" and save this whole prompt to that file).
Never fake evidence. Every number in the docs must come from a real command you ran. If something fails or is skipped, say so.
Never write claims about me. In AI_USAGE.md, fill in only the facts you can see (what you generated, and which files). Leave clearly marked TODO (candidate): sections for what I personally reviewed, changed and validated. I will write those myself.
Public-repo hygiene:
no secrets, no .env with real values;
no absolute local paths or usernames in committed logs or evidence (store source_file as a path relative to the input root);
add a .gitignore covering .venv/, __pycache__/, *.db, output/, data/generated/ and .pytest_cache/;
commit only the small sample data and the generator, never the 100k generated files.
Ask me before deviating from this spec. Record every assumption and deviation in docs/assumptions_and_deviations.md.
3. Technical constraints
Python 3.10+, standard library only for the pipeline, with sqlite3 as storage and target, so there are zero services to set up.
The only third-party dependency is pytest (requirements.txt). Document the setup:
python -m venv .venv, activate it (Windows and Linux/macOS commands), then pip install -r requirements.txt;
include a fallback note for when venv can't install pip.
Must run on Windows and Linux: use pathlib, and no shell-only scripts are required.
Paths and parameters can be set with CLI arguments, with env-var defaults (INV_DB_PATH, INV_OUTPUT_DIR).
Use snake_case names, ISO-8601 dates (YYYY-MM-DD) and UTC timestamps (...Z), and an explicit run_id and source_file on every loaded or rejected row.
4. Source data contract
Reference files (reference/):

dim_sku.csv: sku (PK), description, uom
dim_location.csv: location_id (PK), location_name, location_type
opening_balance.csv: sku, location_id, as_of_date, quantity. PK is (sku, location_id). This is the on-hand quantity at the end of as_of_date.
Movement files (input/*.csv, one batch per file, processed in filename order). Columns are matched by header name:

column	required	rule
movement_id	yes	natural key from the source
version	no (default 1)	integer ≥ 1. A higher version is a correction of the same movement
movement_type	yes	RECEIPT, ISSUE, TRANSFER or ADJUSTMENT (case-insensitive)
sku	yes	must exist in dim_sku
location_id	yes	must exist in dim_location
to_location_id	TRANSFER only	must exist, must differ from location_id, must be blank for other types
quantity	yes	integer only, abs ≤ 1,000,000 (see sign rules)
business_date	yes	the effective date that drives the balance
event_ts	yes	when the source recorded it. If no timezone is given, assume UTC
reason_code, source_system	no	passed through
5. Business rules
Signs: RECEIPT is +qty. ISSUE is −qty. TRANSFER is −qty at location_id and +qty at to_location_id (two ledger legs). ADJUSTMENT is the signed qty as given.
Quantity: RECEIPT, ISSUE and TRANSFER need qty > 0. ADJUSTMENT needs qty ≠ 0. Exception: qty = 0 is allowed when version > 1, meaning a void/cancel.
Corrections: a movement's current state is its highest version ever received, regardless of arrival order. If v3 arrives before v2, v3 stays current and v2 goes to history only. Never overwrite history.
Closed period: reject movements dated on or before the opening_balance as_of_date for that pair. This applies to both legs of a transfer.
Future-dated: reject a row if business_date is later than the date of event_ts.
Duplicates: the key is (movement_id, version), checked against both the database and earlier rows in the same file.
Identical payload: DUPLICATE_EVENT (dropped, counted separately).
Different payload: CONFLICTING_DUPLICATE (rejected).
The first arrival wins.
Late data: a row is late if its business_date is before the max business_date already loaded. Flag it (is_late) and count it.
Negative balances are a data-quality warning, not a reject.
Reject codes (the first failure is the primary reason; store all failures in the details): MALFORMED_ROW, MISSING_REQUIRED_FIELD, INVALID_MOVEMENT_TYPE, INVALID_VERSION, INVALID_QUANTITY, INVALID_DATE, INVALID_TIMESTAMP, UNKNOWN_SKU, UNKNOWN_LOCATION, INVALID_TRANSFER, FUTURE_DATED, BEFORE_OPENING_SNAPSHOT, DUPLICATE_EVENT, CONFLICTING_DUPLICATE.
File-level rules:
A missing required column or a duplicate column name rejects the whole file (status REJECTED).
Extra columns are ignored, and the run records a schema warning.
A header-only or zero-byte file is a successful empty batch.
A row with the wrong field count is MALFORMED_ROW.
6. Target tables (SQLite)
dim_sku, dim_location, opening_balance: loaded with upserts, so reloading is a no-op. A changed opening balance triggers a full rebuild.
movement_event: append-only history, PK (movement_id, version), plus payload_hash, is_late, run_id, source_file, source_line, ingested_at.
v_movement_current (view): the latest version per movement_id.
v_movement_leg (view): signed ledger legs. A transfer produces a TRANSFER_OUT leg and a TRANSFER_IN leg.
quarantine_movement: run_id, source_file, source_line, movement_id, reject_reason, reject_details, raw_record. Every reject must trace back to its file and line.
daily_inventory_balance: PK (sku, location_id, balance_date), with opening_qty, receipt_qty, issue_qty, transfer_in_qty, transfer_out_qty, adjustment_qty, net_change_qty, closing_qty and run_id.
Dense grain: one row per day from the pair's start (as_of_date + 1, or its first movement date) up to the global horizon (the max business_date), carrying the balance forward.
pipeline_run: run_id, source_file, file_sha256, status (RUNNING/SUCCESS/SKIPPED/REJECTED/FAILED), started_at, ended_at, duration_ms, rows_read/accepted/rejected/duplicate/late/superseding, min_affected_date, pairs_rebuilt, balance_rows_written, negative_balance_rows, schema_warnings, stage_timings_ms, error_message.
processed_file: file_sha256 (PK), source_file, run_id. Only written on success.
7. Processing design
Insert a pipeline_run row with status RUNNING and commit it straight away, so failures stay visible.
If the file's SHA-256 is in processed_file and --force is not set, mark the run SKIPPED.
Check the header contract.
Inside one transaction: validate rows, insert accepted events and quarantine rows, recompute balances, mark the file processed and the run SUCCESS, then COMMIT. On any exception, ROLLBACK and mark the run FAILED with the error, so a retry is clean.
Incremental recompute: for each accepted event that becomes current, mark its (sku, location, business_date) legs as affected, plus the legs of the version it replaced. For each pair, recompute from the earliest affected date to the horizon. Also extend unaffected pairs when the horizon advances. Provide --full-rebuild, and a test proving incremental results equal a full rebuild.
Fault injection: --fail-at after_load|after_rebuild raises an error inside the transaction, to demonstrate partial failure and retry.
Log to the console and to output/logs/pipeline.log as key=value lines that include run_id.
8. CLI (python -m inventory_pipeline <command>)
init, load-reference --ref-dir, run --input <file|dir> [--force] [--full-rebuild] [--fail-at], reconcile, report --out, generate --rows --seed --out ..., and demo (a fresh DB → sample reference → sample input → reconcile → report). The exit code is non-zero on a FAILED/REJECTED run or a failed reconciliation check.

9. Reconciliation checks (PASS / FAIL / WARN)
Per run: rows_read = accepted + rejected + duplicate, and this matches the table counts for that run_id.
Independent recompute: a SQL window cumulative sum of leg deltas, plus the opening balance, equals closing_qty on every movement date.
Continuity: today's opening equals yesterday's closing, with no date gaps. The first row's opening equals the opening balance, or 0.
closing = opening + net_change on every row.
Component totals (receipts, issues, transfers in and out, adjustments) match the ledger.
Total transfer_in = total transfer_out.
Every pair ends at the global horizon.
One current version per movement_id, and no orphan sku or location values.
Every quarantine row has a source_file, a source_line and a raw_record.
Negative closing balances are reported as a WARN, with a count and examples.
report writes output/reconciliation_report.md and .json, plus CSV exports of balances, quarantine and the run log.

10. Data
A hand-made sample (data/sample/): 3 SKUs, 3 locations, opening balances for some pairs, and two batch files.

Batch 1: normal movements of all four types, plus one deliberately broken row each for: an exact duplicate, a non-numeric qty, a blank sku, an unknown sku, an extra field, a transfer to the same location, an invalid type, a row before the opening snapshot, and a future-dated row.
Batch 2:
next-day movements;
a late backdated issue;
a v2 correction of a batch-1 receipt;
a v3 void that arrives before its v2;
a new pair that goes negative;
an unknown location;
a conflicting duplicate;
a zero-qty v1 row;
an invalid date (2026-02-30).
Work out the expected closing balances by hand and show the arithmetic in data/sample/expected/README.md. Store the expected balances and expected rejects as CSVs in data/sample/expected/, separate from the input. Never derive them from the pipeline's own output.
Edge-case files: header-only, zero bytes, a missing required column, and extra plus reordered columns.
Generator: fixed seed, with configurable rows (default 100,000), SKUs, locations, days, batches and defect rates.

It tracks the true balance as it goes, so stock never goes negative from issues or transfers.
It injects late arrivals (in later batches), receipt corrections, exact and conflicting duplicates, and labelled invalid rows (one defect per row, with IDs like MV-BAD-…).
It writes labels/expected_rejects.csv and labels/expected_final_balances.csv outside the input folder, as an independent oracle for the tests.
11. Tests (pytest)
Validation unit tests: one per rule and reject code, including nulls and boundaries (1,000,000 passes, 1,000,001 fails, 1.5 fails, a negative receipt fails, a negative adjustment passes, 0 at v1 fails, 0 at v2 passes).
End to end on the sample: balances equal the hand-computed file, rejects equal the expected rejects, run counts are right, and all checks pass (the negative pair is a WARN).
Idempotency: rerunning the same file is SKIPPED with no new rows. --force turns every row into a duplicate and leaves balances unchanged. Incremental results equal a full rebuild.
Edge cases:
empty batch and zero-byte file;
missing column (REJECTED) and extra column (warning, still loads);
partial failure (FAILED, with nothing written) followed by a successful retry;
a late event recomputes past days;
out-of-order versions are handled independently of arrival order;
a correction that changes the location moves stock between pairs;
duplicates within one file are caught.
Generator:
the same seed gives byte-identical files;
on a small generated run, reject counts per reason equal the labels, and final balances equal the oracle.
12. Performance
scripts/benchmark.py generates 100k rows, loads every batch, and records the time per stage and rows per second. It then compares an incremental recompute against a full rebuild for one small late batch. Write the results to docs/benchmark.md with machine details (CPU and OS only, no username), the one optimization measured, and next steps such as DuckDB or Polars, or date partitioning.

13. Repository layout
README.md                         # start here
AI_USAGE.md                       # honest AI disclosure (candidate fills TODOs)
CLAUDE.md                         # working rules for Claude Code
requirements.txt  .gitignore
inventory_pipeline/               # the package (config, schema, db, reference, ingest,
                                  #   validate, balances, pipeline, reconcile, report, generator, cli)
tests/
scripts/benchmark.py
sql/sample_queries.sql
data/sample/{reference,input,edge_cases,expected}/
docs/
  assignment.md                   # original problem statement
  architecture.md                 # Mermaid flow + module responsibilities
  data_dictionary.md
  design_notes.md                 # decisions and trade-offs
  assumptions_and_deviations.md
  verification.md                 # "how I know it works"
  walkthrough.md                  # interview prep
  benchmark.md
  evidence/                       # real outputs: pytest, demo log, reconciliation report
prompt_history/
  README.md                       # which tools were used, how prompts were captured, gaps
  00_initial_prompt.md
  claude_code_prompts.md          # running log (rule in CLAUDE.md)
  (exported transcripts + earlier planning-chat prompts, added by me)
14. Documentation contents
README.md:
problem in two lines and a quick start (clone → venv → install → demo → pytest), with expected output;
an architecture diagram;
how to run each command, and the rerun/retry/backfill behaviour;
the output tables;
links to the docs;
known limitations and next steps;
a short "AI usage" paragraph linking to AI_USAGE.md and prompt_history/.
docs/verification.md: every acceptance criterion from the assignment mapped to the test or reconciliation check that proves it, with links to the real evidence files.
docs/design_notes.md: decisions and trade-offs, e.g.:
SQLite vs. Spark or DuckDB;
a dense vs. sparse daily grid;
version-based corrections vs. reversal entries;
incremental vs. full recompute;
why negative stock is a warning;
file-hash idempotency vs. key-based idempotency (and why both).
docs/walkthrough.md: for my interview.
the end-to-end flow in simple words;
one sample record traced from CSV to balance;
how a backdated correction flows through;
the 10 most likely reviewer questions, with short answers;
which parts are most important to understand.
AI_USAGE.md:
tools used;
a table of which files or parts were AI-generated;
where AI accelerated the work;
where I relied on it heavily;
TODO (candidate): sections for what I reviewed, changed and validated, and for disagreements or bugs I caught.
prompt_history/README.md: lists every AI tool used, how prompts were captured, and any limitation (for example, a tool that can't export).
15. Phases (stop after each, as described in rule 2)
Scaffold (this is the first commit and push): repo layout, .gitignore, requirements.txt, CLAUDE.md, prompt_history/ (save this prompt), docs/assignment.md placeholder, a short README stub (title, one-line purpose, "work in progress"), schema and DB modules, an init command, and one smoke test that init creates every table. Commit as chore: project scaffold, schema and init command, push, then stop.
Ingest and validate: file reading, header contract, row validation, reject codes, plus validation unit tests.
Load and balances: event loading, duplicates, versions and corrections, the incremental and full rebuild, and the run log, idempotency and transaction with fault injection.
Reconcile, report and CLI: all checks, the report and exports, and the demo command.
Sample data and tests: the hand-made sample with the arithmetic shown, edge-case files, and the end-to-end, idempotency and edge-case tests.
Generator and benchmark: the oracle labels, generator tests, and a benchmark run with real numbers.
Docs and evidence:
all docs in section 14;
run pytest -v and demo from a fresh clone in a temp folder, then save the outputs to docs/evidence/;
check that nothing personal or secret is committed (grep for absolute paths, usernames and emails).
16. Definition of done
 pytest -q is all green, and the output is saved in docs/evidence/.
 python -m inventory_pipeline demo works from a fresh clone following only the README.
 Every reconciliation check is PASS (or an explained WARN) on both the sample and the 100k run.
 Every assignment acceptance criterion is mapped in docs/verification.md.
 The commit history shows the phases clearly.
 prompt_history/ and AI_USAGE.md are present, and I have filled in the candidate TODOs.
 No secrets, personal data, absolute paths or generated bulk data are in the repo.
 A final summary for me: what was built, what's incomplete, known limitations, and what I should review most carefully before submitting.
