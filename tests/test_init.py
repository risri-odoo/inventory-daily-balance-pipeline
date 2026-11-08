"""Smoke test: `init` creates every table and view, and is safe to run twice."""

from pathlib import Path

from inventory_pipeline import cli, db, schema


def test_init_creates_every_table_and_view(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"

    exit_code = cli.main(["init", "--db", str(db_path)])

    assert exit_code == 0
    assert db_path.exists()
    conn = db.connect(db_path)
    try:
        tables = db.list_objects(conn, "table")
        views = db.list_objects(conn, "view")
    finally:
        conn.close()
    for table in schema.TABLE_NAMES:
        assert table in tables, f"missing table {table}"
    for view in schema.VIEW_NAMES:
        assert view in views, f"missing view {view}"


def test_init_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"

    assert cli.main(["init", "--db", str(db_path)]) == 0
    assert cli.main(["init", "--db", str(db_path)]) == 0


def test_init_replaces_old_view_definitions_and_keeps_data(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "test.db")
    try:
        db.init_db(conn)
        conn.execute("DROP VIEW v_movement_leg")
        conn.execute("DROP VIEW v_movement_current")
        conn.execute("CREATE VIEW v_movement_current AS SELECT * FROM movement_event")   # an outdated definition
        conn.execute("INSERT INTO dim_sku VALUES ('S1', 'Widget', 'EA', '2026-01-01T00:00:00Z')")

        db.init_db(conn)

        view_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'v_movement_current'").fetchone()[0]
        assert "NOT EXISTS" in view_sql
        assert db.list_objects(conn, "view") == sorted(schema.VIEW_NAMES)
        assert conn.execute("SELECT COUNT(*) FROM dim_sku").fetchone()[0] == 1
    finally:
        conn.close()


def test_views_are_queryable_on_empty_db(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "test.db")
    try:
        db.init_db(conn)
        assert conn.execute("SELECT COUNT(*) FROM v_movement_current").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM v_movement_leg").fetchone()[0] == 0
    finally:
        conn.close()
