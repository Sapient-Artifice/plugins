"""Migration test — upgrading a pre-existing settings table gains the missed
policy columns with correct defaults, without touching existing rows.
"""
from __future__ import annotations

from sqlalchemy import create_engine, text


def _old_settings_table(engine):
    """Create a 'settings' table shaped like a pre-upgrade install."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE settings ("
            " id INTEGER PRIMARY KEY,"
            " cleanup_enabled INTEGER NOT NULL DEFAULT 0,"
            " task_retention_days INTEGER NOT NULL DEFAULT 30)"
        )
        conn.exec_driver_sql("INSERT INTO settings (id, cleanup_enabled) VALUES (1, 1)")


def test_missed_columns_added_with_defaults():
    import db

    engine = create_engine("sqlite:///:memory:")
    _old_settings_table(engine)

    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(settings)").fetchall()}
        assert "missed_task_policy" not in cols  # precondition: old shape

        db._add_column_if_missing(
            conn, "settings", cols, "missed_task_policy",
            "TEXT NOT NULL DEFAULT 'always_ask'",
        )
        db._add_column_if_missing(
            conn, "settings", cols, "missed_grace_seconds",
            "INTEGER NOT NULL DEFAULT 300",
        )

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT missed_task_policy, missed_grace_seconds, cleanup_enabled "
                 "FROM settings WHERE id = 1")
        ).fetchone()

    # Existing row keeps its value; new columns get their defaults.
    assert row[0] == "always_ask"
    assert row[1] == 300
    assert row[2] == 1


def test_idempotent_when_columns_present():
    import db

    engine = create_engine("sqlite:///:memory:")
    _old_settings_table(engine)

    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(settings)").fetchall()}
        db._add_column_if_missing(conn, "settings", cols, "missed_task_policy",
                                  "TEXT NOT NULL DEFAULT 'always_ask'")

    # Second pass: column already exists — must be a no-op, not an error.
    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(settings)").fetchall()}
        db._add_column_if_missing(conn, "settings", cols, "missed_task_policy",
                                  "TEXT NOT NULL DEFAULT 'always_ask'")
        final = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(settings)").fetchall()}

    assert list(final).count("missed_task_policy") == 1
