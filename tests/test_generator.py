"""Generator tests: determinism, and the pipeline's results equal the generator's oracle labels."""

import csv
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Iterator

import pytest

from inventory_pipeline import cli, db, generator, pipeline, reconcile

SMALL = generator.GeneratorConfig(rows=3000, seed=7, skus=6, locations=4, days=12, batches=4)


def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def all_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.csv"))}


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("gen")
    generator.generate(SMALL, out)
    return out


@pytest.fixture(scope="module")
def loaded(generated: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path_factory.mktemp("db") / "gen.db")
    db.init_db(conn)
    assert pipeline.run_load_reference(conn, generated / "reference").status == pipeline.SUCCESS
    results = pipeline.run_input(conn, generated / "input")
    assert {r.status for r in results} == {"SUCCESS"}
    yield conn
    conn.close()


# --- determinism ---------------------------------------------------------------------


def test_same_seed_gives_byte_identical_files(tmp_path: Path) -> None:
    generator.generate(SMALL, tmp_path / "a")
    generator.generate(SMALL, tmp_path / "b")
    first, second = all_bytes(tmp_path / "a"), all_bytes(tmp_path / "b")
    assert first == second
    assert len(first) == 3 + SMALL.batches + 3      # reference + batches + labels


def test_different_seed_gives_different_data(tmp_path: Path) -> None:
    generator.generate(SMALL, tmp_path / "a")
    other = generator.GeneratorConfig(**{**SMALL.__dict__, "seed": 8})
    generator.generate(other, tmp_path / "b")
    assert all_bytes(tmp_path / "a" / "input") != all_bytes(tmp_path / "b" / "input")


def test_cli_generate_writes_the_expected_layout(tmp_path: Path) -> None:
    out = tmp_path / "gen"
    assert cli.main(["generate", "--rows", "500", "--seed", "3", "--batches", "3", "--out", str(out)]) == 0
    assert sorted(p.name for p in (out / "input").iterdir()) == ["batch_001.csv", "batch_002.csv", "batch_003.csv"]
    assert (out / "labels" / "expected_rejects.csv").exists()
    assert not list((out / "input").glob("expected*"))      # labels are kept out of the input folder


# --- shape of the generated data ---------------------------------------------------------


def test_row_count_and_every_defect_kind_present(generated: Path) -> None:
    data_rows = sum(len(read_csv(p)) for p in (generated / "input").glob("*.csv"))
    assert data_rows == SMALL.rows
    reasons = Counter(r["reject_reason"] for r in read_csv(generated / "labels" / "expected_rejects.csv"))
    for code in generator.DEFECT_KINDS + ["DUPLICATE_EVENT", "CONFLICTING_DUPLICATE"]:
        assert reasons[code] > 0, code
    bad_ids = {r["movement_id"] for r in read_csv(generated / "labels" / "expected_rejects.csv")
               if r["reject_reason"] not in ("DUPLICATE_EVENT", "CONFLICTING_DUPLICATE")}
    assert all(i.startswith("MV-BAD-") for i in bad_ids)


def test_true_balances_never_negative(generated: Path) -> None:
    assert all(int(r["closing_qty"]) >= 0 for r in read_csv(generated / "labels" / "expected_final_balances.csv"))


# --- the pipeline agrees with the oracle ---------------------------------------------------


def test_rejects_equal_labels_row_by_row(loaded: sqlite3.Connection, generated: Path) -> None:
    actual = [
        {k: str(v) for k, v in dict(row).items()}
        for row in loaded.execute(
            "SELECT source_file, source_line, movement_id, reject_reason FROM quarantine_movement ORDER BY source_file, source_line"
        )
    ]
    expected = read_csv(generated / "labels" / "expected_rejects.csv")
    assert Counter(r["reject_reason"] for r in actual) == Counter(r["reject_reason"] for r in expected)
    assert actual == expected


def test_final_balances_equal_oracle(loaded: sqlite3.Connection, generated: Path) -> None:
    expected = {(r["sku"], r["location_id"], r["balance_date"]): int(r["closing_qty"])
                for r in read_csv(generated / "labels" / "expected_final_balances.csv")}
    horizon = next(iter(expected))[2]
    actual = {(sku, loc, day): qty for sku, loc, day, qty in loaded.execute(
        "SELECT sku, location_id, balance_date, closing_qty FROM daily_inventory_balance WHERE balance_date = ?", (horizon,))}
    assert actual == expected


def test_batch_counts_equal_labels(loaded: sqlite3.Connection, generated: Path) -> None:
    columns = ["source_file", "rows_read", "rows_accepted", "rows_rejected", "rows_duplicate", "rows_late"]
    actual = [{c: str(row[c]) for c in columns} for row in loaded.execute(
        f"SELECT {', '.join(columns)} FROM pipeline_run WHERE source_file LIKE 'batch_%' ORDER BY source_file")]
    assert actual == read_csv(generated / "labels" / "expected_batch_counts.csv")
    assert sum(int(r["rows_late"]) for r in actual) > 0


def test_reconciliation_passes_on_generated_data(loaded: sqlite3.Connection) -> None:
    result = reconcile.run_reconciliation(loaded)
    assert {c.name: c.status for c in result.checks} == {c.name: "PASS" for c in result.checks}
