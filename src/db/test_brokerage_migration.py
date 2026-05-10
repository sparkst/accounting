"""Migration round-trip test for the brokerage tables (REQ-005, TASK-04).

Verifies the brokerage Alembic migration directly by running upgrade/downgrade
on the brokerage migration revision against a pre-seeded tmp SQLite DB.

Approach: stamp the DB at the revision *before* the brokerage migration
(``4cbdb23e658f``), then upgrade to the brokerage revision
(``ceb6f498e2b1``), assert the 4 tables exist, verify the CHECK constraint
on ``broker``, downgrade back, and re-upgrade to confirm round-trip.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

# Brokerage migration revision identifiers.
_BROKERAGE_REVISION = "ceb6f498e2b1"
_PREV_REVISION = "4cbdb23e658f"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_alembic(*args: str) -> None:
    """Run alembic with the given CLI arguments (in-process)."""
    from alembic.config import main as alembic_main

    alembic_main(list(args))


def _table_names(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _column_names(engine: sa.Engine, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(engine).get_columns(table)}


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_brokerage_migration_round_trip(tmp_path: Path) -> None:
    """Full upgrade → assert tables → bad-broker IntegrityError → downgrade → upgrade."""
    db_path = tmp_path / "test_brok.db"
    os.environ["DATABASE_PATH"] = str(db_path)

    try:
        # ── 0. create the DB and stamp it at the revision just before brokerage ──
        # We create the ingestion_log table manually so the stamp works without
        # needing to run every historic migration (those depend on tables in
        # production that won't exist in a fresh DB).
        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            # Create the alembic_version table and stamp the parent revision.
            conn.execute(sa.text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            ))
            conn.execute(sa.text(
                f"INSERT INTO alembic_version VALUES ('{_PREV_REVISION}')"
            ))
        engine.dispose()

        # ── 1. upgrade to brokerage revision ────────────────────────────────────
        _run_alembic("upgrade", _BROKERAGE_REVISION)

        engine2 = sa.create_engine(f"sqlite:///{db_path}")

        tables = _table_names(engine2)
        assert "account" in tables, f"account table missing; got {tables}"
        assert "brokerage_transaction" in tables
        assert "position_snapshot" in tables
        assert "realized_gain_loss" in tables

        # Check that key columns exist.
        assert "broker" in _column_names(engine2, "account")
        assert "account_number" in _column_names(engine2, "account")
        assert "source_row_hash" in _column_names(engine2, "brokerage_transaction")
        assert "source_row_hash" in _column_names(engine2, "position_snapshot")
        assert "source_row_hash" in _column_names(engine2, "realized_gain_loss")

        # ── 2. CHECK constraint rejects 'robinhood' broker ────────────────────
        import uuid

        with engine2.begin() as conn:
            conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            with pytest.raises((IntegrityError, sa.exc.IntegrityError)):
                conn.execute(
                    sa.text(
                        "INSERT INTO account "
                        "(id, broker, account_number, account_type, entity,"
                        " tax_sheltered, is_plan_wrapper, created_at, updated_at) "
                        "VALUES (:id, :broker, :num, :atype, :entity, 0, 0,"
                        " datetime('now'), datetime('now'))"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "broker": "robinhood",  # NOT in CHECK constraint
                        "num": "12345",
                        "atype": "taxable",
                        "entity": "personal",
                    },
                )

        engine2.dispose()

        # ── 3. downgrade -1 (back to 4cbdb23e658f) ────────────────────────────
        _run_alembic("downgrade", "-1")

        engine3 = sa.create_engine(f"sqlite:///{db_path}")
        tables_after_down = _table_names(engine3)
        assert "account" not in tables_after_down
        assert "brokerage_transaction" not in tables_after_down
        assert "position_snapshot" not in tables_after_down
        assert "realized_gain_loss" not in tables_after_down
        engine3.dispose()

        # ── 4. re-upgrade (round-trip) ─────────────────────────────────────────
        _run_alembic("upgrade", _BROKERAGE_REVISION)

        engine4 = sa.create_engine(f"sqlite:///{db_path}")
        tables_again = _table_names(engine4)
        assert "account" in tables_again
        assert "brokerage_transaction" in tables_again
        assert "position_snapshot" in tables_again
        assert "realized_gain_loss" in tables_again
        engine4.dispose()

    finally:
        os.environ.pop("DATABASE_PATH", None)
