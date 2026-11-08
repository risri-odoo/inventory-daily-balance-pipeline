"""Read-only HTML dashboard: one self-contained file to SEE the results during a walkthrough.

The page works offline: inline CSS and vanilla JavaScript, no external URLs, no server.

Two kinds of content:
- Static sections (header, reconciliation cards, run log, rejects by reason) are rendered here in
  Python, with every value passed through html.escape().
- Interactive sections (quarantine filter, balance explorer, movement history) are drawn by the
  page's JavaScript from data embedded as JSON. The JavaScript only ever writes data with
  textContent (never innerHTML), and the JSON is embedded with <, >, & escaped, so a value like
  "</script><script>..." in raw_record can neither end the data block nor run.

The dashboard only reads the database (it runs the same reconciliation checks as `report`).
"""

import html
import json
import re
import sqlite3
from pathlib import Path
from typing import Optional

from inventory_pipeline import reconcile
from inventory_pipeline.db import utc_now_iso

MAX_EXAMPLE_COLUMNS = 8

BALANCE_COLUMNS = ["sku", "location_id", "balance_date", "opening_qty", "receipt_qty", "issue_qty",
                   "transfer_in_qty", "transfer_out_qty", "adjustment_qty", "net_change_qty", "closing_qty"]
MOVEMENT_COLUMNS = ["arrival", "movement_id", "version", "movement_type", "sku", "location_id", "to_location_id",
                    "quantity", "business_date", "event_ts", "is_late", "source_file", "source_line", "is_current"]
QUARANTINE_COLUMNS = ["source_file", "source_line", "movement_id", "reject_reason", "raw_record", "reject_details"]

PLACEHOLDER = re.compile(r"__(?:OVERALL|HEADER|CHECKS|RUNS|REJECT_SUMMARY|DATA)__")


# ---------------------------------------------------------------------------
# Reading the data
# ---------------------------------------------------------------------------


def query_rows(conn: sqlite3.Connection, sql: str) -> list[list]:
    """Run a query and return plain lists (compact to embed as JSON)."""
    return [list(row) for row in conn.execute(sql).fetchall()]


def collect_data(conn: sqlite3.Connection) -> dict:
    """Everything the dashboard shows, read from the database."""
    result = reconcile.run_reconciliation(conn)
    horizon = conn.execute("SELECT MAX(balance_date) FROM daily_inventory_balance").fetchone()[0]
    on_hand = conn.execute(
        "SELECT COALESCE(SUM(closing_qty), 0) FROM daily_inventory_balance WHERE balance_date = ?", (horizon,)
    ).fetchone()[0] if horizon else 0
    runs = [dict(row) for row in conn.execute(
        """
        SELECT run_id, source_file, status, started_at, rows_read, rows_accepted, rows_rejected, rows_duplicate,
               rows_late, rows_superseding, pairs_rebuilt, balance_rows_written, duration_ms, error_message
        FROM pipeline_run ORDER BY rowid
        """
    )]
    rejects_by_reason = query_rows(
        conn, "SELECT reject_reason, COUNT(*) FROM quarantine_movement GROUP BY reject_reason ORDER BY COUNT(*) DESC, reject_reason")
    return {
        "generated_at": utc_now_iso(),
        "reconciliation": result,
        "horizon": horizon,
        "on_hand_at_horizon": on_hand,
        "pairs": conn.execute("SELECT COUNT(*) FROM (SELECT DISTINCT sku, location_id FROM daily_inventory_balance)").fetchone()[0],
        "runs": runs,
        "rejects_by_reason": rejects_by_reason,
        # Embedded for the interactive sections:
        "balances": query_rows(conn, f"SELECT {', '.join(BALANCE_COLUMNS)} FROM daily_inventory_balance ORDER BY sku, location_id, balance_date"),
        "movements": query_rows(conn, """
            SELECT e.rowid, e.movement_id, e.version, e.movement_type, e.sku, e.location_id, e.to_location_id,
                   e.quantity, e.business_date, e.event_ts, e.is_late, e.source_file, e.source_line,
                   CASE WHEN c.movement_id IS NULL THEN 0 ELSE 1 END
            FROM movement_event AS e
            LEFT JOIN v_movement_current AS c ON c.movement_id = e.movement_id AND c.version = e.version
            ORDER BY e.movement_id, e.version
        """),
        "quarantine": query_rows(conn, f"SELECT {', '.join(QUARANTINE_COLUMNS)} FROM quarantine_movement ORDER BY quarantine_id"),
    }


# ---------------------------------------------------------------------------
# Rendering the static sections (every value is escaped)
# ---------------------------------------------------------------------------


def esc(value: object) -> str:
    """HTML-escape any value; None becomes an empty string."""
    return "" if value is None else html.escape(str(value), quote=True)


def status_badge(status: Optional[str]) -> str:
    """A coloured status label, e.g. PASS / WARN / FAIL / SUCCESS / FAILED."""
    css = (status or "").lower()
    return f'<span class="badge badge-{esc(css)}">{esc(status)}</span>'


def render_table(columns: list[str], rows: list[list], row_classes: Optional[list[str]] = None) -> str:
    """A simple HTML table; cells are escaped."""
    head = "".join(f"<th>{esc(c)}</th>" for c in columns)
    body = []
    for index, row in enumerate(rows):
        css = f' class="{esc(row_classes[index])}"' if row_classes and row_classes[index] else ""
        body.append(f"<tr{css}>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>")
    if not rows:
        body.append(f'<tr><td colspan="{len(columns)}" class="muted">none</td></tr>')
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def render_header(data: dict) -> str:
    """Overall status and the headline numbers."""
    result = data["reconciliation"]
    counts = result.table_counts
    tiles = [
        ("Reconciliation", status_badge(result.overall_status)),
        ("Horizon (latest day)", esc(data["horizon"] or "none")),
        ("On hand at horizon", f'<span id="on-hand">{esc(data["on_hand_at_horizon"])}</span>'),
        ("Pairs", esc(data["pairs"])),
        ("Balance rows", esc(counts.get("daily_inventory_balance", 0))),
        ("Movement events", esc(counts.get("movement_event", 0))),
        ("Quarantined rows", f'<span id="quarantined">{esc(counts.get("quarantine_movement", 0))}</span>'),
        ("Runs", esc(counts.get("pipeline_run", 0))),
    ]
    cells = "".join(f'<div class="tile"><div class="tile-label">{esc(label)}</div><div class="tile-value">{value}</div></div>'
                    for label, value in tiles)
    return f'<div class="tiles">{cells}</div><p class="muted">Generated at {esc(data["generated_at"])}. Read-only view of the database.</p>'


def render_checks(result: reconcile.ReconciliationReport) -> str:
    """One card per reconciliation check, with up to 5 example rows."""
    cards = []
    for check in result.checks:
        examples = ""
        if check.examples:
            columns = list(check.examples[0].keys())[:MAX_EXAMPLE_COLUMNS]
            examples = "<details><summary>Examples</summary>" + render_table(
                columns, [[row.get(c) for c in columns] for row in check.examples]) + "</details>"
        cards.append(
            f'<div class="card card-{esc(check.status.lower())}">'
            f'<div class="card-title">{status_badge(check.status)} <strong>{esc(check.name)}</strong>'
            f'<span class="muted"> &middot; failures: {esc(check.failures)}</span></div>'
            f'<p>{esc(check.description)}</p>'
            f'<p class="muted">{esc(check.details)}</p>{examples}</div>'
        )
    return '<div class="cards">' + "".join(cards) + "</div>"


def render_runs(runs: list[dict]) -> str:
    """The run log in insertion order."""
    columns = ["run_id", "source_file", "status", "rows_read", "rows_accepted", "rows_rejected", "rows_duplicate",
               "rows_late", "rows_superseding", "pairs_rebuilt", "balance_rows_written", "duration_ms", "error_message"]
    rows = [[run[c] for c in columns] for run in runs]
    classes = ["row-bad" if run["status"] in ("FAILED", "REJECTED") else "" for run in runs]
    return render_table(columns, rows, classes)


def render_reject_summary(rejects_by_reason: list[list]) -> str:
    """Quarantined rows per reason."""
    return render_table(["reject_reason", "rows"], rejects_by_reason)


# ---------------------------------------------------------------------------
# Embedding data safely
# ---------------------------------------------------------------------------


def safe_json(payload: object) -> str:
    """JSON that is safe inside <script type="application/json">.

    '<', '>' and '&' become \\u003c, \\u003e, \\u0026 (so "</script>" or "<!--" in the data
    cannot end the script block), and the two Unicode line separators are escaped too.
    JSON.parse turns them back into the original characters.
    """
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return (text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
                .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def build_html(data: dict) -> str:
    """Fill the page template."""
    embedded = {
        "balanceColumns": BALANCE_COLUMNS,
        "balances": data["balances"],
        "movementColumns": MOVEMENT_COLUMNS,
        "movements": data["movements"],
        "quarantineColumns": QUARANTINE_COLUMNS,
        "quarantine": data["quarantine"],
    }
    values = {
        "__OVERALL__": esc(data["reconciliation"].overall_status),
        "__HEADER__": render_header(data),
        "__CHECKS__": render_checks(data["reconciliation"]),
        "__RUNS__": render_runs(data["runs"]),
        "__REJECT_SUMMARY__": render_reject_summary(data["rejects_by_reason"]),
        "__DATA__": safe_json(embedded),
    }
    # One pass over the template: inserted text is never scanned again, so data that happens
    # to contain a placeholder name (e.g. a raw_record "__DATA__") is left alone.
    return PLACEHOLDER.sub(lambda match: values[match.group(0)], PAGE_TEMPLATE)


def write_dashboard(conn: sqlite3.Connection, out_path: Path) -> Path:
    """Read the database and write the dashboard HTML file. Returns its path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_html(collect_data(conn)), encoding="utf-8", newline="\n")
    return out_path


# ---------------------------------------------------------------------------
# The page. Placeholders like __HEADER__ are replaced by build_html().
# ---------------------------------------------------------------------------

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inventory Dashboard</title>
<style>
:root {
  --bg: #f7f7f5; --panel: #ffffff; --text: #1d1d1b; --muted: #6b6b66; --border: #e2e2dc;
  --pass: #1f7a3d; --pass-bg: #e6f4ea; --warn: #8a5a00; --warn-bg: #fff3d6;
  --fail: #b3261e; --fail-bg: #fde7e5; --accent: #2f5fb3; --neg-bg: #fde7e5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161615; --panel: #20201e; --text: #ecece8; --muted: #a3a39c; --border: #34342f;
    --pass: #7fd19b; --pass-bg: #1c3324; --warn: #f0c46b; --warn-bg: #3a2f14;
    --fail: #ff9b93; --fail-bg: #3d1d1b; --accent: #8fb3ff; --neg-bg: #3d1d1b;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header, main { max-width: 1200px; margin: 0 auto; padding: 16px; }
h1 { font-size: 22px; margin: 8px 0 12px; }
h2 { font-size: 17px; margin: 0 0 10px; }
section { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 16px; }
nav a { color: var(--accent); margin-right: 14px; text-decoration: none; }
.muted { color: var(--muted); }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
.tile { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.tile-label { color: var(--muted); font-size: 12px; }
.tile-value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.badge-pass, .badge-success { color: var(--pass); background: var(--pass-bg); }
.badge-warn, .badge-skipped, .badge-running { color: var(--warn); background: var(--warn-bg); }
.badge-fail, .badge-failed, .badge-rejected { color: var(--fail); background: var(--fail-bg); }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 10px; }
.card { border: 1px solid var(--border); border-left-width: 4px; border-radius: 8px; padding: 10px 12px; }
.card p { margin: 6px 0; }
.card-pass { border-left-color: var(--pass); } .card-warn { border-left-color: var(--warn); } .card-fail { border-left-color: var(--fail); }
.table-wrap { overflow-x: auto; max-height: 460px; overflow-y: auto; border: 1px solid var(--border); border-radius: 6px; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { padding: 4px 8px; border-bottom: 1px solid var(--border); text-align: left; white-space: nowrap; }
th { position: sticky; top: 0; background: var(--panel); font-weight: 600; }
td.num, th.num { text-align: right; }
td.raw { white-space: pre; font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
tr.row-bad td, tr.negative td { background: var(--neg-bg); }
tr.current td { font-weight: 600; }
.controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 10px; }
select, input { font: inherit; padding: 4px 8px; border: 1px solid var(--border); border-radius: 6px;
                background: var(--panel); color: var(--text); }
#chart { width: 100%; height: 240px; display: block; }
#chart .line { fill: none; stroke: var(--accent); stroke-width: 2; }
#chart .zero { stroke: var(--muted); stroke-dasharray: 4 4; }
#chart .axis { fill: var(--muted); font-size: 11px; }
#chart .point { fill: var(--accent); } #chart .point.neg { fill: var(--fail); }
.pill { display: inline-block; padding: 0 6px; border-radius: 4px; font-size: 12px; background: var(--pass-bg); color: var(--pass); }
details summary { cursor: pointer; color: var(--accent); margin: 4px 0; }
</style>
</head>
<body>
<header>
  <h1>Inventory daily balances &middot; reconciliation __OVERALL__</h1>
  <nav><a href="#recon">Reconciliation</a><a href="#runs">Run log</a><a href="#rejects">Rejects</a><a href="#balances">Balance explorer</a><a href="#history">Movement history</a></nav>
</header>
<main>
<section id="summary">__HEADER__</section>

<section id="recon"><h2>Reconciliation checks</h2>__CHECKS__</section>

<section id="runs"><h2>Run log (in the order runs were started)</h2>__RUNS__</section>

<section id="rejects">
  <h2>Rejects</h2>
  <p class="muted">Quarantined rows by reason (DUPLICATE_EVENT rows are dropped duplicates, counted separately from rejects).</p>
  __REJECT_SUMMARY__
  <div class="controls" style="margin-top:12px">
    <label>Filter <input id="q-filter" type="search" placeholder="file, movement_id, reason or text"></label>
    <span id="q-count" class="muted"></span>
  </div>
  <div class="table-wrap"><table id="q-table"><thead></thead><tbody></tbody></table></div>
</section>

<section id="balances">
  <h2>Balance explorer</h2>
  <div class="controls">
    <label>SKU <select id="b-sku"></select></label>
    <label>Location <select id="b-loc"></select></label>
    <span id="b-note" class="muted"></span>
  </div>
  <svg id="chart" viewBox="0 0 800 240" preserveAspectRatio="none" role="img" aria-label="closing quantity over time"></svg>
  <div class="table-wrap" style="margin-top:10px"><table id="b-table"><thead></thead><tbody></tbody></table></div>
</section>

<section id="history">
  <h2>Movement history</h2>
  <div class="controls">
    <label>movement_id <input id="m-id" type="search" list="m-examples" placeholder="e.g. MV-1001"></label>
    <datalist id="m-examples"></datalist>
    <span id="m-note" class="muted"></span>
  </div>
  <div class="table-wrap"><table id="m-table"><thead></thead><tbody></tbody></table></div>
  <h2 style="margin-top:14px; font-size:15px">Quarantined rows with this movement_id</h2>
  <div class="table-wrap"><table id="m-qtable"><thead></thead><tbody></tbody></table></div>
</section>
</main>

<script type="application/json" id="dashboard-data">__DATA__</script>
<script>
"use strict";
// All data comes from the JSON block above. It is only ever written with textContent.
var DATA = JSON.parse(document.getElementById("dashboard-data").textContent);
var NUMERIC = { opening_qty: 1, receipt_qty: 1, issue_qty: 1, transfer_in_qty: 1, transfer_out_qty: 1,
                adjustment_qty: 1, net_change_qty: 1, closing_qty: 1, quantity: 1, version: 1, source_line: 1, arrival: 1 };

function el(tag, text, className) {
  var node = document.createElement(tag);
  if (text !== undefined && text !== null) { node.textContent = String(text); }
  if (className) { node.className = className; }
  return node;
}

function fillTable(table, columns, rows, rowClass) {
  var head = table.querySelector("thead"), body = table.querySelector("tbody");
  head.textContent = ""; body.textContent = "";
  var tr = el("tr");
  columns.forEach(function (c) { tr.appendChild(el("th", c, NUMERIC[c] ? "num" : "")); });
  head.appendChild(tr);
  if (rows.length === 0) {
    var empty = el("tr"), td = el("td", "none", "muted");
    td.colSpan = columns.length; empty.appendChild(td); body.appendChild(empty); return;
  }
  rows.forEach(function (row) {
    var r = el("tr", null, rowClass ? rowClass(row) : "");
    row.forEach(function (cell, i) {
      var name = columns[i];
      r.appendChild(el("td", cell === null ? "" : cell, NUMERIC[name] ? "num" : (name === "raw_record" ? "raw" : "")));
    });
    body.appendChild(r);
  });
}

// ---- Rejects: filterable quarantine table ----
(function () {
  var cols = DATA.quarantineColumns, shown = cols.slice(0, 5), input = document.getElementById("q-filter");
  var LIMIT = 500;
  function render() {
    var term = input.value.trim().toLowerCase();
    var rows = DATA.quarantine.filter(function (row) {
      return term === "" || row.join(" ").toLowerCase().indexOf(term) !== -1;
    });
    fillTable(document.getElementById("q-table"), shown, rows.slice(0, LIMIT).map(function (r) { return r.slice(0, 5); }));
    document.getElementById("q-count").textContent = rows.length + " of " + DATA.quarantine.length + " rows" +
      (rows.length > LIMIT ? " (first " + LIMIT + " shown)" : "");
  }
  input.addEventListener("input", render);
  render();
})();

// ---- Balance explorer ----
(function () {
  var cols = DATA.balanceColumns, byPair = {}, skus = [];
  DATA.balances.forEach(function (row) {
    var key = row[0] + "\\u0000" + row[1];
    if (!byPair[key]) { byPair[key] = []; if (skus.indexOf(row[0]) === -1) { skus.push(row[0]); } }
    byPair[key].push(row);
  });
  var skuSel = document.getElementById("b-sku"), locSel = document.getElementById("b-loc");
  var chart = document.getElementById("chart");
  var SVG_NS = chart.namespaceURI;   // taken from the page, so no namespace URL is written in this file

  function option(select, value) { var o = el("option", value); o.value = value; select.appendChild(o); }
  function locationsFor(sku) {
    return Object.keys(byPair).filter(function (k) { return k.split("\\u0000")[0] === sku; })
                              .map(function (k) { return k.split("\\u0000")[1]; });
  }
  function svg(tag, attrs, text) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs).forEach(function (a) { node.setAttribute(a, attrs[a]); });
    if (text !== undefined) { node.textContent = String(text); }
    chart.appendChild(node);
    return node;
  }
  function drawChart(rows) {
    chart.textContent = "";
    if (rows.length === 0) { return; }
    var W = 800, H = 240, L = 50, R = 10, T = 12, B = 26;
    var values = rows.map(function (r) { return r[10]; });
    var lo = Math.min(0, Math.min.apply(null, values)), hi = Math.max(0, Math.max.apply(null, values));
    if (hi === lo) { hi = lo + 1; }
    function x(i) { return rows.length === 1 ? (L + W - R) / 2 : L + i * (W - L - R) / (rows.length - 1); }
    function y(v) { return T + (hi - v) * (H - T - B) / (hi - lo); }
    svg("line", { x1: L, x2: W - R, y1: y(0), y2: y(0), "class": "zero" });
    svg("polyline", { points: values.map(function (v, i) { return x(i) + "," + y(v); }).join(" "), "class": "line" });
    values.forEach(function (v, i) {
      var p = svg("circle", { cx: x(i), cy: y(v), r: 3.5, "class": v < 0 ? "point neg" : "point" });
      var title = document.createElementNS(SVG_NS, "title"); title.textContent = rows[i][2] + ": " + v; p.appendChild(title);
    });
    svg("text", { x: 4, y: y(hi) + 4, "class": "axis" }, hi);
    svg("text", { x: 4, y: y(lo) + 4, "class": "axis" }, lo);
    if (lo < 0 && hi > 0) { svg("text", { x: 4, y: y(0) + 4, "class": "axis" }, 0); }
    svg("text", { x: L, y: H - 6, "class": "axis" }, rows[0][2]);
    svg("text", { x: W - R - 70, y: H - 6, "class": "axis" }, rows[rows.length - 1][2]);
  }
  function render() {
    var rows = byPair[skuSel.value + "\\u0000" + locSel.value] || [];
    drawChart(rows);
    fillTable(document.getElementById("b-table"), cols.slice(2), rows.map(function (r) { return r.slice(2); }),
              function (r) { return r[r.length - 1] < 0 ? "negative" : ""; });
    var negatives = rows.filter(function (r) { return r[10] < 0; }).length;
    document.getElementById("b-note").textContent = rows.length + " days" + (negatives ? ", " + negatives + " negative (highlighted)" : "");
  }
  function fillLocations() {
    locSel.textContent = "";
    var locations = locationsFor(skuSel.value);
    locations.forEach(function (l) { option(locSel, l); });
    if (locations.length) { locSel.value = locations[0]; }
    render();
  }
  skus.forEach(function (s) { option(skuSel, s); });
  if (skus.length) { skuSel.value = skus[0]; }
  skuSel.addEventListener("change", fillLocations);
  locSel.addEventListener("change", render);
  if (skus.length) { fillLocations(); } else { document.getElementById("b-note").textContent = "no balances yet"; }
})();

// ---- Movement history ----
(function () {
  var cols = DATA.movementColumns, byId = {}, input = document.getElementById("m-id");
  DATA.movements.forEach(function (row) { (byId[row[1]] = byId[row[1]] || []).push(row); });
  // Suggest movements that have more than one version (corrections), up to 50.
  var list = document.getElementById("m-examples"), suggested = 0;
  Object.keys(byId).forEach(function (id) {
    if (byId[id].length > 1 && suggested < 50) { var o = el("option"); o.value = id; list.appendChild(o); suggested++; }
  });
  function render() {
    var id = input.value.trim();
    var rows = (byId[id] || []).slice().sort(function (a, b) { return a[2] - b[2]; });
    var display = rows.map(function (r) {
      var copy = r.slice(); copy[10] = r[10] ? "late" : ""; copy[13] = r[13] ? "CURRENT" : "history"; return copy;
    });
    fillTable(document.getElementById("m-table"), cols, display, function (r) { return r[13] === "CURRENT" ? "current" : ""; });
    var outOfOrder = rows.some(function (r, i) { return i > 0 && r[0] < rows[i - 1][0]; });
    document.getElementById("m-note").textContent = id === "" ? (suggested ? "pick a corrected movement from the list, or type any id" : "type a movement_id") :
      rows.length + " version(s)" + (outOfOrder ? "; versions arrived out of order (see 'arrival')" : "");
    var q = DATA.quarantine.filter(function (r) { return r[2] === id; }).map(function (r) { return r.slice(0, 5); });
    fillTable(document.getElementById("m-qtable"), DATA.quarantineColumns.slice(0, 5), q);
  }
  input.addEventListener("input", render);
  var first = Object.keys(byId).filter(function (id) { return byId[id].length > 1; })[0];
  if (first) { input.value = first; }
  render();
})();
</script>
</body>
</html>
"""
