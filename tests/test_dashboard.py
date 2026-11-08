"""The read-only HTML dashboard: content, offline-only, escaping, empty database."""

import json
import re
import sqlite3
from pathlib import Path

from conftest import BatchWriter
from inventory_pipeline import cli, dashboard, db, pipeline, reconcile


def embedded_data(page: str) -> dict:
    """Parse the JSON block the page's JavaScript reads."""
    block = re.search(r'<script type="application/json" id="dashboard-data">(.*?)</script>', page, re.S)
    assert block is not None
    return json.loads(block.group(1))


def demo_dashboard(tmp_path: Path) -> str:
    out = tmp_path / "out"
    assert cli.main(["demo", "--output-dir", str(out)]) == 0      # demo also writes the dashboard
    return (out / "dashboard.html").read_text(encoding="utf-8")


def test_demo_dashboard_shows_the_expected_totals(tmp_path: Path) -> None:
    page = demo_dashboard(tmp_path)

    assert '<span id="on-hand">189</span>' in page                 # hand-computed final on-hand
    assert '<span id="quarantined">13</span>' in page              # 13 quarantined rows in the sample
    assert "reconciliation WARN" in page                           # overall status (negative pair)
    for check in reconcile.ALL_CHECKS:
        name = check.__name__.removeprefix("check_")
        assert f"<strong>{name}</strong>" in page
    data = embedded_data(page)
    assert len(data["balances"]) == 21 and len(data["quarantine"]) == 13
    current = {(m[1], m[2]) for m in data["movements"] if m[13] == 1}
    assert ("MV-1005", 3) in current and ("MV-1005", 2) not in current


def test_dashboard_has_no_external_urls(tmp_path: Path) -> None:
    page = demo_dashboard(tmp_path)
    assert "http://" not in page and "https://" not in page
    assert "<link" not in page and " src=" not in page              # no external CSS or scripts


def test_run_log_is_in_insertion_order(tmp_path: Path) -> None:
    page = demo_dashboard(tmp_path)
    runs_section = page[page.index('id="runs"'):page.index('id="rejects"')]
    assert runs_section.index("reference") < runs_section.index("batch_01.csv") < runs_section.index("batch_02.csv")


def test_hostile_values_are_escaped(tmp_path: Path, conn: sqlite3.Connection, write_batch: BatchWriter) -> None:
    hostile_sku = '<script>alert("x")</script>'
    rows = [
        f"BAD-1,1,RECEIPT,{hostile_sku},A,,1,2026-02-01,2026-02-01T00:00:00Z,,X",   # UNKNOWN_SKU
        "</script><b>__DATA__,1,RECEIPT,S1,A,,abc,2026-02-01,2026-02-01T00:00:00Z,,X",  # INVALID_QUANTITY
    ]
    path = write_batch("evil.csv", rows)
    assert pipeline.run_file(conn, path, path.parent).status == pipeline.SUCCESS

    page = (dashboard.write_dashboard(conn, tmp_path / "dash.html")).read_text(encoding="utf-8")

    assert "<script>alert" not in page
    assert page.count("<script") == 2                              # only the page's own two script tags
    assert page.count("</script>") == 2
    assert "<b>" not in page
    assert "\\u003cscript\\u003ealert" in page                     # escaped inside the JSON block
    data = embedded_data(page)                                     # and still parses back to the original text
    raw_records = [row[4] for row in data["quarantine"]]
    assert any(hostile_sku in raw for raw in raw_records)
    assert any(raw.startswith("</script><b>__DATA__") for raw in raw_records)


def test_hostile_values_in_server_rendered_sections_are_escaped(tmp_path: Path, conn: sqlite3.Connection,
                                                                write_batch: BatchWriter) -> None:
    # A duplicate column name is user data that reaches pipeline_run.error_message, which the
    # run log renders in Python (not via the JSON block).
    header = "movement_id,movement_type,sku,location_id,quantity,business_date,event_ts,<img src=x onerror=alert(1)>,<img src=x onerror=alert(1)>"
    path = write_batch("evil_header.csv", ["M1,RECEIPT,S1,A,1,2026-02-01,2026-02-01T00:00:00Z,a,b"], header=header)
    assert pipeline.run_file(conn, path, path.parent).status == pipeline.REJECTED

    page = dashboard.write_dashboard(conn, tmp_path / "dash.html").read_text(encoding="utf-8")

    assert "<img" not in page
    runs_section = page[page.index('id="runs"'):page.index('id="rejects"')]
    assert "&lt;img src=x onerror=alert(1)&gt;" in runs_section


def test_empty_database(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "empty.db")
    db.init_db(conn)
    page = dashboard.write_dashboard(conn, tmp_path / "dash.html").read_text(encoding="utf-8")
    conn.close()

    assert '<span id="on-hand">0</span>' in page
    assert "reconciliation PASS" in page
    data = embedded_data(page)
    assert data["balances"] == [] and data["movements"] == [] and data["quarantine"] == []


def test_cli_dashboard_command(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert cli.main(["demo", "--output-dir", str(out)]) == 0
    target = tmp_path / "custom" / "view.html"
    assert cli.main(["dashboard", "--db", str(out / "demo.db"), "--out", str(target)]) == 0
    assert target.exists() and "<title>Inventory Dashboard</title>" in target.read_text(encoding="utf-8")


def test_safe_json_escapes_script_breakers() -> None:
    text = dashboard.safe_json({"a": "</script><!-- &  "})
    assert "<" not in text and ">" not in text and "&" not in text and " " not in text
    assert json.loads(text) == {"a": "</script><!-- &  "}
