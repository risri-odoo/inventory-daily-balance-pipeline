"""Synthetic data generator with an independent answer key (oracle labels).

Writes:
    <out>/reference/   dim_sku.csv, dim_location.csv, opening_balance.csv
    <out>/input/       batch_001.csv ... (the pipeline input)
    <out>/labels/      expected_rejects.csv, expected_final_balances.csv, expected_batch_counts.csv
                       (outside input/, so the pipeline never sees the answers)

How the data is built (all from one random.Random(seed), so the same seed gives
byte-identical files):
1. Base events, generated in business-date order while tracking the true on-hand balance of
   every (sku, location). An ISSUE or TRANSFER larger than the stock becomes a RECEIPT, and a
   negative ADJUSTMENT never goes below zero, so the true balance never goes negative.
2. Batches: each base event goes to the batch that covers its business date.
3. Late arrivals: some ISSUE events are moved 1-2 batches later (only issues, so stock that
   arrives late can never make an earlier day negative while waiting).
4. Receipt corrections: version 2 of some receipts with a HIGHER quantity (so a correction can
   never push later stock negative), in the same or a later batch.
5. Exact duplicates and conflicting duplicates (same key, different reason_code) of base events,
   always placed after their original, so the original is the first arrival.
6. Invalid rows with ids MV-BAD-nnnnnn, each with exactly one defect, cycling through every
   validation reject code.
Finally, a small simulation walks the batches in processing order to label every row
(accepted / rejected / duplicate, late or not) and computes the final balances.
"""

import csv
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from inventory_pipeline import validate as v

HEADER = ["movement_id", "version", "movement_type", "sku", "location_id", "to_location_id",
          "quantity", "business_date", "event_ts", "reason_code", "source_system"]

# The defects injected into invalid rows, one per row, in this rotating order.
DEFECT_KINDS = [
    v.MALFORMED_ROW, v.MISSING_REQUIRED_FIELD, v.INVALID_MOVEMENT_TYPE, v.INVALID_VERSION,
    v.INVALID_QUANTITY, v.INVALID_DATE, v.INVALID_TIMESTAMP, v.UNKNOWN_SKU, v.UNKNOWN_LOCATION,
    v.INVALID_TRANSFER, v.FUTURE_DATED, v.BEFORE_OPENING_SNAPSHOT,
]

Pair = tuple[str, str]


@dataclass
class GeneratorConfig:
    """Generator settings. Rates are fractions of `rows`."""

    rows: int = 100_000
    seed: int = 42
    skus: int = 50
    locations: int = 10
    days: int = 30
    batches: int = 10
    start_date: str = "2026-03-01"
    invalid_rate: float = 0.02
    duplicate_rate: float = 0.01
    conflict_rate: float = 0.005
    correction_rate: float = 0.01
    late_rate: float = 0.02
    opening_share: float = 0.7      # share of (sku, location) pairs that get an opening balance


@dataclass
class GenRow:
    """One generated CSV row plus what we know about it."""

    values: list[str]               # the 11 CSV values (12 for a malformed row)
    batch: int
    position: float                 # sort key inside the batch
    label: Optional[str] = None     # the reject code this row must get, or None if it is valid
    kind: str = "base"              # base / correction / duplicate / conflict / invalid

    @property
    def key(self) -> tuple[str, str]:
        return (self.values[0], self.values[1])


@dataclass
class GeneratorSummary:
    """What was generated (for logs and the benchmark)."""

    rows_written: int
    batch_files: int
    base_events: int
    corrections: int
    duplicates: int
    conflicts: int
    invalid: int
    late: int
    horizon: str
    pairs: int
    files: list[Path] = field(default_factory=list)


def day_string(start: date, offset: int) -> str:
    return (start + timedelta(days=offset)).isoformat()


def event_ts(rng: random.Random, day: str) -> str:
    """A random UTC time on the given day."""
    return f"{day}T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}Z"


def make_base_event(rng: random.Random, number: int, day: str, skus: list[str], locations: list[str],
                    balance: dict[Pair, int]) -> list[str]:
    """One valid movement that keeps every true balance >= 0; updates `balance`."""
    sku = rng.choice(skus)
    location = rng.choice(locations)
    roll = rng.random()
    movement_type = "RECEIPT" if roll < 0.35 else "ISSUE" if roll < 0.70 else "TRANSFER" if roll < 0.90 else "ADJUSTMENT"
    on_hand = balance.get((sku, location), 0)
    to_location = ""
    reason = "PO"

    if movement_type in ("ISSUE", "TRANSFER") and on_hand < 1:
        movement_type = "RECEIPT"            # nothing on hand: receive instead
    if movement_type == "RECEIPT":
        quantity = rng.randint(1, 100)
        balance[(sku, location)] = on_hand + quantity
    elif movement_type == "ISSUE":
        quantity = rng.randint(1, min(50, on_hand))
        balance[(sku, location)] = on_hand - quantity
        reason = "SO"
    elif movement_type == "TRANSFER":
        quantity = rng.randint(1, min(50, on_hand))
        to_location = rng.choice([loc for loc in locations if loc != location])
        balance[(sku, location)] = on_hand - quantity
        balance[(sku, to_location)] = balance.get((sku, to_location), 0) + quantity
        reason = "REPLENISH"
    else:  # ADJUSTMENT
        quantity = rng.choice([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5])
        if on_hand + quantity < 0:
            quantity = -quantity
        balance[(sku, location)] = on_hand + quantity
        reason = "CYCLE_COUNT"

    return [f"MV-{number:07d}", "1", movement_type, sku, location, to_location, str(quantity),
            day, event_ts(rng, day), reason, "GEN"]


def make_invalid_row(rng: random.Random, number: int, defect: str, start: date, days: int,
                     skus: list[str], locations: list[str], openings: dict[Pair, tuple[str, int]]) -> list[str]:
    """A row that is valid except for exactly one defect."""
    day = day_string(start, rng.randrange(days))
    values = [f"MV-BAD-{number:06d}", "1", "RECEIPT", rng.choice(skus), rng.choice(locations), "",
              "10", day, f"{day}T08:00:00Z", "BAD", "GEN"]
    if defect == v.MALFORMED_ROW:
        values.append("EXTRA")
    elif defect == v.MISSING_REQUIRED_FIELD:
        values[3] = ""
    elif defect == v.INVALID_MOVEMENT_TYPE:
        values[2] = "RETURN"
    elif defect == v.INVALID_VERSION:
        values[1] = "0"
    elif defect == v.INVALID_QUANTITY:
        values[6] = "abc"
    elif defect == v.INVALID_DATE:
        values[7] = day[:5] + "02-30"
    elif defect == v.INVALID_TIMESTAMP:
        values[8] = "not-a-timestamp"
    elif defect == v.UNKNOWN_SKU:
        values[3] = "SKU-UNKNOWN"
    elif defect == v.UNKNOWN_LOCATION:
        values[4] = "LOC-UNKNOWN"
    elif defect == v.INVALID_TRANSFER:
        values[2] = "TRANSFER"
        values[5] = values[4]                # transfer to the same location
    elif defect == v.FUTURE_DATED:
        values[7] = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    elif defect == v.BEFORE_OPENING_SNAPSHOT:
        sku, location = rng.choice(sorted(openings))
        values[3], values[4] = sku, location
        values[7] = openings[(sku, location)][0]          # on the as_of_date: closed period
    else:
        raise ValueError(f"unknown defect {defect}")
    return values


def signed_legs(values: list[str]) -> list[tuple[Pair, int]]:
    """The ledger legs of a valid row: ((sku, location), signed qty)."""
    movement_type, sku, location, to_location, quantity = values[2], values[3], values[4], values[5], int(values[6])
    if movement_type == "RECEIPT":
        return [((sku, location), quantity)]
    if movement_type == "ISSUE":
        return [((sku, location), -quantity)]
    if movement_type == "TRANSFER":
        return [((sku, location), -quantity), ((sku, to_location), quantity)]
    return [((sku, location), quantity)]


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    """Write a CSV with '\\n' line endings (same bytes on every OS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def generate(config: GeneratorConfig, out_dir: Path) -> GeneratorSummary:
    """Generate reference data, batch files and oracle labels into out_dir."""
    rng = random.Random(config.seed)
    out_dir = Path(out_dir)
    start = date.fromisoformat(config.start_date)
    as_of_date = day_string(start, -1)
    last_batch = config.batches - 1

    # --- reference data -------------------------------------------------------------
    skus = [f"SKU-{i:04d}" for i in range(1, config.skus + 1)]
    locations = [f"LOC-{i:03d}" for i in range(1, config.locations + 1)]
    openings: dict[Pair, tuple[str, int]] = {}
    for sku in skus:
        for location in locations:
            if rng.random() < config.opening_share:
                openings[(sku, location)] = (as_of_date, rng.randint(0, 200))

    # --- how many rows of each kind ----------------------------------------------------
    n_invalid = round(config.rows * config.invalid_rate)
    n_duplicate = round(config.rows * config.duplicate_rate)
    n_conflict = round(config.rows * config.conflict_rate)
    n_correction = round(config.rows * config.correction_rate)
    n_base = config.rows - n_invalid - n_duplicate - n_conflict - n_correction
    if n_base < 1:
        raise ValueError("defect rates leave no room for valid rows")

    # --- 1-2. base events in business-date order, assigned to batches -------------------------
    balance: dict[Pair, int] = {pair: qty for pair, (_, qty) in openings.items()}
    base: list[GenRow] = []
    for i in range(n_base):
        day_index = i * config.days // n_base
        values = make_base_event(rng, i + 1, day_string(start, day_index), skus, locations, balance)
        batch = day_index * config.batches // config.days
        base.append(GenRow(values=values, batch=batch, position=rng.random()))

    # --- 3. late arrivals: issues moved 1-2 batches later -------------------------------------
    issue_rows = [row for row in base if row.values[2] == "ISSUE" and row.batch < last_batch]
    late_rows = rng.sample(issue_rows, min(round(config.rows * config.late_rate), len(issue_rows)))
    for row in late_rows:
        row.batch = min(last_batch, row.batch + rng.randint(1, 2))

    extras: list[GenRow] = []

    # --- 4. receipt corrections (version 2, higher quantity) ----------------------------------
    receipts = [row for row in base if row.values[2] == "RECEIPT"]
    for original in rng.sample(receipts, min(n_correction, len(receipts))):
        values = list(original.values)
        values[1] = "2"
        values[6] = str(int(values[6]) + rng.randint(1, 20))
        values[8] = f"{values[7]}T23:59:59Z"
        values[9] = "PO-CORRECTION"
        extras.append(GenRow(values=values, batch=rng.randint(original.batch, last_batch),
                             position=rng.random(), kind="correction"))

    # --- 5. duplicates, always after their original ---------------------------------------------
    def place_after(original: GenRow) -> tuple[int, float]:
        batch = rng.randint(original.batch, last_batch)
        if batch == original.batch:
            return batch, original.position + (1.0 - original.position) * max(rng.random(), 1e-9)
        return batch, rng.random()

    for original in rng.sample(base, min(n_duplicate, len(base))):
        batch, position = place_after(original)
        extras.append(GenRow(values=list(original.values), batch=batch, position=position,
                             label=v.DUPLICATE_EVENT, kind="duplicate"))
    for original in rng.sample(base, min(n_conflict, len(base))):
        values = list(original.values)
        values[9] = "CONFLICTING-COPY"        # same key, different payload
        batch, position = place_after(original)
        extras.append(GenRow(values=values, batch=batch, position=position,
                             label=v.CONFLICTING_DUPLICATE, kind="conflict"))

    # --- 6. invalid rows, one defect each -------------------------------------------------------
    kinds = [k for k in DEFECT_KINDS if k != v.BEFORE_OPENING_SNAPSHOT or openings]
    for i in range(n_invalid):
        defect = kinds[i % len(kinds)]
        values = make_invalid_row(rng, i + 1, defect, start, config.days, skus, locations, openings)
        extras.append(GenRow(values=values, batch=rng.randint(0, last_batch), position=rng.random(),
                             label=defect, kind="invalid"))

    # --- arrange the batch files ------------------------------------------------------------------
    batches: list[list[GenRow]] = [[] for _ in range(config.batches)]
    for row in base + extras:
        batches[row.batch].append(row)
    for rows in batches:
        rows.sort(key=lambda r: r.position)

    # --- simulate processing order to label every row ---------------------------------------------
    accepted: dict[tuple[str, str], list[str]] = {}   # key -> values of the first arrival
    current: dict[str, list[str]] = {}                # movement_id -> values of the highest version
    max_loaded: Optional[str] = None
    reject_labels: list[list[str]] = []
    batch_counts: list[list[str]] = []
    late_total = 0
    for index, rows in enumerate(batches):
        file_name = f"batch_{index + 1:03d}.csv"
        late_cutoff = max_loaded
        read = ok = rejected = duplicate = late = 0
        for line_number, row in enumerate(rows, start=2):
            read += 1
            if row.kind == "invalid":
                rejected += 1
                reject_labels.append([file_name, str(line_number), row.values[0], row.label or ""])
                continue
            if row.key in accepted:
                code = v.DUPLICATE_EVENT if accepted[row.key] == row.values else v.CONFLICTING_DUPLICATE
                if code != row.label:
                    raise AssertionError(f"generator bug: {row.key} expected {row.label}, simulated {code}")
                reject_labels.append([file_name, str(line_number), row.values[0], code])
                if code == v.DUPLICATE_EVENT:
                    duplicate += 1
                else:
                    rejected += 1
                continue
            accepted[row.key] = row.values
            ok += 1
            business_date = row.values[7]
            if late_cutoff is not None and business_date < late_cutoff:
                late += 1
            if max_loaded is None or business_date > max_loaded:
                max_loaded = business_date
            movement_id, version = row.values[0], int(row.values[1])
            if movement_id not in current or version > int(current[movement_id][1]):
                current[movement_id] = row.values
        late_total += late
        batch_counts.append([file_name, str(read), str(ok), str(rejected), str(duplicate), str(late)])

    # --- final balances from the current versions --------------------------------------------------
    final: dict[Pair, int] = {pair: qty for pair, (_, qty) in openings.items()}
    for values in current.values():
        for pair, delta in signed_legs(values):
            final[pair] = final.get(pair, 0) + delta
    horizon = max_loaded or as_of_date

    # --- write everything ---------------------------------------------------------------------------
    files = []
    ref_dir, input_dir, labels_dir = out_dir / "reference", out_dir / "input", out_dir / "labels"
    write_csv(ref_dir / "dim_sku.csv", ["sku", "description", "uom"], [[s, f"Item {s}", "EA"] for s in skus])
    write_csv(ref_dir / "dim_location.csv", ["location_id", "location_name", "location_type"],
              [[loc, f"Location {loc}", "WAREHOUSE" if i == 0 else "STORE"] for i, loc in enumerate(locations)])
    write_csv(ref_dir / "opening_balance.csv", ["sku", "location_id", "as_of_date", "quantity"],
              [[s, loc, d, str(q)] for (s, loc), (d, q) in sorted(openings.items())])
    files += [ref_dir / "dim_sku.csv", ref_dir / "dim_location.csv", ref_dir / "opening_balance.csv"]
    for index, rows in enumerate(batches):
        path = input_dir / f"batch_{index + 1:03d}.csv"
        write_csv(path, HEADER, [row.values for row in rows])
        files.append(path)
    write_csv(labels_dir / "expected_rejects.csv", ["source_file", "source_line", "movement_id", "reject_reason"], reject_labels)
    write_csv(labels_dir / "expected_final_balances.csv", ["sku", "location_id", "balance_date", "closing_qty"],
              [[s, loc, horizon, str(q)] for (s, loc), q in sorted(final.items())])
    write_csv(labels_dir / "expected_batch_counts.csv",
              ["source_file", "rows_read", "rows_accepted", "rows_rejected", "rows_duplicate", "rows_late"], batch_counts)
    files += [labels_dir / "expected_rejects.csv", labels_dir / "expected_final_balances.csv", labels_dir / "expected_batch_counts.csv"]

    corrections = sum(1 for row in extras if row.kind == "correction")
    return GeneratorSummary(
        rows_written=sum(len(rows) for rows in batches),
        batch_files=config.batches,
        base_events=len(base),
        corrections=corrections,
        duplicates=sum(1 for row in extras if row.kind == "duplicate"),
        conflicts=sum(1 for row in extras if row.kind == "conflict"),
        invalid=n_invalid,
        late=late_total,
        horizon=horizon,
        pairs=len(final),
        files=files,
    )
