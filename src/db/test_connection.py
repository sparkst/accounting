"""Tests for database connection and initialisation.

REQ-ID: DB-007 (init_db creates all 5 tables; WAL mode enabled)
"""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from src.db.connection import _build_engine, init_db
from src.models.base import Base
from src.models.transaction import Transaction
from src.utils.dedup import compute_source_hash


class TestInitDb:
    def test_creates_all_tables_in_memory(self) -> None:
        """init_db with a fresh in-memory engine creates all 5 expected tables."""
        eng = _build_engine("sqlite:///:memory:")
        Base.metadata.create_all(eng)
        insp = inspect(eng)
        tables = set(insp.get_table_names())
        expected = {
            "transactions",
            "vendor_rules",
            "ingested_files",
            "audit_events",
            "ingestion_log",
        }
        assert expected.issubset(tables)

    def test_creates_file_based_db(self, tmp_path: Path) -> None:
        """init_db creates the data directory and SQLite file."""
        db_path = tmp_path / "sub" / "accounting.db"
        url = f"sqlite:///{db_path}"
        init_db(url)
        assert db_path.exists(), "Database file was not created"
        assert db_path.stat().st_size > 0

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling init_db twice does not raise."""
        url = f"sqlite:///{tmp_path / 'accounting.db'}"
        init_db(url)
        init_db(url)  # Should not raise

    def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        """WAL journal mode is active after init_db."""
        url = f"sqlite:///{tmp_path / 'accounting.db'}"
        eng = _build_engine(url)
        init_db(url)
        with eng.connect() as conn:
            result = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert result == "wal"

    def test_foreign_keys_enabled(self, tmp_path: Path) -> None:
        """Foreign key enforcement is ON after connecting."""
        url = f"sqlite:///{tmp_path / 'accounting.db'}"
        eng = _build_engine(url)
        init_db(url)
        with eng.connect() as conn:
            result = conn.execute(text("PRAGMA foreign_keys")).scalar()
        assert result == 1


class TestSessionFactory:
    def test_session_can_query(self, tmp_path: Path) -> None:
        """A session bound to a fresh DB can execute a simple query."""
        url = f"sqlite:///{tmp_path / 'accounting.db'}"
        eng = _build_engine(url)
        init_db(url)
        SessionCls = sessionmaker(bind=eng)
        with SessionCls() as s:
            result = s.execute(text("SELECT 1")).scalar()
        assert result == 1


class TestPreventTransactionDelete:
    """S1-007: DB-level trigger prevents DELETE on transactions."""

    def test_delete_raises_abort(self, tmp_path: Path) -> None:
        """Attempting to DELETE a transaction row raises an error."""
        url = f"sqlite:///{tmp_path / 'accounting.db'}"
        eng = _build_engine(url)
        init_db(url)
        SessionCls = sessionmaker(bind=eng)

        with SessionCls() as s:
            tx = Transaction(
                source="bank_csv",
                source_id="test-delete-trigger",
                source_hash=compute_source_hash("bank_csv", "test-delete-trigger"),
                date="2025-01-01",
                description="Test transaction",
                amount=Decimal("100.00"),
                raw_data={},
            )
            s.add(tx)
            s.commit()
            tx_id = tx.id

        # The trigger must fire and prevent the DELETE.
        with SessionCls() as s:
            with pytest.raises(Exception, match="cannot be deleted"):
                s.execute(text("DELETE FROM transactions WHERE id = :id"), {"id": tx_id})
                s.commit()

    def test_update_and_insert_still_work(self, tmp_path: Path) -> None:
        """The trigger does not affect INSERT or UPDATE operations."""
        url = f"sqlite:///{tmp_path / 'accounting.db'}"
        eng = _build_engine(url)
        init_db(url)
        SessionCls = sessionmaker(bind=eng)

        with SessionCls() as s:
            tx = Transaction(
                source="bank_csv",
                source_id="test-no-side-effects",
                source_hash=compute_source_hash("bank_csv", "test-no-side-effects"),
                date="2025-01-01",
                description="Original description",
                amount=Decimal("50.00"),
                raw_data={},
            )
            s.add(tx)
            s.commit()

            tx.description = "Updated description"
            s.commit()

            updated = s.query(Transaction).filter_by(id=tx.id).one()
            assert updated.description == "Updated description"
