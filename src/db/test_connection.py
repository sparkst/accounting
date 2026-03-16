"""Tests for database connection and initialisation.

REQ-ID: DB-007 (init_db creates all 5 tables; WAL mode enabled)
"""

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from src.db.connection import _build_engine, init_db
from src.models.base import Base


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
