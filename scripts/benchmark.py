"""Benchmark: generate 100k rows, load every batch, time each stage, compare incremental vs full rebuild.

Usage (from the repository root):
    python scripts/benchmark.py                 # 100,000 rows, writes docs/benchmark.md
    python scripts/benchmark.py --rows 20000    # quicker run

Writes docs/benchmark.md and docs/evidence/benchmark_results.json. Generated data goes to
data/generated/benchmark/ and the database to output/benchmark/ (both git-ignored).
Only CPU and OS details are recorded about the machine (no user or host names).
"""

import argparse
import csv
import json
import platform
import shutil
import sqlite3
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))   # so `inventory_pipeline` imports without installing the package

from inventory_pipeline import db, generator, pipeline, reconcile  # noqa: E402

LATE_HEADER = ",".join(generator.HEADER)


def cpu_name() -> str:
    """A readable CPU model name, using only the standard library."""
    if sys.platform.startswith("win"):
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine() or "unknown"


def machine_info() -> dict[str, str]:
    """CPU and OS details only."""
    import os
    return {
        "cpu": cpu_name(),
        "logical_cpus": str(os.cpu_count()),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def compare_with_oracle(conn: sqlite3.Connection, labels: Path) -> dict[str, object]:
    """Compare rejects, batch counts and final balances with the generator's labels."""
    expected_rejects = Counter(r["reject_reason"] for r in read_csv(labels / "expected_rejects.csv"))
    actual_rejects = Counter(dict(conn.execute("SELECT reject_reason, COUNT(*) FROM quarantine_movement GROUP BY reject_reason").fetchall()))

    expected_final = {(r["sku"], r["location_id"]): int(r["closing_qty"]) for r in read_csv(labels / "expected_final_balances.csv")}
    horizon = read_csv(labels / "expected_final_balances.csv")[0]["balance_date"]
    actual_final = {(s, l): q for s, l, q in conn.execute(
        "SELECT sku, location_id, closing_qty FROM daily_inventory_balance WHERE balance_date = ?", (horizon,))}
    mismatched_pairs = sum(1 for pair in set(expected_final) | set(actual_final) if expected_final.get(pair) != actual_final.get(pair))

    expected_batches = read_csv(labels / "expected_batch_counts.csv")
    actual_batches = [
        {k: str(row[k]) for k in expected_batches[0]}
        for row in conn.execute(
            "SELECT source_file, rows_read, rows_accepted, rows_rejected, rows_duplicate, rows_late "
            "FROM pipeline_run WHERE source_file LIKE 'batch_%' AND status = 'SUCCESS' ORDER BY source_file")
    ]
    return {
        "rejects_by_reason_match": expected_rejects == actual_rejects,
        "rejects_by_reason": dict(sorted(actual_rejects.items())),
        "final_balance_pairs": len(expected_final),
        "final_balance_mismatched_pairs": mismatched_pairs,
        "batch_counts_match": expected_batches == actual_batches,
    }


def write_late_batch(path: Path, conn: sqlite3.Connection, rows: int, business_date: str) -> None:
    """A small late batch: `rows` receipts dated `business_date`, spread over existing pairs."""
    pairs = conn.execute(
        "SELECT DISTINCT sku, location_id FROM daily_inventory_balance ORDER BY sku, location_id").fetchall()
    step = max(1, len(pairs) // rows)
    lines = [LATE_HEADER]
    for i in range(rows):
        sku, location = pairs[(i * step) % len(pairs)]
        lines.append(f"MV-LATE-{i + 1:05d},1,RECEIPT,{sku},{location},,5,{business_date},{business_date}T12:00:00Z,LATE,BENCH")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def time_late_batch(base_db: Path, late_file: Path, work_dir: Path, full_rebuild: bool, repeats: int) -> dict[str, object]:
    """Load the late batch into fresh copies of the database; return median timings."""
    rebuild_ms, total_ms = [], []
    result = None
    balances_snapshot = None
    for attempt in range(repeats):
        copy_path = work_dir / f"late_{'full' if full_rebuild else 'incr'}_{attempt}.db"
        shutil.copyfile(base_db, copy_path)
        conn = db.connect(copy_path)
        result = pipeline.run_file(conn, late_file, late_file.parent, full_rebuild=full_rebuild)
        rebuild_ms.append(result.stage_timings_ms["rebuild"])
        total_ms.append(result.duration_ms or 0)
        balances_snapshot = conn.execute(
            "SELECT sku, location_id, balance_date, opening_qty, receipt_qty, issue_qty, transfer_in_qty, "
            "transfer_out_qty, adjustment_qty, net_change_qty, closing_qty FROM daily_inventory_balance "
            "ORDER BY sku, location_id, balance_date").fetchall()
        conn.close()
    assert result is not None
    return {
        "mode": "full rebuild" if full_rebuild else "incremental",
        "rebuild_ms_median": statistics.median(rebuild_ms),
        "total_ms_median": statistics.median(total_ms),
        "rebuild_ms_all": rebuild_ms,
        "pairs_rebuilt": result.pairs_rebuilt,
        "balance_rows_written": result.balance_rows_written,
        "min_affected_date": result.min_affected_date,
        "balances": balances_snapshot,
    }


def render_markdown(r: dict) -> str:
    """docs/benchmark.md, filled with the measured numbers."""
    m, g, load, inc, full = r["machine"], r["generator"], r["load"], r["incremental"], r["full"]
    speedup_rebuild = full["rebuild_ms_median"] / max(inc["rebuild_ms_median"], 1)
    speedup_total = full["total_ms_median"] / max(inc["total_ms_median"], 1)
    stage = load["stage_totals_ms"]
    stage_sum = sum(stage.values()) or 1
    lines = [
        "# Benchmark",
        "",
        f"_Generated by `python scripts/benchmark.py --rows {r['config']['rows']} --seed {r['config']['seed']}` "
        f"on {r['finished_at']}. Every number below comes from that run; raw results are in "
        "[evidence/benchmark_results.json](evidence/benchmark_results.json)._",
        "",
        "## Machine",
        "",
        "| item | value |",
        "|---|---|",
        f"| CPU | {m['cpu']} ({m['logical_cpus']} logical CPUs) |",
        f"| OS | {m['os']} |",
        f"| Python | {m['python']} |",
        f"| SQLite | {m['sqlite']} |",
        "",
        "## Dataset",
        "",
        f"Generated with seed {r['config']['seed']}: **{g['rows_written']:,} rows** in {g['batch_files']} batch files, "
        f"{r['config']['skus']} SKUs x {r['config']['locations']} locations, {r['config']['days']} days "
        f"(horizon {g['horizon']}). Includes {g['corrections']:,} corrections, {g['duplicates']:,} exact duplicates, "
        f"{g['conflicts']:,} conflicting duplicates, {g['invalid']:,} invalid rows and {g['late']:,} late rows. "
        f"Generation took {g['seconds']:.2f} s.",
        "",
        "## Load: every batch, incremental recompute",
        "",
        "| batch | rows | accepted | rejected | duplicate | late | read ms | validate+load ms | rebuild ms | total ms | rows/s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for b in load["batches"]:
        rate = b["rows_read"] / max(b["duration_ms"], 1) * 1000
        lines.append(
            f"| {b['source_file']} | {b['rows_read']:,} | {b['rows_accepted']:,} | {b['rows_rejected']:,} | "
            f"{b['rows_duplicate']:,} | {b['rows_late']:,} | {b['stage_timings_ms'].get('read', 0)} | "
            f"{b['stage_timings_ms'].get('validate_load', 0)} | {b['stage_timings_ms'].get('rebuild', 0)} | "
            f"{b['duration_ms']} | {rate:,.0f} |")
    lines += [
        "",
        f"**Total: {load['rows']:,} rows in {load['seconds']:.2f} s = {load['rows_per_second']:,.0f} rows/s** "
        f"(reference load {load['reference_ms']} ms). The balance table holds {load['balance_rows']:,} rows "
        f"for {load['pairs']:,} pairs.",
        "",
        "Time per stage, summed over all batches:",
        "",
        "| stage | ms | share |",
        "|---|---:|---:|",
    ]
    for name, value in stage.items():
        lines.append(f"| {name} | {value:,} | {value / stage_sum:.0%} |")
    lines += [
        "",
        "## Correctness at scale",
        "",
        f"- Reconciliation ({r['reconcile']['seconds']:.2f} s): overall **{r['reconcile']['overall_status']}**. "
        + ", ".join(f"{name} {status}" for name, status in r["reconcile"]["checks"].items()) + ".",
        f"- Rejects by reason equal the generator labels: **{r['oracle']['rejects_by_reason_match']}** "
        f"({', '.join(f'{k} {v:,}' for k, v in r['oracle']['rejects_by_reason'].items())}).",
        f"- Per-batch counts (read / accepted / rejected / duplicate / late) equal the labels: **{r['oracle']['batch_counts_match']}**.",
        f"- Final balances equal the oracle: **{r['oracle']['final_balance_mismatched_pairs']} mismatched pairs** "
        f"out of {r['oracle']['final_balance_pairs']:,}.",
        "",
        "## Optimization measured: incremental recompute vs. full rebuild",
        "",
        f"After all batches were loaded, one small late batch ({r['late_rows']} receipts dated "
        f"{inc['min_affected_date']}, {r['late_days_before_horizon']} days before the horizon) was loaded into fresh "
        f"copies of the database, {r['repeats']} times per mode. Medians:",
        "",
        "| mode | pairs rebuilt | balance rows written | rebuild stage ms | whole run ms |",
        "|---|---:|---:|---:|---:|",
        f"| incremental (default) | {inc['pairs_rebuilt']:,} | {inc['balance_rows_written']:,} | {inc['rebuild_ms_median']:,} | {inc['total_ms_median']:,} |",
        f"| `--full-rebuild` | {full['pairs_rebuilt']:,} | {full['balance_rows_written']:,} | {full['rebuild_ms_median']:,} | {full['total_ms_median']:,} |",
        "",
        f"The incremental rebuild stage was **{speedup_rebuild:.1f}x faster** ({speedup_total:.1f}x for the whole run) "
        f"and both modes produced identical balance tables: **{r['incremental_equals_full']}**.",
        "",
        "Why: a full rebuild deletes and rewrites every row of every pair. The incremental rebuild only rewrites "
        "the affected pairs from their earliest affected date to the horizon (plus extending other pairs if the "
        "horizon moves), keeping all earlier days. Both still read the ledger with one aggregate query over "
        "`v_movement_leg`, so that part of the cost is the same in both modes.",
        "",
        "## Where the time goes and next steps",
        "",
        "- **Validate + load** is row-by-row Python: each row runs the validation rules and two primary-key lookups "
        "(duplicate check and current version) before its insert. That is simple to explain and fast enough here. "
        "Next step: look keys up in bulk (one query per batch into a dict) and insert with `executemany`.",
        "- **Rebuild** reads the whole current ledger once per batch, even in incremental mode. Next step: filter "
        "`v_movement_leg` to the affected pairs and dates, or keep a materialised current-version table.",
        "- **Bigger data**: the same SQL runs on DuckDB (columnar, parallel) with few changes, and Polars could do "
        "the dense-grid build as vectorised window operations. For years of history, partition balances by month "
        "so a backdated correction only rewrites the partitions from its date onwards.",
        "- SQLite is single-writer, so batches are processed one at a time. That matches the ordering rules "
        "(filename order, stop at the first failure) but caps throughput at one core.",
        "",
        "## Reproduce",
        "",
        "```",
        f"python scripts/benchmark.py --rows {r['config']['rows']} --seed {r['config']['seed']}",
        "```",
        "",
        "Timings vary between runs and machines; the counts and correctness results do not.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the pipeline on generated data.")
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--late-rows", type=int, default=50, help="Rows in the small late batch")
    parser.add_argument("--late-days", type=int, default=5, help="How many days before the horizon the late batch is dated")
    parser.add_argument("--repeats", type=int, default=3, help="Repeats per mode for the late-batch comparison")
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data" / "generated" / "benchmark")
    parser.add_argument("--work-dir", type=Path, default=REPO_ROOT / "output" / "benchmark")
    parser.add_argument("--doc", type=Path, default=REPO_ROOT / "docs" / "benchmark.md")
    parser.add_argument("--json", type=Path, default=REPO_ROOT / "docs" / "evidence" / "benchmark_results.json")
    args = parser.parse_args()

    config = generator.GeneratorConfig(rows=args.rows, seed=args.seed)
    shutil.rmtree(args.data_dir, ignore_errors=True)
    shutil.rmtree(args.work_dir, ignore_errors=True)
    args.work_dir.mkdir(parents=True)

    print(f"generating {args.rows:,} rows ...", flush=True)
    t0 = time.perf_counter()
    summary = generator.generate(config, args.data_dir)
    gen_seconds = time.perf_counter() - t0

    print("loading ...", flush=True)
    db_path = args.work_dir / "bench.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    ref = pipeline.run_load_reference(conn, args.data_dir / "reference")
    t0 = time.perf_counter()
    results = pipeline.run_input(conn, args.data_dir / "input")
    load_seconds = time.perf_counter() - t0
    if any(r.status != pipeline.SUCCESS for r in results):
        print("a batch did not succeed:", [(r.source_file, r.status, r.error_message) for r in results])
        return 1
    stage_totals: dict[str, int] = {}
    for r in results:
        for name, ms in r.stage_timings_ms.items():
            stage_totals[name] = stage_totals.get(name, 0) + ms
    total_rows = sum(r.rows_read for r in results)

    print("reconciling ...", flush=True)
    t0 = time.perf_counter()
    rec = reconcile.run_reconciliation(conn)
    rec_seconds = time.perf_counter() - t0
    oracle = compare_with_oracle(conn, args.data_dir / "labels")
    balance_rows = conn.execute("SELECT COUNT(*) FROM daily_inventory_balance").fetchone()[0]
    pairs = conn.execute("SELECT COUNT(*) FROM (SELECT DISTINCT sku, location_id FROM daily_inventory_balance)").fetchone()[0]

    print("incremental vs full rebuild ...", flush=True)
    from inventory_pipeline.balances import shift_day
    late_date = shift_day(summary.horizon, -args.late_days)
    late_dir = args.work_dir / "late"
    late_dir.mkdir()
    late_file = late_dir / "late_batch.csv"
    write_late_batch(late_file, conn, args.late_rows, late_date)
    conn.close()
    incremental = time_late_batch(db_path, late_file, args.work_dir, full_rebuild=False, repeats=args.repeats)
    full = time_late_batch(db_path, late_file, args.work_dir, full_rebuild=True, repeats=args.repeats)
    same = incremental.pop("balances") == full.pop("balances")

    results_doc = {
        "finished_at": db.utc_now_iso(),
        "config": config.__dict__,
        "machine": machine_info(),
        "generator": {**{k: v for k, v in summary.__dict__.items() if k != "files"}, "seconds": round(gen_seconds, 3)},
        "load": {
            "rows": total_rows,
            "seconds": round(load_seconds, 3),
            "rows_per_second": round(total_rows / load_seconds, 1),
            "reference_ms": ref.duration_ms,
            "stage_totals_ms": stage_totals,
            "balance_rows": balance_rows,
            "pairs": pairs,
            "batches": [
                {k: getattr(r, k) for k in ("source_file", "status", "rows_read", "rows_accepted", "rows_rejected",
                                            "rows_duplicate", "rows_late", "rows_superseding", "pairs_rebuilt",
                                            "balance_rows_written", "duration_ms", "stage_timings_ms")}
                for r in results
            ],
        },
        "reconcile": {
            "seconds": round(rec_seconds, 3),
            "overall_status": rec.overall_status,
            "checks": {c.name: c.status for c in rec.checks},
        },
        "oracle": oracle,
        "late_rows": args.late_rows,
        "late_days_before_horizon": args.late_days,
        "repeats": args.repeats,
        "incremental": incremental,
        "full": full,
        "incremental_equals_full": same,
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(results_doc, indent=2) + "\n", encoding="utf-8")
    args.doc.write_text(render_markdown(results_doc), encoding="utf-8")
    print(f"load: {total_rows:,} rows in {load_seconds:.2f}s ({total_rows / load_seconds:,.0f} rows/s); "
          f"reconciliation {rec.overall_status}; oracle match: rejects={oracle['rejects_by_reason_match']} "
          f"batches={oracle['batch_counts_match']} balance_mismatches={oracle['final_balance_mismatched_pairs']}; "
          f"late batch rebuild median incremental={incremental['rebuild_ms_median']}ms full={full['rebuild_ms_median']}ms "
          f"same_result={same}")
    ok = rec.passed and oracle["rejects_by_reason_match"] and oracle["batch_counts_match"] \
        and oracle["final_balance_mismatched_pairs"] == 0 and same
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
